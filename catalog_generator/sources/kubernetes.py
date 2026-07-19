"""Real Kubernetes runtime adapter (live mode) — plain REST via ``requests``.

Implements :class:`~catalog_generator.sources.base.RuntimeSource` against the
Kubernetes API without the ``kubernetes`` client package. Strictly read-only:
only GET/LIST on ``deployments``, ``replicasets`` and ``pods`` (matching the
ClusterRole in PRD §7). Authentication uses a ServiceAccount bearer token
read from an environment variable whose *name* comes from the config
(``kubernetes.token_env``) — the token value never appears in config files,
URLs, or error messages.

Error contract (PRD §6.2 "show error, don't hide"):

* Deployment not found (404)               → ``None`` (*not deployed*).
* Per-lookup failure (HTTP error, timeout) → :class:`RuntimeState` with
  ``lookup_error`` set (rendered as *Unknown* downstream).
* API unreachable (connection refused/DNS) → :class:`RuntimeUnavailableError`;
  in :meth:`list_deployments` any failing structural LIST call raises it too,
  because a silently missing namespace would hide gaps.

Privacy: only the handful of fields the catalog needs are extracted —
replica counts, container image references, rollout timestamps, restart
counts. Secrets, env vars, and full pod specs are never read or stored.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from datetime import datetime

import requests

from catalog_generator.config import ConfigError, KubernetesConfig
from catalog_generator.model import RuntimeState
from catalog_generator.sources.base import RuntimeSource, RuntimeUnavailableError

#: Per-request timeout in seconds (connect + read). Applied to every call.
DEFAULT_TIMEOUT = 10.0

#: Page size for LIST calls; the API returns a ``metadata.continue`` token
#: when there are more items, which we follow until exhausted.
LIST_PAGE_LIMIT = 200


class _NotFound(Exception):
    """Internal: the API returned 404 for a single GET/LIST."""


class _RequestFailed(Exception):
    """Internal: one request failed (HTTP error, timeout, bad payload)."""


class KubernetesRuntimeSource(RuntimeSource):
    """Runtime source backed by the real Kubernetes REST API."""

    def __init__(
        self,
        config: KubernetesConfig,
        *,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        if not config.api_endpoint:
            raise ConfigError(
                "kubernetes.api_endpoint is required for the live runtime source"
            )
        token = os.environ.get(config.token_env)
        if not token:
            raise ConfigError(
                f"environment variable {config.token_env!r} (kubernetes.token_env) is "
                "not set; the read-only ServiceAccount bearer token must come from "
                "the environment, never from a config file"
            )
        self._base = config.api_endpoint.rstrip("/")
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers["Authorization"] = f"Bearer {token}"
        # Cluster CA bundle path from config; default to standard verification.
        self._session.verify = config.ca_path if config.ca_path else True

    # --- RuntimeSource interface ---------------------------------------------

    def get_deployment(self, namespace: str, name: str) -> RuntimeState | None:
        url = f"{self._base}/apis/apps/v1/namespaces/{namespace}/deployments/{name}"
        try:
            deployment = self._get_json(url)
        except _NotFound:
            return None  # not deployed
        except _RequestFailed as exc:
            return RuntimeState(
                deployment_name=name, namespace=namespace, lookup_error=str(exc)
            )

        spec = _mapping(deployment.get("spec"))
        status = _mapping(deployment.get("status"))
        ready = _as_int(status.get("readyReplicas"))
        desired = _as_int(spec.get("replicas"))
        image_tags = _container_images(spec)

        # Rollout timestamp: Progressing condition and/or the newest owned
        # ReplicaSet's creationTimestamp — take the most recent available.
        rollout_candidates = []
        progressing = _progressing_last_update(status)
        if progressing is not None:
            rollout_candidates.append(progressing)

        selector = _label_selector(spec)
        uid = _mapping(deployment.get("metadata")).get("uid")
        try:
            if selector:
                rs_created = self._newest_owned_replicaset(namespace, selector, uid)
                if rs_created is not None:
                    rollout_candidates.append(rs_created)
                restarts = self._pod_restarts(namespace, selector)
            else:
                restarts = 0
        except (_NotFound, _RequestFailed) as exc:
            # The Deployment itself was readable; report the partial failure
            # instead of hiding it (→ Unknown downstream), keep what we have.
            return RuntimeState(
                deployment_name=name,
                namespace=namespace,
                ready_replicas=ready,
                desired_replicas=desired,
                image_tags=image_tags,
                last_rollout=max(rollout_candidates, default=None),
                lookup_error=str(exc),
            )

        return RuntimeState(
            deployment_name=name,
            namespace=namespace,
            ready_replicas=ready,
            desired_replicas=desired,
            image_tags=image_tags,
            last_rollout=max(rollout_candidates, default=None),
            recent_restarts=restarts,
        )

    def list_deployments(self, namespaces: Sequence[str]) -> list[RuntimeState]:
        states: list[RuntimeState] = []
        for namespace in namespaces:
            url = f"{self._base}/apis/apps/v1/namespaces/{namespace}/deployments"
            try:
                items = self._list_items(url)
            except _NotFound:
                continue  # namespace does not exist → no deployments in it
            except _RequestFailed as exc:
                # A failed structural LIST would silently hide a whole
                # namespace from the gap check — treat as unavailable.
                raise RuntimeUnavailableError(
                    f"cannot list deployments in namespace {namespace!r}: {exc}"
                ) from exc
            for item in items:
                metadata = _mapping(item.get("metadata"))
                spec = _mapping(item.get("spec"))
                status = _mapping(item.get("status"))
                states.append(
                    RuntimeState(
                        deployment_name=str(metadata.get("name", "")),
                        namespace=str(metadata.get("namespace", "") or namespace),
                        ready_replicas=_as_int(status.get("readyReplicas")),
                        desired_replicas=_as_int(spec.get("replicas")),
                        image_tags=_container_images(spec),
                        last_rollout=_progressing_last_update(status),
                    )
                )
        states.sort(key=lambda s: (s.namespace, s.deployment_name))
        return states

    # --- per-deployment detail lookups ----------------------------------------

    def _newest_owned_replicaset(
        self, namespace: str, selector: str, owner_uid: object
    ) -> datetime | None:
        """CreationTimestamp of the newest ReplicaSet owned by the Deployment."""
        url = f"{self._base}/apis/apps/v1/namespaces/{namespace}/replicasets"
        newest: datetime | None = None
        for item in self._list_items(url, label_selector=selector):
            metadata = _mapping(item.get("metadata"))
            if owner_uid is not None and not any(
                _mapping(ref).get("uid") == owner_uid
                for ref in metadata.get("ownerReferences") or []
            ):
                continue
            created = _parse_time(metadata.get("creationTimestamp"))
            if created is not None and (newest is None or created > newest):
                newest = created
        return newest

    def _pod_restarts(self, namespace: str, selector: str) -> int:
        """Sum container restartCounts over the pods matching the selector.

        Only ``status.containerStatuses[].restartCount`` is read — never pod
        specs, env vars, or anything else from the pod objects.
        """
        url = f"{self._base}/api/v1/namespaces/{namespace}/pods"
        restarts = 0
        for item in self._list_items(url, label_selector=selector):
            container_statuses = _mapping(item.get("status")).get("containerStatuses") or []
            for container in container_statuses:
                restarts += _as_int(_mapping(container).get("restartCount"))
        return restarts

    # --- HTTP plumbing ---------------------------------------------------------

    def _list_items(self, url: str, *, label_selector: str | None = None) -> list[dict]:
        """LIST with pagination: follow ``metadata.continue`` until exhausted."""
        items: list[dict] = []
        params: dict[str, object] = {"limit": LIST_PAGE_LIMIT}
        if label_selector:
            params["labelSelector"] = label_selector
        while True:
            data = self._get_json(url, params=params)
            page = data.get("items") or []
            if not isinstance(page, list):
                raise _RequestFailed(f"unexpected list payload from GET {url}")
            items.extend(item for item in page if isinstance(item, dict))
            continue_token = _mapping(data.get("metadata")).get("continue")
            if not continue_token:
                return items
            params = {**params, "continue": continue_token}

    def _get_json(self, url: str, params: dict | None = None) -> dict:
        """One GET with timeout; classify failures per the error contract."""
        try:
            # Order matters: ConnectTimeout subclasses both Timeout and
            # ConnectionError — a timeout counts as a per-lookup failure.
            response = self._session.get(url, params=params, timeout=self._timeout)
        except requests.exceptions.Timeout as exc:
            raise _RequestFailed(
                f"timeout after {self._timeout}s: GET {url}"
            ) from exc
        except requests.exceptions.ConnectionError as exc:
            raise RuntimeUnavailableError(
                f"Kubernetes API unreachable: GET {url}: {exc}"
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise _RequestFailed(f"GET {url} failed: {exc}") from exc

        if response.status_code == 404:
            raise _NotFound(url)
        if response.status_code >= 400:
            raise _RequestFailed(f"HTTP {response.status_code} for GET {url}")
        try:
            data = response.json()
        except ValueError as exc:
            raise _RequestFailed(f"invalid JSON from GET {url}: {exc}") from exc
        if not isinstance(data, dict):
            raise _RequestFailed(f"unexpected payload from GET {url}")
        return data


# --- pure field extractors (no I/O) -------------------------------------------


def _mapping(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _as_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _parse_time(value: object) -> datetime | None:
    """Parse a Kubernetes RFC 3339 timestamp (e.g. ``2026-06-12T10:02:00Z``)."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _container_images(spec: dict) -> tuple[str, ...]:
    """Full image references of the pod-template containers (deduplicated)."""
    template_spec = _mapping(_mapping(spec.get("template")).get("spec"))
    containers = template_spec.get("containers") or []
    images: list[str] = []
    for container in containers:
        image = _mapping(container).get("image")
        if isinstance(image, str) and image and image not in images:
            images.append(image)
    return tuple(images)


def _progressing_last_update(status: dict) -> datetime | None:
    """``lastUpdateTime`` of the ``Progressing`` deployment condition."""
    for condition in status.get("conditions") or []:
        condition = _mapping(condition)
        if condition.get("type") == "Progressing":
            return _parse_time(condition.get("lastUpdateTime"))
    return None


def _label_selector(spec: dict) -> str | None:
    """Encode ``spec.selector.matchLabels`` as a ``labelSelector`` string."""
    match_labels = _mapping(_mapping(spec.get("selector")).get("matchLabels"))
    if not match_labels:
        return None
    return ",".join(f"{key}={value}" for key, value in sorted(match_labels.items()))
