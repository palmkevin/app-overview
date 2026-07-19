"""Tests for the real Kubernetes runtime adapter (issue #7).

All HTTP is mocked by monkeypatching ``requests.Session`` inside the adapter
module — no live network calls ever. Hostnames and tokens are placeholders.
The annotation prefix plays no role here (deployment matching happens in the
aggregation layer); this adapter only speaks namespace + deployment name.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import requests

import catalog_generator.sources.kubernetes as k8s
from catalog_generator.config import ConfigError, KubernetesConfig
from catalog_generator.model import RuntimeState
from catalog_generator.sources.base import RuntimeUnavailableError

TOKEN_ENV = "CATALOG_K8S_TOKEN"
TOKEN_VALUE = "placeholder-test-token"  # never a real credential

CONFIG = KubernetesConfig(
    api_endpoint="https://k8s.example.invalid:6443",
    ca_path="/placeholder/ca.crt",
    namespaces=("apps", "tools"),
    token_env=TOKEN_ENV,
)


# --- fake HTTP layer -----------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: object = None) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload


class FakeSession:
    """Stands in for ``requests.Session``; routes GETs through a handler."""

    def __init__(self, handler) -> None:
        self.handler = handler
        self.headers: dict[str, str] = {}
        self.verify: object = True
        self.calls: list[tuple[str, dict | None, object]] = []

    def get(self, url: str, params: dict | None = None, timeout: object = None):
        self.calls.append((url, params, timeout))
        return self.handler(url, params)


def make_source(monkeypatch, handler) -> tuple[k8s.KubernetesRuntimeSource, FakeSession]:
    session = FakeSession(handler)
    monkeypatch.setenv(TOKEN_ENV, TOKEN_VALUE)
    monkeypatch.setattr(k8s.requests, "Session", lambda: session)
    return k8s.KubernetesRuntimeSource(CONFIG), session


# --- payload builders ------------------------------------------------------------


def deployment_payload(
    name: str = "time-tracker",
    namespace: str = "tools",
    *,
    ready: int = 2,
    desired: int = 2,
    images: tuple[str, ...] = ("registry.example/time-tracker:1.4.2",),
    progressing: str | None = "2026-06-12T10:02:00Z",
    uid: str = "uid-tt-1",
    labels: dict[str, str] | None = None,
) -> dict:
    conditions = (
        [{"type": "Progressing", "lastUpdateTime": progressing}] if progressing else []
    )
    return {
        "metadata": {"name": name, "namespace": namespace, "uid": uid},
        "spec": {
            "replicas": desired,
            "selector": {"matchLabels": labels or {"app": name}},
            "template": {
                "spec": {
                    "containers": [
                        {"name": f"c{i}", "image": img} for i, img in enumerate(images)
                    ],
                    # A secret-shaped field the adapter must never surface:
                    "volumes": [{"secret": {"secretName": "must-not-leak"}}],
                }
            },
        },
        "status": {"readyReplicas": ready, "conditions": conditions},
    }


def replicasets_payload(*, owner_uid: str = "uid-tt-1", timestamps=()) -> dict:
    return {
        "items": [
            {
                "metadata": {
                    "creationTimestamp": ts,
                    "ownerReferences": [{"uid": owner}],
                }
            }
            for ts, owner in timestamps
        ]
    }


def pods_payload(restart_counts=()) -> dict:
    return {
        "items": [
            {
                "status": {
                    "containerStatuses": [{"restartCount": n} for n in counts]
                }
            }
            for counts in restart_counts
        ]
    }


def route(deployment=None, replicasets=None, pods=None):
    """Standard handler for one deployment plus its RS/pod sub-lookups."""

    def handler(url: str, params: dict | None):
        if "/replicasets" in url:
            return replicasets or FakeResponse(200, {"items": []})
        if "/pods" in url:
            return pods or FakeResponse(200, {"items": []})
        if "/deployments/" in url:
            return deployment or FakeResponse(404)
        raise AssertionError(f"unexpected URL: {url}")

    return handler


# --- construction / auth ---------------------------------------------------------


def test_bearer_token_and_ca_come_from_env_and_config(monkeypatch):
    source, session = make_source(monkeypatch, route())
    assert session.headers["Authorization"] == f"Bearer {TOKEN_VALUE}"
    assert session.verify == CONFIG.ca_path
    assert source is not None


def test_missing_token_env_is_a_config_error(monkeypatch):
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    with pytest.raises(ConfigError, match=TOKEN_ENV):
        k8s.KubernetesRuntimeSource(CONFIG)


def test_missing_api_endpoint_is_a_config_error(monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, TOKEN_VALUE)
    with pytest.raises(ConfigError, match="api_endpoint"):
        k8s.KubernetesRuntimeSource(KubernetesConfig(api_endpoint=None, token_env=TOKEN_ENV))


# --- get_deployment: healthy / degraded / down -----------------------------------


def test_get_deployment_healthy(monkeypatch):
    handler = route(
        deployment=FakeResponse(200, deployment_payload(ready=2, desired=2)),
        replicasets=FakeResponse(
            200,
            replicasets_payload(
                timestamps=[
                    ("2026-06-10T08:00:00Z", "uid-tt-1"),
                    ("2026-06-12T10:00:00Z", "uid-tt-1"),
                    ("2026-06-13T00:00:00Z", "uid-other"),  # not ours → ignored
                ]
            ),
        ),
        pods=FakeResponse(200, pods_payload(restart_counts=[(0,), (0,)])),
    )
    source, session = make_source(monkeypatch, handler)
    state = source.get_deployment("tools", "time-tracker")

    assert isinstance(state, RuntimeState)
    assert state.lookup_error is None
    assert state.deployment_name == "time-tracker"
    assert state.namespace == "tools"
    assert state.ready_replicas == state.desired_replicas == 2
    assert state.image_tags == ("registry.example/time-tracker:1.4.2",)
    assert state.recent_restarts == 0
    # Progressing lastUpdateTime (10:02) is newer than the owned RS (10:00);
    # the foreign-owned RS from 06-13 must not win.
    assert state.last_rollout == datetime(2026, 6, 12, 10, 2, tzinfo=UTC)
    # Sub-lookups must use the deployment's selector, and every call times out.
    selector_params = [p for (u, p, _) in session.calls if p and "labelSelector" in p]
    assert all(p["labelSelector"] == "app=time-tracker" for p in selector_params)
    assert len(selector_params) == 2
    assert all(t == k8s.DEFAULT_TIMEOUT for (_, _, t) in session.calls)


def test_get_deployment_newest_owned_replicaset_wins_when_newer(monkeypatch):
    handler = route(
        deployment=FakeResponse(200, deployment_payload(progressing="2026-06-12T10:02:00Z")),
        replicasets=FakeResponse(
            200, replicasets_payload(timestamps=[("2026-07-01T12:00:00Z", "uid-tt-1")])
        ),
    )
    source, _ = make_source(monkeypatch, handler)
    state = source.get_deployment("tools", "time-tracker")
    assert state.last_rollout == datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


def test_get_deployment_degraded_counts_pod_restarts(monkeypatch):
    handler = route(
        deployment=FakeResponse(200, deployment_payload(ready=1, desired=3)),
        pods=FakeResponse(200, pods_payload(restart_counts=[(2, 1), (4,)])),
    )
    source, _ = make_source(monkeypatch, handler)
    state = source.get_deployment("tools", "time-tracker")
    assert state.ready_replicas == 1
    assert state.desired_replicas == 3
    assert state.recent_restarts == 7  # 2 + 1 + 4 across matched pods
    assert state.lookup_error is None


def test_get_deployment_down(monkeypatch):
    handler = route(deployment=FakeResponse(200, deployment_payload(ready=0, desired=2)))
    source, _ = make_source(monkeypatch, handler)
    state = source.get_deployment("tools", "time-tracker")
    assert state.ready_replicas == 0
    assert state.desired_replicas == 2
    assert state.lookup_error is None


# --- get_deployment: not found / errors -------------------------------------------


def test_get_deployment_404_means_not_deployed(monkeypatch):
    source, _ = make_source(monkeypatch, route(deployment=FakeResponse(404)))
    assert source.get_deployment("apps", "hr-portal") is None


def test_get_deployment_http_500_sets_lookup_error(monkeypatch):
    source, _ = make_source(monkeypatch, route(deployment=FakeResponse(500)))
    state = source.get_deployment("tools", "time-tracker")
    assert isinstance(state, RuntimeState)
    assert state.deployment_name == "time-tracker"
    assert state.namespace == "tools"
    assert "500" in state.lookup_error
    assert TOKEN_VALUE not in state.lookup_error  # never leak the token


def test_get_deployment_timeout_sets_lookup_error(monkeypatch):
    def handler(url, params):
        raise requests.exceptions.ReadTimeout("read timed out")

    source, _ = make_source(monkeypatch, handler)
    state = source.get_deployment("tools", "time-tracker")
    assert state.lookup_error is not None
    assert "timeout" in state.lookup_error.lower()


def test_get_deployment_sub_lookup_failure_keeps_partial_data(monkeypatch):
    def handler(url, params):
        if "/pods" in url:
            return FakeResponse(500)
        if "/replicasets" in url:
            return FakeResponse(200, {"items": []})
        return FakeResponse(200, deployment_payload(ready=2, desired=2))

    source, _ = make_source(monkeypatch, handler)
    state = source.get_deployment("tools", "time-tracker")
    assert state.lookup_error is not None and "500" in state.lookup_error
    assert state.ready_replicas == 2  # partial data preserved, error shown
    assert state.image_tags == ("registry.example/time-tracker:1.4.2",)


def test_connection_error_raises_runtime_unavailable(monkeypatch):
    def handler(url, params):
        raise requests.exceptions.ConnectionError("connection refused")

    source, _ = make_source(monkeypatch, handler)
    with pytest.raises(RuntimeUnavailableError):
        source.get_deployment("tools", "time-tracker")
    with pytest.raises(RuntimeUnavailableError):
        source.list_deployments(["tools"])


# --- list_deployments ---------------------------------------------------------------


def _list_item(name: str, namespace: str, ready: int = 1, desired: int = 1) -> dict:
    return deployment_payload(name, namespace, ready=ready, desired=desired, uid=f"uid-{name}")


def test_list_deployments_lists_all_namespaces_sorted(monkeypatch):
    def handler(url, params):
        if url.endswith("/namespaces/apps/deployments"):
            return FakeResponse(200, {"items": [_list_item("zeta", "apps")]})
        if url.endswith("/namespaces/tools/deployments"):
            return FakeResponse(
                200,
                {"items": [_list_item("time-tracker", "tools"), _list_item("alpha", "tools")]},
            )
        raise AssertionError(f"unexpected URL: {url}")

    source, session = make_source(monkeypatch, handler)
    states = source.list_deployments(["apps", "tools"])
    assert [(s.namespace, s.deployment_name) for s in states] == [
        ("apps", "zeta"),
        ("tools", "alpha"),
        ("tools", "time-tracker"),
    ]
    assert all(s.image_tags for s in states)
    # LIST-only: no per-deployment RS/pod calls in list mode.
    assert all("/replicasets" not in u and "/pods" not in u for (u, _, _) in session.calls)


def test_list_deployments_follows_continue_token(monkeypatch):
    def handler(url, params):
        assert params["limit"] == k8s.LIST_PAGE_LIMIT
        if params.get("continue") == "page-2":
            return FakeResponse(200, {"items": [_list_item("second", "tools")]})
        assert "continue" not in params
        return FakeResponse(
            200,
            {"items": [_list_item("first", "tools")], "metadata": {"continue": "page-2"}},
        )

    source, session = make_source(monkeypatch, handler)
    states = source.list_deployments(["tools"])
    assert [s.deployment_name for s in states] == ["first", "second"]
    assert len(session.calls) == 2
    assert session.calls[1][1]["continue"] == "page-2"


def test_list_deployments_skips_missing_namespace(monkeypatch):
    def handler(url, params):
        if "/namespaces/gone/" in url:
            return FakeResponse(404)
        return FakeResponse(200, {"items": [_list_item("app-x", "tools")]})

    source, _ = make_source(monkeypatch, handler)
    states = source.list_deployments(["gone", "tools"])
    assert [s.deployment_name for s in states] == ["app-x"]


def test_list_deployments_http_error_raises_runtime_unavailable(monkeypatch):
    source, _ = make_source(monkeypatch, lambda url, params: FakeResponse(503))
    with pytest.raises(RuntimeUnavailableError, match="tools"):
        source.list_deployments(["tools"])


# --- privacy: never collect secrets/env/full pod specs ------------------------------


def test_states_never_carry_secret_shaped_data(monkeypatch):
    pod_with_secret_env = {
        "status": {"containerStatuses": [{"restartCount": 1}]},
        "spec": {  # the adapter must never read/propagate this
            "containers": [
                {"env": [{"name": "SECRET_TOKEN", "value": "must-not-leak"}]}
            ]
        },
    }
    handler = route(
        deployment=FakeResponse(200, deployment_payload()),
        pods=FakeResponse(200, {"items": [pod_with_secret_env]}),
    )
    source, _ = make_source(monkeypatch, handler)
    state = source.get_deployment("tools", "time-tracker")
    assert state.recent_restarts == 1
    assert "must-not-leak" not in repr(state)
    assert TOKEN_VALUE not in repr(state)
