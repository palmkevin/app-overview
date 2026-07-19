"""Fake (testrun-mode) adapters reading deterministic fixtures from testdata/.

Fixture layout::

    testdata/
      repos/<repo-name>/repo.yaml           # url, default_branch, last_commit_date, has_readme
      repos/<repo-name>/catalog-info.yaml   # optional; absent = uncataloged repo
      k8s.yaml                              # all fake Deployment records
      scenarios/<scenario>.yaml             # e.g. runtime_unavailable: true

Everything is fully deterministic: no network, no randomness, no clock
reads — every timestamp comes from the fixture files. Repos and
deployments are returned in sorted order so two runs over the same
fixtures always yield identical results.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

import yaml

from catalog_generator.model import Repo, RuntimeState
from catalog_generator.sources.base import (
    RepoSource,
    RuntimeSource,
    RuntimeUnavailableError,
)

DEFAULT_SCENARIO = "default"


class FixtureError(Exception):
    """A fixture file is missing or malformed (a bug in testdata/, not runtime state)."""


class FakeRepoSource(RepoSource):
    """Repo source backed by ``<testdata_dir>/repos/<name>/`` fixtures."""

    def __init__(self, testdata_dir: str | Path = "testdata") -> None:
        self._repos_dir = Path(testdata_dir) / "repos"

    def list_repos(self) -> list[Repo]:
        if not self._repos_dir.is_dir():
            raise FixtureError(f"fixture directory not found: {self._repos_dir}")
        repos: list[Repo] = []
        for repo_dir in sorted(p for p in self._repos_dir.iterdir() if p.is_dir()):
            meta = _load_yaml_mapping(repo_dir / "repo.yaml")
            repos.append(
                Repo(
                    name=repo_dir.name,
                    url=_require_str(meta, "url", repo_dir / "repo.yaml"),
                    default_branch=_require_str(meta, "default_branch", repo_dir / "repo.yaml"),
                    last_commit_date=_parse_datetime(meta.get("last_commit_date")),
                    has_readme=bool(meta.get("has_readme", False)),
                )
            )
        return repos

    def fetch_manifest(self, repo: Repo) -> str | None:
        manifest_path = self._repos_dir / repo.name / "catalog-info.yaml"
        if not manifest_path.is_file():
            return None
        return manifest_path.read_text(encoding="utf-8")


class FakeRuntimeSource(RuntimeSource):
    """Runtime source backed by ``<testdata_dir>/k8s.yaml``.

    ``scenario`` selects ``<testdata_dir>/scenarios/<scenario>.yaml``; with
    ``runtime_unavailable: true`` every call raises
    :class:`RuntimeUnavailableError` carrying the scenario's deterministic
    ``since`` timestamp.
    """

    def __init__(
        self,
        testdata_dir: str | Path = "testdata",
        scenario: str = DEFAULT_SCENARIO,
    ) -> None:
        testdata_dir = Path(testdata_dir)
        scenario_path = testdata_dir / "scenarios" / f"{scenario}.yaml"
        if not scenario_path.is_file():
            available = sorted(
                p.stem for p in (testdata_dir / "scenarios").glob("*.yaml") if p.is_file()
            )
            raise FixtureError(
                f"unknown scenario {scenario!r}: {scenario_path} not found "
                f"(available: {', '.join(available) or 'none'})"
            )
        profile = _load_yaml_mapping(scenario_path)
        self._unavailable = bool(profile.get("runtime_unavailable", False))
        self._unavailable_since = _parse_datetime(profile.get("since"))
        self._deployments = self._load_deployments(testdata_dir / "k8s.yaml")

    @staticmethod
    def _load_deployments(path: Path) -> list[RuntimeState]:
        data = _load_yaml_mapping(path)
        raw = data.get("deployments")
        if not isinstance(raw, list):
            raise FixtureError(f"{path}: 'deployments' must be a list")
        deployments: list[RuntimeState] = []
        for record in raw:
            if not isinstance(record, dict):
                raise FixtureError(f"{path}: each deployment must be a mapping, got {record!r}")
            deployments.append(
                RuntimeState(
                    deployment_name=_require_str(record, "name", path),
                    namespace=_require_str(record, "namespace", path),
                    ready_replicas=int(record.get("ready_replicas", 0)),
                    desired_replicas=int(record.get("desired_replicas", 0)),
                    image_tags=tuple(record.get("image_tags", ())),
                    last_rollout=_parse_datetime(record.get("last_rollout")),
                    recent_restarts=int(record.get("recent_restarts", 0)),
                )
            )
        deployments.sort(key=lambda d: (d.namespace, d.deployment_name))
        return deployments

    def _check_available(self) -> None:
        if self._unavailable:
            raise RuntimeUnavailableError(
                "runtime API unreachable (simulated by scenario fixture)",
                since=self._unavailable_since,
            )

    def get_deployment(self, namespace: str, name: str) -> RuntimeState | None:
        self._check_available()
        for deployment in self._deployments:
            if deployment.namespace == namespace and deployment.deployment_name == name:
                return deployment
        return None

    def list_deployments(self, namespaces: Sequence[str]) -> list[RuntimeState]:
        self._check_available()
        wanted = set(namespaces)
        return [d for d in self._deployments if d.namespace in wanted]


# --- fixture-file helpers ----------------------------------------------------


def _load_yaml_mapping(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FixtureError(f"cannot read fixture file {path}: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise FixtureError(f"fixture file {path} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise FixtureError(f"fixture file {path} must contain a YAML mapping")
    return data


def _require_str(data: dict, key: str, path: Path) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise FixtureError(f"{path}: '{key}' must be a non-empty string, got {value!r}")
    return value


def _parse_datetime(value: object) -> datetime | None:
    """Accept ``None``, an ISO-8601 string, or an already-parsed datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    raise FixtureError(f"expected an ISO-8601 timestamp string, got {value!r}")
