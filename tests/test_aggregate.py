"""Tests for the aggregation engine (issue #8).

Runs the real fake adapters over the real ``testdata/`` fixtures, plus small
in-memory sources for the cases the shared fixtures don't cover (non-default
annotation prefix, duplicate names, lookup errors, restart threshold).

The annotation prefix is never hardcoded: fixture-based tests use the default
``Config`` (whose prefix matches the fixtures'), and prefix-sensitive tests
build their manifests from ``config.annotation_key(...)``.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from catalog_generator.aggregate import RECENT_RESTART_THRESHOLD, aggregate, to_json
from catalog_generator.config import Config, KubernetesConfig
from catalog_generator.model import CatalogData, Repo, RuntimeState, Status
from catalog_generator.sources.base import (
    RepoSource,
    RuntimeSource,
    RuntimeUnavailableError,
)
from catalog_generator.sources.fake import FakeRepoSource, FakeRuntimeSource

TESTDATA = Path(__file__).resolve().parent.parent / "testdata"
GENERATED_AT = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)

CONFIG = Config(testdata_dir=str(TESTDATA))
CONFIG_WITH_IGNORES = Config(
    testdata_dir=str(TESTDATA),
    ignore_deployments=("nginx-ingress", "cert-.*"),  # explicit name + regex
)


# --- small in-memory sources for cases the shared fixtures don't cover -------


class InMemoryRepoSource(RepoSource):
    """Repo source over a ``{repo_name: manifest_text_or_None}`` mapping."""

    def __init__(self, manifests: dict[str, str | None]) -> None:
        self._manifests = manifests

    def list_repos(self) -> list[Repo]:
        return [
            Repo(name=name, url=f"https://git.example.test/{name}", default_branch="main")
            for name in sorted(self._manifests)
        ]

    def fetch_manifest(self, repo: Repo) -> str | None:
        return self._manifests[repo.name]


class InMemoryRuntimeSource(RuntimeSource):
    """Runtime source over a fixed list of deployments."""

    def __init__(
        self,
        deployments: Sequence[RuntimeState] = (),
        unavailable: bool = False,
        since: datetime | None = None,
    ) -> None:
        self._deployments = list(deployments)
        self._unavailable = unavailable
        self._since = since

    def _check(self) -> None:
        if self._unavailable:
            raise RuntimeUnavailableError("runtime API unreachable", since=self._since)

    def get_deployment(self, namespace: str, name: str) -> RuntimeState | None:
        self._check()
        for deployment in self._deployments:
            if deployment.namespace == namespace and deployment.deployment_name == name:
                return deployment
        return None

    def list_deployments(self, namespaces: Sequence[str]) -> list[RuntimeState]:
        self._check()
        wanted = set(namespaces)
        return [d for d in self._deployments if d.namespace in wanted]


def manifest_text(
    name: str,
    config: Config,
    deployment: str | None = None,
    namespace: str | None = None,
    lifecycle: str = "production",
) -> str:
    """A valid manifest using the *configured* annotation prefix."""
    annotations = ""
    if deployment and namespace:
        annotations = (
            "  annotations:\n"
            f"    {config.annotation_key('k8s-deployment')}: {deployment}\n"
            f"    {config.annotation_key('k8s-namespace')}: {namespace}\n"
        )
    return (
        "apiVersion: backstage.io/v1alpha1\n"
        "kind: Component\n"
        "metadata:\n"
        f"  name: {name}\n"
        f"  description: Test app {name}\n"
        f"{annotations}"
        "spec:\n"
        "  type: service\n"
        f"  lifecycle: {lifecycle}\n"
        "  owner: dev.one\n"
    )


def run_fixtures(config: Config = CONFIG, scenario: str = "default") -> CatalogData:
    return aggregate(
        FakeRepoSource(config.testdata_dir),
        FakeRuntimeSource(config.testdata_dir, scenario=scenario),
        config,
        GENERATED_AT,
    )


def statuses_by_repo(catalog: CatalogData) -> dict[str, Status]:
    return {entry.repo.name: entry.status for entry in catalog.entries}


# --- statuses (every status appears in the fixture set) ----------------------


def test_every_status_appears_in_fixture_catalog():
    statuses = statuses_by_repo(run_fixtures())
    assert statuses == {
        "app-catalog": Status.HEALTHY,  # dogfood entry (#15)
        "asset-inventory": Status.HEALTHY,
        "holiday-planner": Status.HEALTHY,
        "invoice-portal": Status.HEALTHY,
        "meeting-room-booker": Status.HEALTHY,
        "time-tracker": Status.HEALTHY,
        "pdf-renderer": Status.DEGRADED,  # 0 < ready < desired
        "ldap-sync": Status.DOWN,  # ready == 0
        "chat-prototype": Status.NOT_DEPLOYED,  # experimental, no annotations
        "hr-portal": Status.NOT_DEPLOYED,  # declared deployment missing
        "legacy-wiki": Status.UNKNOWN,  # YAML parse error
        "metrics-dashboard": Status.UNKNOWN,  # validation error
    }
    assert set(statuses.values()) == set(Status)


def test_healthy_entry_carries_runtime_state():
    catalog = run_fixtures()
    entry = next(e for e in catalog.entries if e.repo.name == "time-tracker")
    assert entry.runtime is not None
    assert entry.runtime.ready_replicas == entry.runtime.desired_replicas == 2
    assert entry.validation_errors == ()


def test_restart_threshold_degrades_fully_ready_app():
    config = Config(kubernetes=KubernetesConfig(namespaces=("prod",)))
    repo_source = InMemoryRepoSource(
        {"flappy": manifest_text("flappy", config, deployment="flappy", namespace="prod")}
    )

    def run(restarts: int) -> Status:
        runtime_source = InMemoryRuntimeSource(
            [
                RuntimeState(
                    deployment_name="flappy",
                    namespace="prod",
                    ready_replicas=2,
                    desired_replicas=2,
                    recent_restarts=restarts,
                )
            ]
        )
        catalog = aggregate(repo_source, runtime_source, config, GENERATED_AT)
        return catalog.entries[0].status

    assert run(RECENT_RESTART_THRESHOLD) is Status.HEALTHY
    assert run(RECENT_RESTART_THRESHOLD + 1) is Status.DEGRADED


def test_per_app_lookup_error_yields_unknown():
    config = Config(kubernetes=KubernetesConfig(namespaces=("prod",)))
    repo_source = InMemoryRepoSource(
        {"oops": manifest_text("oops", config, deployment="oops", namespace="prod")}
    )
    runtime_source = InMemoryRuntimeSource(
        [
            RuntimeState(
                deployment_name="oops",
                namespace="prod",
                lookup_error="API error 500 for this deployment",
            )
        ]
    )
    catalog = aggregate(repo_source, runtime_source, config, GENERATED_AT)
    assert catalog.entries[0].status is Status.UNKNOWN
    assert catalog.runtime_available is True


# --- broken manifests never break the run ------------------------------------


def test_invalid_yaml_keeps_entry_and_does_not_affect_other_apps():
    catalog = run_fixtures()
    assert len(catalog.entries) == 12  # all cataloged repos present

    wiki = next(e for e in catalog.entries if e.repo.name == "legacy-wiki")
    assert wiki.status is Status.UNKNOWN
    assert wiki.manifest is None
    assert any("YAML parse error" in message for message in wiki.validation_errors)

    # The neighbours are untouched by the broken manifest.
    assert statuses_by_repo(catalog)["time-tracker"] is Status.HEALTHY


def test_validation_failure_keeps_entry_with_messages():
    catalog = run_fixtures()
    entry = next(e for e in catalog.entries if e.repo.name == "metrics-dashboard")
    assert entry.status is Status.UNKNOWN
    assert any("spec.owner" in message for message in entry.validation_errors)
    assert entry.manifest is not None  # still parsed for display


def test_duplicate_names_flag_all_affected_entries():
    config = Config()
    repo_source = InMemoryRepoSource(
        {
            "repo-a": manifest_text("same-name", config, lifecycle="experimental"),
            "repo-b": manifest_text("same-name", config, lifecycle="experimental"),
            "repo-c": manifest_text("other-name", config, lifecycle="experimental"),
        }
    )
    catalog = aggregate(repo_source, InMemoryRuntimeSource(), config, GENERATED_AT)
    flagged = {e.repo.name for e in catalog.entries if e.validation_errors}
    assert flagged == {"repo-a", "repo-b"}
    assert all(
        any("unique" in message for message in e.validation_errors)
        for e in catalog.entries
        if e.repo.name in flagged
    )
    assert {e.repo.name for e in catalog.gaps.invalid_metadata} == flagged


# --- gaps ---------------------------------------------------------------------


def test_gap_deployed_uncataloged_without_ignore_list():
    catalog = run_fixtures(CONFIG)
    names = {(d.namespace, d.deployment_name) for d in catalog.gaps.deployed_uncataloged}
    assert names == {
        ("tools", "status-page"),
        ("tools", "nginx-ingress"),
        ("apps", "cert-manager"),
    }


def test_gap_ignore_list_filters_explicit_name_and_regex():
    catalog = run_fixtures(CONFIG_WITH_IGNORES)
    names = {d.deployment_name for d in catalog.gaps.deployed_uncataloged}
    assert names == {"status-page"}  # nginx-ingress (literal) + cert-.* (regex) ignored


def test_ignore_list_matches_full_name_only():
    # A pattern must match the whole deployment name — "status" alone must
    # not swallow "status-page".
    catalog = run_fixtures(Config(testdata_dir=str(TESTDATA), ignore_deployments=("status",)))
    names = {d.deployment_name for d in catalog.gaps.deployed_uncataloged}
    assert "status-page" in names


def test_scan_namespaces_from_config_restrict_the_gap_check():
    config = Config(
        testdata_dir=str(TESTDATA),
        kubernetes=KubernetesConfig(namespaces=("apps",)),
    )
    catalog = run_fixtures(config)
    names = {d.deployment_name for d in catalog.gaps.deployed_uncataloged}
    assert names == {"cert-manager"}  # tools namespace not scanned


def test_gap_cataloged_not_deployed_excludes_experimental():
    catalog = run_fixtures()
    assert [e.repo.name for e in catalog.gaps.cataloged_not_deployed] == ["hr-portal"]
    # chat-prototype is NOT_DEPLOYED but experimental -> no gap entry.
    assert statuses_by_repo(catalog)["chat-prototype"] is Status.NOT_DEPLOYED


def test_gap_invalid_metadata_lists_both_broken_repos():
    catalog = run_fixtures()
    assert {e.repo.name for e in catalog.gaps.invalid_metadata} == {
        "legacy-wiki",
        "metrics-dashboard",
    }
    assert all(e.validation_errors for e in catalog.gaps.invalid_metadata)


# --- coverage -----------------------------------------------------------------


def test_coverage_counts_repos_without_manifest_as_uncataloged():
    catalog = run_fixtures()
    assert catalog.coverage.cataloged == 12  # ops-scripts has no manifest
    assert catalog.coverage.total == 13
    assert catalog.coverage.percent == pytest.approx(100 * 12 / 13)


# --- runtime unavailable --------------------------------------------------------


def test_runtime_unavailable_still_aggregates_from_repo_data():
    catalog = run_fixtures(scenario="runtime-unavailable")
    assert catalog.runtime_available is False
    assert catalog.runtime_unavailable_since == datetime(
        2026, 7, 1, 5, 30, tzinfo=UTC
    )
    assert len(catalog.entries) == 12

    statuses = statuses_by_repo(catalog)
    # Every runtime-dependent status collapses to UNKNOWN...
    for name in ("time-tracker", "pdf-renderer", "ldap-sync", "hr-portal"):
        assert statuses[name] is Status.UNKNOWN
    # ...while runtime-independent facts survive: no declared deployment
    # means not deployed, and invalid metadata stays flagged.
    assert statuses["chat-prototype"] is Status.NOT_DEPLOYED
    assert {e.repo.name for e in catalog.gaps.invalid_metadata} == {
        "legacy-wiki",
        "metrics-dashboard",
    }
    # Runtime-dependent gaps cannot be computed.
    assert catalog.gaps.deployed_uncataloged == ()
    assert catalog.gaps.cataloged_not_deployed == ()
    # Coverage is repo-only data and is unaffected.
    assert catalog.coverage.cataloged == 12


def test_runtime_unavailable_since_falls_back_to_generated_at():
    config = Config(kubernetes=KubernetesConfig(namespaces=("prod",)))
    repo_source = InMemoryRepoSource(
        {"app": manifest_text("app", config, deployment="app", namespace="prod")}
    )
    runtime_source = InMemoryRuntimeSource(unavailable=True, since=None)
    catalog = aggregate(repo_source, runtime_source, config, GENERATED_AT)
    assert catalog.runtime_available is False
    assert catalog.runtime_unavailable_since == GENERATED_AT


# --- PRD acceptance criterion 1 -------------------------------------------------


def test_prd_acceptance_criterion_1():
    """3 repos (valid manifest, invalid YAML, no manifest) + 1 unmatched
    deployment => 1 healthy app, 1 invalid-metadata entry with the error,
    1 uncataloged repo counted in coverage, 1 deployed-but-uncataloged gap."""
    config = Config(kubernetes=KubernetesConfig(namespaces=("prod",)))
    repo_source = InMemoryRepoSource(
        {
            "good-app": manifest_text("good-app", config, deployment="good-app", namespace="prod"),
            "broken-app": "metadata: [unclosed",
            "no-manifest": None,
        }
    )
    runtime_source = InMemoryRuntimeSource(
        [
            RuntimeState(
                deployment_name="good-app", namespace="prod", ready_replicas=1, desired_replicas=1
            ),
            RuntimeState(
                deployment_name="mystery", namespace="prod", ready_replicas=1, desired_replicas=1
            ),
        ]
    )
    catalog = aggregate(repo_source, runtime_source, config, GENERATED_AT)

    statuses = statuses_by_repo(catalog)
    assert statuses["good-app"] is Status.HEALTHY  # 1 healthy cataloged app
    assert [e.repo.name for e in catalog.gaps.invalid_metadata] == ["broken-app"]
    assert catalog.gaps.invalid_metadata[0].validation_errors  # error shown
    assert catalog.coverage.cataloged == 2
    assert catalog.coverage.total == 3  # the uncataloged repo counts
    assert [
        (d.namespace, d.deployment_name) for d in catalog.gaps.deployed_uncataloged
    ] == [("prod", "mystery")]


# --- configurable annotation prefix ---------------------------------------------


def test_deployment_lookup_uses_the_configured_prefix():
    prefix_config = Config(
        annotation_prefix="acme.example",
        kubernetes=KubernetesConfig(namespaces=("prod",)),
    )
    repo_source = InMemoryRepoSource(
        {
            "acme-app": manifest_text(
                "acme-app", prefix_config, deployment="acme-app", namespace="prod"
            )
        }
    )
    runtime_source = InMemoryRuntimeSource(
        [
            RuntimeState(
                deployment_name="acme-app", namespace="prod", ready_replicas=1, desired_replicas=1
            )
        ]
    )

    catalog = aggregate(repo_source, runtime_source, prefix_config, GENERATED_AT)
    assert catalog.entries[0].status is Status.HEALTHY
    assert catalog.gaps.deployed_uncataloged == ()  # deployment is claimed

    # Under a *different* prefix the same manifest has no usable annotations:
    # validation fails, the app is UNKNOWN, and its deployment is unclaimed.
    other_config = Config(
        annotation_prefix="other.example",
        kubernetes=KubernetesConfig(namespaces=("prod",)),
    )
    catalog = aggregate(repo_source, runtime_source, other_config, GENERATED_AT)
    assert catalog.entries[0].status is Status.UNKNOWN
    assert catalog.entries[0].validation_errors
    assert [d.deployment_name for d in catalog.gaps.deployed_uncataloged] == ["acme-app"]


# --- catalog.json ----------------------------------------------------------------


def test_catalog_json_round_trips_with_documented_shape():
    catalog = run_fixtures(CONFIG_WITH_IGNORES)
    payload = json.loads(to_json(catalog))

    assert payload["schema_version"] == 1
    assert payload["runtime_available"] is True
    assert payload["runtime_unavailable_since"] is None
    # Machine-readable coverage for the future pipeline gate.
    assert payload["coverage"] == {"cataloged": 12, "total": 13, "percent": 92.3}
    # Timestamps are ISO strings.
    assert datetime.fromisoformat(payload["generated_at"]) == GENERATED_AT

    apps = {app["name"]: app for app in payload["apps"]}
    assert len(apps) == 12
    tracker = apps["time-tracker"]
    assert tracker["status"] == Status.HEALTHY.value
    assert tracker["repo"]["default_branch"] == "main"
    assert tracker["runtime"]["ready_replicas"] == 2
    assert datetime.fromisoformat(tracker["runtime"]["last_rollout"])
    # Runtime data is limited to the documented fields — never a pod spec.
    assert set(tracker["runtime"]) == {
        "namespace",
        "deployment",
        "ready_replicas",
        "desired_replicas",
        "image_tags",
        "last_rollout",
        "recent_restarts",
        "lookup_error",
    }

    wiki = apps["legacy-wiki"]
    assert wiki["status"] == Status.UNKNOWN.value
    assert wiki["runtime"] is None
    assert wiki["validation_errors"]

    gaps = payload["gaps"]
    assert [g["name"] for g in gaps["deployed_uncataloged"]] == ["status-page"]
    assert gaps["cataloged_not_deployed"] == ["hr-portal"]
    assert {g["repo"] for g in gaps["invalid_metadata"]} == {
        "legacy-wiki",
        "metrics-dashboard",
    }


def test_catalog_json_is_deterministic():
    assert to_json(run_fixtures()) == to_json(run_fixtures())


def test_catalog_json_runtime_unavailable_banner_fields():
    catalog = run_fixtures(scenario="runtime-unavailable")
    payload = json.loads(to_json(catalog))
    assert payload["runtime_available"] is False
    assert datetime.fromisoformat(payload["runtime_unavailable_since"]) == datetime(
        2026, 7, 1, 5, 30, tzinfo=UTC
    )
    assert all(app["runtime"] is None for app in payload["apps"])
