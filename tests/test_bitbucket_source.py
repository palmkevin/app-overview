"""Tests for the real Bitbucket REST adapter (issue #6).

All HTTP is mocked by monkeypatching ``requests.Session`` — NO live network
calls, no extra test dependencies. Hostnames and tokens are placeholders.
"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlsplit

import pytest
import requests

from catalog_generator.config import BitbucketConfig, Config
from catalog_generator.model import Repo
from catalog_generator.sources.bitbucket import (
    BitbucketRepoSource,
    RepoSourceUnavailableError,
)

BASE_URL = "https://bitbucket.example.test"
API = f"{BASE_URL}/rest/api/1.0"
TOKEN_ENV = "TEST_CATALOG_BITBUCKET_TOKEN"
PLACEHOLDER_TOKEN = "placeholder-token-value"

#: 2026-06-12T09:41:00+00:00 as Bitbucket epoch milliseconds.
COMMIT_TS_MS = 1781257260000


class FakeResponse:
    def __init__(self, status_code: int = 200, json_data: object = None, text: str = ""):
        self.status_code = status_code
        self._json = json_data
        self.text = text

    def json(self) -> object:
        if self._json is None:
            raise ValueError("no JSON body")
        return self._json


class FakeSession:
    """Stand-in for ``requests.Session`` dispatching on path + query params.

    Records every call (url, params, timeout) so tests can assert that a
    timeout is passed on EVERY request and that auth headers are set.
    """

    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, dict | None, object]] = []
        #: path → handler(params) -> FakeResponse
        self.routes: dict[str, object] = {}

    def get(self, url: str, params: dict | None = None, timeout: object = None, **kwargs):
        self.calls.append((url, params, timeout))
        path = urlsplit(url).path
        handler = self.routes.get(path)
        if handler is None:
            return FakeResponse(status_code=404, json_data={"errors": []})
        if callable(handler):
            return handler(dict(params or {}))
        return handler


def repo_record(slug: str, project_key: str = "APPS") -> dict:
    return {
        "slug": slug,
        "name": slug,
        "links": {
            "self": [{"href": f"{BASE_URL}/projects/{project_key}/repos/{slug}/browse"}]
        },
    }


def install_repo_details(
    session: FakeSession,
    slug: str,
    project_key: str = "APPS",
    *,
    branch: str = "main",
    commit_ts_ms: int | None = COMMIT_TS_MS,
    has_readme: bool = True,
    manifest: str | None = None,
) -> None:
    """Wire up the per-repo detail routes for one repository."""
    repo_api = f"/rest/api/1.0/projects/{project_key}/repos/{slug}"
    session.routes[f"{repo_api}/branches/default"] = FakeResponse(
        json_data={"id": f"refs/heads/{branch}", "displayId": branch}
    )
    commits: list[dict] = []
    if commit_ts_ms is not None:
        commits = [{"id": "0" * 40, "committerTimestamp": commit_ts_ms}]
    session.routes[f"{repo_api}/commits"] = FakeResponse(
        json_data={"values": commits, "isLastPage": True}
    )
    session.routes[f"{repo_api}/browse/README.md"] = FakeResponse(
        status_code=200 if has_readme else 404, json_data={"type": "FILE"}
    )
    if manifest is not None:
        session.routes[f"{repo_api}/raw/catalog-info.yaml"] = FakeResponse(text=manifest)


def make_config(project_keys: tuple[str, ...] = ("APPS",)) -> Config:
    return Config(
        mode="testrun",  # mode is irrelevant to the adapter itself
        bitbucket=BitbucketConfig(
            base_url=BASE_URL, project_keys=project_keys, token_env=TOKEN_ENV
        ),
    )


@pytest.fixture()
def session(monkeypatch) -> FakeSession:
    fake = FakeSession()
    monkeypatch.setattr(requests, "Session", lambda: fake)
    monkeypatch.setenv(TOKEN_ENV, PLACEHOLDER_TOKEN)
    return fake


def single_page(records: list[dict]):
    return FakeResponse(json_data={"values": records, "isLastPage": True})


# --- listing + repo metadata --------------------------------------------------


def test_list_repos_builds_full_repo_metadata(session):
    session.routes["/rest/api/1.0/projects/APPS/repos"] = single_page(
        [repo_record("time-tracker")]
    )
    install_repo_details(session, "time-tracker", branch="main", has_readme=True)

    repos = BitbucketRepoSource(make_config()).list_repos()

    assert repos == [
        Repo(
            name="time-tracker",
            url=f"{BASE_URL}/projects/APPS/repos/time-tracker/browse",
            default_branch="main",
            last_commit_date=datetime.fromtimestamp(COMMIT_TS_MS / 1000.0, tz=UTC),
            has_readme=True,
        )
    ]


def test_list_repos_follows_multi_page_pagination(session):
    def repo_listing(params: dict) -> FakeResponse:
        start = int(params.get("start", 0))
        if start == 0:
            return FakeResponse(
                json_data={
                    "values": [repo_record("app-one")],
                    "isLastPage": False,
                    "nextPageStart": 1,
                }
            )
        assert start == 1
        return FakeResponse(json_data={"values": [repo_record("app-two")], "isLastPage": True})

    session.routes["/rest/api/1.0/projects/APPS/repos"] = repo_listing
    for slug in ("app-one", "app-two"):
        install_repo_details(session, slug)

    names = [repo.name for repo in BitbucketRepoSource(make_config()).list_repos()]
    assert names == ["app-one", "app-two"]

    listing_calls = [
        params for url, params, _ in session.calls if urlsplit(url).path.endswith("/APPS/repos")
    ]
    assert [c.get("start") for c in listing_calls] == [0, 1]


def test_list_repos_covers_all_configured_projects_sorted(session):
    session.routes["/rest/api/1.0/projects/APPS/repos"] = single_page(
        [repo_record("zeta-app", "APPS")]
    )
    session.routes["/rest/api/1.0/projects/TOOLS/repos"] = single_page(
        [repo_record("alpha-tool", "TOOLS")]
    )
    install_repo_details(session, "zeta-app", "APPS")
    install_repo_details(session, "alpha-tool", "TOOLS")

    repos = BitbucketRepoSource(make_config(("APPS", "TOOLS"))).list_repos()
    assert [repo.name for repo in repos] == ["alpha-tool", "zeta-app"]  # deterministic order
    assert repos[0].url == f"{BASE_URL}/projects/TOOLS/repos/alpha-tool/browse"


def test_last_commit_date_parses_epoch_milliseconds(session):
    session.routes["/rest/api/1.0/projects/APPS/repos"] = single_page([repo_record("app-one")])
    install_repo_details(session, "app-one", commit_ts_ms=COMMIT_TS_MS)

    (repo,) = BitbucketRepoSource(make_config()).list_repos()
    assert repo.last_commit_date == datetime(2026, 6, 12, 9, 41, tzinfo=UTC)
    assert repo.last_commit_date.tzinfo is not None


def test_readme_existence_check(session):
    session.routes["/rest/api/1.0/projects/APPS/repos"] = single_page(
        [repo_record("with-readme"), repo_record("without-readme")]
    )
    install_repo_details(session, "with-readme", has_readme=True)
    install_repo_details(session, "without-readme", has_readme=False)

    repos = {repo.name: repo for repo in BitbucketRepoSource(make_config()).list_repos()}
    assert repos["with-readme"].has_readme is True
    assert repos["without-readme"].has_readme is False


def test_per_repo_detail_failures_degrade_without_breaking_the_run(session):
    session.routes["/rest/api/1.0/projects/APPS/repos"] = single_page(
        [repo_record("healthy"), repo_record("odd-one")]
    )
    install_repo_details(session, "healthy")
    # "odd-one" has NO detail routes at all → 404s on every detail lookup.

    repos = {repo.name: repo for repo in BitbucketRepoSource(make_config()).list_repos()}
    assert repos["healthy"].default_branch == "main"
    odd = repos["odd-one"]
    assert odd.default_branch == ""
    assert odd.last_commit_date is None
    assert odd.has_readme is False


# --- fetch_manifest -------------------------------------------------------------


def test_fetch_manifest_returns_raw_text(session):
    manifest_text = "apiVersion: backstage.io/v1alpha1\nkind: Component\n"
    session.routes["/rest/api/1.0/projects/APPS/repos"] = single_page([repo_record("app-one")])
    install_repo_details(session, "app-one", manifest=manifest_text)

    source = BitbucketRepoSource(make_config())
    (repo,) = source.list_repos()
    assert source.fetch_manifest(repo) == manifest_text

    manifest_call = next(
        (url, params)
        for url, params, _ in session.calls
        if urlsplit(url).path.endswith("/raw/catalog-info.yaml")
    )
    assert manifest_call[1] == {"at": "refs/heads/main"}


def test_fetch_manifest_404_returns_none(session):
    session.routes["/rest/api/1.0/projects/APPS/repos"] = single_page([repo_record("app-one")])
    install_repo_details(session, "app-one", manifest=None)  # no raw route → 404

    source = BitbucketRepoSource(make_config())
    (repo,) = source.list_repos()
    assert source.fetch_manifest(repo) is None


def test_fetch_manifest_without_prior_listing_derives_project_from_url(session):
    manifest_text = "kind: Component\n"
    install_repo_details(session, "app-one", manifest=manifest_text)

    repo = Repo(
        name="app-one",
        url=f"{BASE_URL}/projects/APPS/repos/app-one/browse",
        default_branch="main",
    )
    assert BitbucketRepoSource(make_config()).fetch_manifest(repo) == manifest_text


# --- auth + timeouts -------------------------------------------------------------


def test_bearer_token_from_configured_env_var(session):
    BitbucketRepoSource(make_config())
    assert session.headers["Authorization"] == f"Bearer {PLACEHOLDER_TOKEN}"


def test_no_auth_header_when_env_var_unset(session, monkeypatch):
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    BitbucketRepoSource(make_config())
    assert "Authorization" not in session.headers


def test_every_request_carries_a_timeout(session):
    session.routes["/rest/api/1.0/projects/APPS/repos"] = single_page([repo_record("app-one")])
    install_repo_details(session, "app-one", manifest="kind: Component\n")

    source = BitbucketRepoSource(make_config(), timeout=7.5)
    (repo,) = source.list_repos()
    source.fetch_manifest(repo)

    assert len(session.calls) >= 5  # listing + 3 detail lookups + manifest
    assert all(timeout == 7.5 for _, _, timeout in session.calls)


# --- total-failure semantics ------------------------------------------------------


def test_connection_error_on_initial_listing_raises_distinct_error(session):
    def boom(params: dict) -> FakeResponse:
        raise requests.ConnectionError("connection refused")

    session.routes["/rest/api/1.0/projects/APPS/repos"] = boom

    with pytest.raises(RepoSourceUnavailableError, match="cannot reach Bitbucket"):
        BitbucketRepoSource(make_config()).list_repos()


def test_timeout_on_initial_listing_raises_distinct_error(session):
    def boom(params: dict) -> FakeResponse:
        raise requests.Timeout("timed out")

    session.routes["/rest/api/1.0/projects/APPS/repos"] = boom

    with pytest.raises(RepoSourceUnavailableError):
        BitbucketRepoSource(make_config()).list_repos()


def test_http_error_status_on_listing_raises_distinct_error(session):
    session.routes["/rest/api/1.0/projects/APPS/repos"] = FakeResponse(
        status_code=401, json_data={"errors": [{"message": "unauthorized"}]}
    )

    with pytest.raises(RepoSourceUnavailableError, match="HTTP 401"):
        BitbucketRepoSource(make_config()).list_repos()


def test_connection_error_fetching_manifest_raises_distinct_error(session):
    session.routes["/rest/api/1.0/projects/APPS/repos"] = single_page([repo_record("app-one")])
    install_repo_details(session, "app-one")

    source = BitbucketRepoSource(make_config())
    (repo,) = source.list_repos()

    def boom(params: dict) -> FakeResponse:
        raise requests.ConnectionError("connection refused")

    session.routes["/rest/api/1.0/projects/APPS/repos/app-one/raw/catalog-info.yaml"] = boom
    with pytest.raises(RepoSourceUnavailableError):
        source.fetch_manifest(repo)


def test_malformed_pagination_never_loops_forever(session):
    # isLastPage false but no usable nextPageStart → stop after one page.
    session.routes["/rest/api/1.0/projects/APPS/repos"] = FakeResponse(
        json_data={"values": [repo_record("app-one")], "isLastPage": False}
    )
    install_repo_details(session, "app-one")

    names = [repo.name for repo in BitbucketRepoSource(make_config()).list_repos()]
    assert names == ["app-one"]


# --- configuration guards ----------------------------------------------------------


def test_requires_base_url_and_project_keys(session):
    with pytest.raises(ValueError, match="base_url"):
        BitbucketRepoSource(Config(bitbucket=BitbucketConfig(project_keys=("APPS",))))
    with pytest.raises(ValueError, match="project_keys"):
        BitbucketRepoSource(Config(bitbucket=BitbucketConfig(base_url=BASE_URL)))


def test_no_query_string_urls_only_params(session):
    """Sanity: the adapter passes query params via `params`, keeping URLs clean."""
    session.routes["/rest/api/1.0/projects/APPS/repos"] = single_page([repo_record("app-one")])
    install_repo_details(session, "app-one")

    BitbucketRepoSource(make_config()).list_repos()
    for url, _, _ in session.calls:
        assert parse_qsl(urlsplit(url).query) == []
