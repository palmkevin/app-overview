"""Tests for the testrun-mode fake adapters and the fixture set (issue #5).

The fixtures are classified through the real parsing/validation code
(``catalog_generator.validation``) and the configured annotation prefix
(``Config.annotation_key``) — the prefix literal is never hardcoded here.
"""

from pathlib import Path

import pytest

from catalog_generator.config import Config
from catalog_generator.model import Repo, RuntimeState
from catalog_generator.sources.base import RuntimeUnavailableError
from catalog_generator.sources.fake import FakeRepoSource, FakeRuntimeSource, FixtureError
from catalog_generator.validation import parse_manifest, validate_manifest

TESTDATA = Path(__file__).resolve().parent.parent / "testdata"

#: Namespaces the fixtures deploy into (would be config `kubernetes.namespaces`).
NAMESPACES = ("apps", "tools")

CONFIG = Config(testdata_dir=str(TESTDATA))


@pytest.fixture()
def repo_source() -> FakeRepoSource:
    return FakeRepoSource(CONFIG.testdata_dir)


@pytest.fixture()
def runtime_source() -> FakeRuntimeSource:
    return FakeRuntimeSource(CONFIG.testdata_dir)


def _manifests_by_repo(source: FakeRepoSource) -> dict[str, str | None]:
    return {repo.name: source.fetch_manifest(repo) for repo in source.list_repos()}


def _declared_deployment(manifest: dict, config: Config) -> tuple[str, str] | None:
    """(namespace, deployment) declared via the *configured* annotation keys."""
    annotations = manifest.get("metadata", {}).get("annotations", {})
    namespace = annotations.get(config.annotation_key("k8s-namespace"))
    deployment = annotations.get(config.annotation_key("k8s-deployment"))
    if namespace and deployment:
        return namespace, deployment
    return None


# --- FakeRepoSource ----------------------------------------------------------


def test_list_repos_returns_fixture_repos_with_metadata(repo_source):
    repos = {repo.name: repo for repo in repo_source.list_repos()}
    assert len(repos) == 12

    tracker = repos["time-tracker"]
    assert isinstance(tracker, Repo)
    assert tracker.url.endswith("/repos/time-tracker")
    assert tracker.default_branch == "main"
    assert tracker.last_commit_date is not None
    assert tracker.last_commit_date.isoformat() == "2026-06-12T09:41:00+00:00"
    assert tracker.has_readme is True

    assert repos["asset-inventory"].has_readme is False
    assert repos["invoice-portal"].default_branch == "develop"


def test_list_repos_is_sorted(repo_source):
    names = [repo.name for repo in repo_source.list_repos()]
    assert names == sorted(names)


def test_fetch_manifest_returns_raw_text(repo_source):
    manifests = _manifests_by_repo(repo_source)
    text = manifests["time-tracker"]
    assert text is not None
    assert "name: time-tracker" in text


def test_fetch_manifest_none_for_uncataloged_repo(repo_source):
    manifests = _manifests_by_repo(repo_source)
    assert manifests["ops-scripts"] is None  # scenario 4: repo without manifest


# --- FakeRuntimeSource -------------------------------------------------------


def test_get_deployment_returns_fixture_state(runtime_source):
    state = runtime_source.get_deployment("tools", "time-tracker")
    assert isinstance(state, RuntimeState)
    assert state.ready_replicas == state.desired_replicas == 2
    assert state.image_tags and all(":" in tag for tag in state.image_tags)
    assert state.last_rollout is not None
    assert state.last_rollout.isoformat() == "2026-06-12T10:02:00+00:00"


def test_get_deployment_returns_none_when_absent(runtime_source):
    assert runtime_source.get_deployment("apps", "hr-portal") is None
    assert runtime_source.get_deployment("no-such-namespace", "time-tracker") is None


def test_list_deployments_filters_by_namespace(runtime_source):
    tools_only = runtime_source.list_deployments(["tools"])
    assert tools_only and all(d.namespace == "tools" for d in tools_only)
    everything = runtime_source.list_deployments(NAMESPACES)
    assert len(everything) > len(tools_only)
    assert runtime_source.list_deployments([]) == []


def test_runtime_unavailable_scenario_raises_with_deterministic_since():
    source = FakeRuntimeSource(CONFIG.testdata_dir, scenario="runtime-unavailable")
    with pytest.raises(RuntimeUnavailableError) as first:
        source.get_deployment("tools", "time-tracker")
    with pytest.raises(RuntimeUnavailableError) as second:
        source.list_deployments(NAMESPACES)
    assert first.value.since is not None
    assert first.value.since == second.value.since
    assert first.value.since.isoformat() == "2026-07-01T05:30:00+00:00"


def test_unknown_scenario_is_rejected():
    with pytest.raises(FixtureError, match="unknown scenario"):
        FakeRuntimeSource(CONFIG.testdata_dir, scenario="no-such-scenario")


# --- determinism -------------------------------------------------------------


def test_two_independent_runs_yield_identical_results():
    first_repo, second_repo = (FakeRepoSource(CONFIG.testdata_dir) for _ in range(2))
    assert first_repo.list_repos() == second_repo.list_repos()
    assert _manifests_by_repo(first_repo) == _manifests_by_repo(second_repo)

    first_rt, second_rt = (FakeRuntimeSource(CONFIG.testdata_dir) for _ in range(2))
    assert first_rt.list_deployments(NAMESPACES) == second_rt.list_deployments(NAMESPACES)
    assert first_rt.get_deployment("tools", "pdf-renderer") == second_rt.get_deployment(
        "tools", "pdf-renderer"
    )


# --- fixture-set completeness (docs/decisions.md scenarios 1-10) -------------


def test_fixture_set_covers_all_required_scenarios(repo_source, runtime_source):
    config = CONFIG
    manifests = _manifests_by_repo(repo_source)
    deployments = {
        (d.namespace, d.deployment_name): d for d in runtime_source.list_deployments(NAMESPACES)
    }

    valid: dict[str, dict] = {}
    parse_failures: list[str] = []
    validation_failures: list[str] = []
    uncataloged_repos: list[str] = []
    for name, text in manifests.items():
        if text is None:
            uncataloged_repos.append(name)
            continue
        parsed, errors = parse_manifest(text)
        if parsed is None:
            parse_failures.append(name)
            continue
        errors = validate_manifest(parsed, annotation_prefix=config.annotation_prefix)
        if errors:
            validation_failures.append(name)
        else:
            valid[name] = parsed

    # 1. several (4-6) valid apps whose declared deployment is fully ready
    healthy = [
        name
        for name, manifest in valid.items()
        if (target := _declared_deployment(manifest, config)) in deployments
        and deployments[target].ready_replicas == deployments[target].desired_replicas > 0
    ]
    assert 4 <= len(healthy) <= 6
    # ...with varied owners, types and lifecycles
    owners = {m["spec"]["owner"] for m in valid.values()}
    types = {m["spec"]["type"] for m in valid.values()}
    lifecycles = {m["spec"]["lifecycle"] for m in valid.values()}
    assert len(owners) >= 3
    assert types == {"website", "service", "tool"}
    assert {"production", "experimental", "deprecated"} <= lifecycles

    # 2. one manifest with a real YAML parse error
    assert parse_failures == ["legacy-wiki"]

    # 3. one manifest that parses but fails validation (missing spec.owner)
    assert validation_failures == ["metrics-dashboard"]

    # 4. one repo without any manifest
    assert uncataloged_repos == ["ops-scripts"]

    # 5. one deployed-but-uncataloged workload (no manifest claims it)
    claimed = {_declared_deployment(m, config) for m in valid.values()}
    unclaimed = set(deployments) - claimed
    assert ("tools", "status-page") in unclaimed

    # 6. one degraded app (0 < ready < desired) claimed by a valid manifest
    degraded = deployments[_declared_deployment(valid["pdf-renderer"], config)]
    assert 0 < degraded.ready_replicas < degraded.desired_replicas

    # 7. one down app (ready == 0, desired > 0) claimed by a valid manifest
    down = deployments[_declared_deployment(valid["ldap-sync"], config)]
    assert down.ready_replicas == 0
    assert down.desired_replicas > 0

    # 8. one experimental app, not deployed, without k8s annotations (no gap)
    experimental = valid["chat-prototype"]
    assert experimental["spec"]["lifecycle"] == "experimental"
    assert _declared_deployment(experimental, config) is None

    # 9. one production app whose declared deployment is missing (gap)
    missing_target = _declared_deployment(valid["hr-portal"], config)
    assert valid["hr-portal"]["spec"]["lifecycle"] == "production"
    assert missing_target is not None
    assert missing_target not in deployments

    # 10. runtime-unavailable scenario exists and raises (see dedicated test)
    with pytest.raises(RuntimeUnavailableError):
        FakeRuntimeSource(config.testdata_dir, scenario="runtime-unavailable").list_deployments(
            NAMESPACES
        )

    # ignore-list candidates: infra deployments present for the gap check
    infra = {name for _, name in unclaimed}
    assert {"nginx-ingress", "cert-manager"} <= infra
