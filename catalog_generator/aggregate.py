"""Aggregation: join repo metadata with runtime state, derive status and gaps.

This is the core of the generator (PRD §6.2): it consumes a
:class:`~catalog_generator.sources.base.RepoSource` and a
:class:`~catalog_generator.sources.base.RuntimeSource` and produces the fully
aggregated :class:`~catalog_generator.model.CatalogData` handed to the
renderer, plus a stable ``catalog.json`` serialization for future tooling.

Rules implemented here (all pure functions, no I/O beyond the sources):

* Manifests are parsed/validated exclusively via
  :mod:`catalog_generator.validation` with the *configured* annotation prefix.
  A broken or invalid manifest never aborts the run — the app entry is kept
  with ``validation_errors`` attached and status ``UNKNOWN``.
* Deployment matching happens STRICTLY via the declared
  ``{prefix}/k8s-deployment`` + ``{prefix}/k8s-namespace`` annotations
  (``Config.annotation_key``); never by name-guessing.
* Status per PRD §6.2: HEALTHY (ready == desired >= 1), DEGRADED
  (0 < ready < desired, or ready == desired but more than
  ``RECENT_RESTART_THRESHOLD`` recent restarts), DOWN (deployment found,
  ready == 0), NOT_DEPLOYED (declared deployment not found, and also valid
  manifests without k8s annotations — only ``experimental`` may omit them),
  UNKNOWN (runtime unavailable, per-app lookup error, or invalid manifest).
* If the runtime API is entirely unreachable
  (:class:`~catalog_generator.sources.base.RuntimeUnavailableError`), the
  catalog is still produced from repo data: ``runtime_available=False``,
  ``runtime_unavailable_since`` set (the error's ``since``, falling back to
  the injected ``generated_at``), runtime-dependent statuses become UNKNOWN
  and the runtime-dependent gap lists stay empty.
* The deployed-but-uncataloged scan covers ``config.kubernetes.namespaces``;
  when that list is empty (typical testrun config) it falls back to the
  namespaces declared by the cataloged apps themselves. Deployments whose
  name fully matches any ``config.ignore_deployments`` pattern (a regex; an
  explicit name is just a regex without metacharacters) are excluded.

``generated_at`` is always injected by the caller — this module never reads
the clock.

catalog.json schema (``to_json``)
---------------------------------

Keys are emitted sorted (deterministic output); all timestamps are ISO-8601
strings or ``null``. The future pipeline gate reads ``coverage``.

::

    {
      "schema_version": 1,
      "generated_at": "2026-07-15T12:00:00+00:00",
      "runtime_available": true,
      "runtime_unavailable_since": null,        # ISO string when false
      "coverage": {"cataloged": 11, "total": 12, "percent": 91.7},
      "apps": [                                  # sorted by repo name
        {
          "name": "time-tracker",               # metadata.name, falls back
                                                # to the repo name
          "description": "...",                 # or null
          "owner": "jane.doe",                  # or null
          "type": "website",                    # or null
          "lifecycle": "production",            # or null
          "tags": ["react"],
          "links": [{"url": "...", "title": "..."}],
          "annotations": {"<prefix>/k8s-deployment": "..."},
          "repo": {
            "name": "time-tracker",
            "url": "https://...",
            "default_branch": "main",
            "last_commit_date": "2026-07-01T09:00:00+00:00",  # or null
            "has_readme": true
          },
          "status": "healthy",                   # Status enum value
          "runtime": {                           # or null (never a pod spec)
            "namespace": "tools",
            "deployment": "time-tracker",
            "ready_replicas": 2,
            "desired_replicas": 2,
            "image_tags": ["..."],
            "last_rollout": "2026-06-12T10:02:00+00:00",  # or null
            "recent_restarts": 0,
            "lookup_error": null
          },
          "validation_errors": []
        }
      ],
      "gaps": {
        "deployed_uncataloged": [
          {"namespace": "tools", "name": "status-page",
           "ready_replicas": 1, "desired_replicas": 1, "image_tags": ["..."]}
        ],
        "cataloged_not_deployed": ["hr-portal"],       # app names
        "invalid_metadata": [
          {"repo": "legacy-wiki", "errors": ["<file>: YAML parse error: ..."]}
        ]
      }
    }
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime

from catalog_generator.config import Config
from catalog_generator.model import (
    AppEntry,
    CatalogData,
    Coverage,
    Gaps,
    Link,
    Manifest,
    Repo,
    RuntimeState,
    Status,
)
from catalog_generator.sources.base import (
    RepoSource,
    RuntimeSource,
    RuntimeUnavailableError,
)
from catalog_generator.validation import (
    check_name_uniqueness,
    parse_manifest,
    validate_manifest,
)

#: catalog.json schema version, bumped on breaking shape changes.
JSON_SCHEMA_VERSION = 1

#: "Recent restarts" above this count degrade an otherwise-healthy app
#: (PRD §6.2: "Deployment found, 0 < ready < desired, or recent restarts").
RECENT_RESTART_THRESHOLD = 3


@dataclass
class _Draft:
    """Mutable per-repo working record used while aggregating."""

    repo: Repo
    manifest: Manifest | None
    errors: list[str] = field(default_factory=list)
    #: (namespace, deployment) declared via the configured annotations.
    declared: tuple[str, str] | None = None


def aggregate(
    repo_source: RepoSource,
    runtime_source: RuntimeSource,
    config: Config,
    generated_at: datetime,
) -> CatalogData:
    """Produce the fully aggregated :class:`CatalogData` from the two sources."""
    repos = sorted(repo_source.list_repos(), key=lambda r: r.name)

    drafts: list[_Draft] = []
    parsed_dicts: list[dict] = []
    for repo in repos:
        text = repo_source.fetch_manifest(repo)
        if text is None:
            continue  # uncataloged repo: counts in coverage only
        data, parse_errors = parse_manifest(text)
        if data is None:
            drafts.append(_Draft(repo, None, [str(e) for e in parse_errors]))
            continue
        parsed_dicts.append(data)
        manifest = _build_manifest(data)
        drafts.append(
            _Draft(
                repo,
                manifest,
                [str(e) for e in validate_manifest(data, config.annotation_prefix)],
                _declared_deployment(manifest, config),
            )
        )

    _attach_uniqueness_errors(drafts, parsed_dicts)

    runtime_available = True
    unavailable_since: datetime | None = None
    lookups: dict[tuple[str, str], RuntimeState | None] = {}
    observed: list[RuntimeState] = []
    try:
        for draft in drafts:
            if draft.declared is not None and draft.declared not in lookups:
                namespace, name = draft.declared
                lookups[draft.declared] = runtime_source.get_deployment(namespace, name)
        scan = _scan_namespaces(config, drafts)
        if scan:
            observed = runtime_source.list_deployments(scan)
    except RuntimeUnavailableError as exc:
        runtime_available = False
        unavailable_since = exc.since or generated_at
        lookups = {}
        observed = []

    entries = tuple(
        AppEntry(
            repo=draft.repo,
            manifest=draft.manifest,
            runtime=lookups.get(draft.declared) if draft.declared else None,
            status=_derive_status(draft, runtime_available, lookups),
            validation_errors=tuple(draft.errors),
        )
        for draft in drafts
    )

    claimed = {draft.declared for draft in drafts if draft.declared is not None}
    gaps = Gaps(
        deployed_uncataloged=tuple(
            dep
            for dep in observed
            if (dep.namespace, dep.deployment_name) not in claimed
            and not _is_ignored(dep.deployment_name, config.ignore_deployments)
        ),
        cataloged_not_deployed=tuple(
            entry
            for entry in entries
            if entry.status is Status.NOT_DEPLOYED
            and entry.manifest is not None
            and entry.manifest.lifecycle != "experimental"
        ),
        invalid_metadata=tuple(entry for entry in entries if entry.validation_errors),
    )

    return CatalogData(
        entries=entries,
        gaps=gaps,
        coverage=Coverage(cataloged=len(drafts), total=len(repos)),
        generated_at=generated_at,
        runtime_available=runtime_available,
        runtime_unavailable_since=unavailable_since,
    )


def to_json(catalog: CatalogData) -> str:
    """Serialize :class:`CatalogData` to the documented catalog.json shape.

    Deterministic: keys are sorted and all values derive from the (already
    deterministic) catalog data. See the module docstring for the schema.
    """
    payload = {
        "schema_version": JSON_SCHEMA_VERSION,
        "generated_at": catalog.generated_at.isoformat(),
        "runtime_available": catalog.runtime_available,
        "runtime_unavailable_since": _iso(catalog.runtime_unavailable_since),
        "coverage": {
            "cataloged": catalog.coverage.cataloged,
            "total": catalog.coverage.total,
            "percent": round(catalog.coverage.percent, 1),
        },
        "apps": [_app_to_json(entry) for entry in catalog.entries],
        "gaps": {
            "deployed_uncataloged": [
                {
                    "namespace": dep.namespace,
                    "name": dep.deployment_name,
                    "ready_replicas": dep.ready_replicas,
                    "desired_replicas": dep.desired_replicas,
                    "image_tags": list(dep.image_tags),
                }
                for dep in catalog.gaps.deployed_uncataloged
            ],
            "cataloged_not_deployed": [
                _entry_name(entry) for entry in catalog.gaps.cataloged_not_deployed
            ],
            "invalid_metadata": [
                {"repo": entry.repo.name, "errors": list(entry.validation_errors)}
                for entry in catalog.gaps.invalid_metadata
            ],
        },
    }
    return json.dumps(payload, sort_keys=True, indent=2)


# --- helpers -----------------------------------------------------------------


def _build_manifest(data: dict) -> Manifest:
    """Build the display model from a parsed manifest dict, tolerantly.

    Whether the content is *valid* is decided solely by
    ``catalog_generator.validation``; this only extracts what is usable.
    """
    metadata = data.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    spec = data.get("spec")
    spec = spec if isinstance(spec, dict) else {}

    raw_annotations = metadata.get("annotations")
    annotations = (
        {k: v for k, v in raw_annotations.items() if isinstance(k, str) and isinstance(v, str)}
        if isinstance(raw_annotations, dict)
        else {}
    )

    raw_links = metadata.get("links")
    links = tuple(
        Link(url=item["url"], title=_opt_str(item.get("title")))
        for item in (raw_links if isinstance(raw_links, list) else ())
        if isinstance(item, dict) and isinstance(item.get("url"), str)
    )

    raw_tags = metadata.get("tags")
    tags = tuple(
        tag for tag in (raw_tags if isinstance(raw_tags, list) else ()) if isinstance(tag, str)
    )

    return Manifest(
        name=_opt_str(metadata.get("name")),
        description=_opt_str(metadata.get("description")),
        annotations=annotations,
        links=links,
        tags=tags,
        type=_opt_str(spec.get("type")),
        lifecycle=_opt_str(spec.get("lifecycle")),
        owner=_opt_str(spec.get("owner")),
    )


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _declared_deployment(manifest: Manifest, config: Config) -> tuple[str, str] | None:
    """(namespace, deployment) declared via the *configured* annotation keys."""
    namespace = _opt_str(manifest.annotations.get(config.annotation_key("k8s-namespace")))
    deployment = _opt_str(manifest.annotations.get(config.annotation_key("k8s-deployment")))
    if namespace and deployment:
        return namespace, deployment
    return None


def _attach_uniqueness_errors(drafts: list[_Draft], parsed_dicts: list[dict]) -> None:
    """Attach cross-repo name-uniqueness errors to every affected entry.

    The rule itself lives in ``validation.check_name_uniqueness``; here we
    only route each returned error to the entries carrying that name (the
    error message embeds the duplicated name in ``repr`` form).
    """
    for error in check_name_uniqueness(parsed_dicts):
        for draft in drafts:
            name = draft.manifest.name if draft.manifest else None
            if name is not None and f"name {name!r} " in error.message:
                draft.errors.append(str(error))


def _scan_namespaces(config: Config, drafts: list[_Draft]) -> tuple[str, ...]:
    """Namespaces to scan for uncataloged deployments.

    ``config.kubernetes.namespaces`` when set; otherwise fall back to the
    namespaces declared by the cataloged apps (sorted, deterministic).
    """
    if config.kubernetes.namespaces:
        return config.kubernetes.namespaces
    return tuple(sorted({draft.declared[0] for draft in drafts if draft.declared is not None}))


def _derive_status(
    draft: _Draft,
    runtime_available: bool,
    lookups: dict[tuple[str, str], RuntimeState | None],
) -> Status:
    """Derive the per-app status per the PRD §6.2 status table."""
    if draft.errors:
        return Status.UNKNOWN  # invalid/unparseable manifest
    if draft.declared is None:
        # Valid manifest without k8s annotations — only allowed for
        # 'experimental' (validation enforces that): nothing is deployed.
        return Status.NOT_DEPLOYED
    if not runtime_available:
        return Status.UNKNOWN
    runtime = lookups.get(draft.declared)
    if runtime is None:
        return Status.NOT_DEPLOYED
    if runtime.lookup_error:
        return Status.UNKNOWN
    if runtime.ready_replicas == 0:
        return Status.DOWN
    if runtime.ready_replicas == runtime.desired_replicas:
        if runtime.recent_restarts > RECENT_RESTART_THRESHOLD:
            return Status.DEGRADED
        return Status.HEALTHY
    return Status.DEGRADED  # 0 < ready != desired (rollout / partial outage)


def _is_ignored(deployment_name: str, patterns: tuple[str, ...]) -> bool:
    """True when the name fully matches any ignore pattern (regex or literal)."""
    return any(re.fullmatch(pattern, deployment_name) for pattern in patterns)


def _entry_name(entry: AppEntry) -> str:
    """Display name of an entry: manifest name, falling back to the repo name."""
    if entry.manifest is not None and entry.manifest.name:
        return entry.manifest.name
    return entry.repo.name


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _app_to_json(entry: AppEntry) -> dict:
    manifest = entry.manifest
    runtime = entry.runtime
    return {
        "name": _entry_name(entry),
        "description": manifest.description if manifest else None,
        "owner": manifest.owner if manifest else None,
        "type": manifest.type if manifest else None,
        "lifecycle": manifest.lifecycle if manifest else None,
        "tags": list(manifest.tags) if manifest else [],
        "links": [
            {"url": link.url, "title": link.title} for link in (manifest.links if manifest else ())
        ],
        "annotations": dict(manifest.annotations) if manifest else {},
        "repo": {
            "name": entry.repo.name,
            "url": entry.repo.url,
            "default_branch": entry.repo.default_branch,
            "last_commit_date": _iso(entry.repo.last_commit_date),
            "has_readme": entry.repo.has_readme,
        },
        "status": entry.status.value,
        "runtime": {
            "namespace": runtime.namespace,
            "deployment": runtime.deployment_name,
            "ready_replicas": runtime.ready_replicas,
            "desired_replicas": runtime.desired_replicas,
            "image_tags": list(runtime.image_tags),
            "last_rollout": _iso(runtime.last_rollout),
            "recent_restarts": runtime.recent_restarts,
            "lookup_error": runtime.lookup_error,
        }
        if runtime is not None
        else None,
        "validation_errors": list(entry.validation_errors),
    }
