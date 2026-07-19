"""Real Bitbucket Server / Data Center REST adapter (live mode, issue #6).

Implements :class:`~catalog_generator.sources.base.RepoSource` against the
Bitbucket Server / Data Center REST API (``/rest/api/1.0/...``) using
``requests``. Read-only: the adapter only ever issues GET requests.

Authentication uses a Bearer token read from the environment variable named
by the configuration (``bitbucket.token_env``) — never from a config file,
and the token value never appears in any produced data.

Failure semantics (PRD §6.2 / CLAUDE.md hard rules):

* Cannot reach Bitbucket at all (connection error / timeout / auth or
  server error while listing repositories) → :class:`RepoSourceUnavailableError`,
  which the CLI turns into a non-zero exit.
* Per-repo *detail* lookups (default branch, last commit, README existence)
  degrade gracefully — one odd repo must never break a generator run.
* ``fetch_manifest``: HTTP 404 → ``None`` (uncataloged repo). Connection
  errors or unexpected statuses raise :class:`RepoSourceUnavailableError`,
  because at that point the source as a whole is misbehaving (bad token,
  proxy, outage) rather than one manifest being broken.

Every HTTP request carries an explicit timeout.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import requests

from catalog_generator.config import Config
from catalog_generator.model import Repo
from catalog_generator.sources.base import RepoSource

#: Seconds before any single HTTP request is abandoned. Applied to EVERY call.
DEFAULT_TIMEOUT = 10.0

#: Page size for paged listings (Bitbucket caps this server-side anyway).
PAGE_LIMIT = 100

#: File whose existence backs the catalog's README/docs link (PRD §6.2).
README_PATH = "README.md"

#: The manifest file fetched from each repo's default branch (PRD §6.1).
MANIFEST_PATH = "catalog-info.yaml"

_PROJECT_KEY_IN_URL = re.compile(r"/projects/([^/]+)/repos/")


class RepoSourceUnavailableError(Exception):
    """Bitbucket is entirely unreachable (not a per-repo hiccup).

    The CLI catches this to exit non-zero — the one condition under which
    a generator run is allowed to fail (PRD §6.2: "Generator exits non-zero
    only on total failure").
    """


class BitbucketRepoSource(RepoSource):
    """Repo source backed by the Bitbucket Server / Data Center REST API."""

    def __init__(self, config: Config, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        settings = config.bitbucket
        if not settings.base_url:
            raise ValueError("BitbucketRepoSource requires bitbucket.base_url to be configured")
        if not settings.project_keys:
            raise ValueError("BitbucketRepoSource requires bitbucket.project_keys to be configured")
        self._base_url = settings.base_url.rstrip("/")
        self._api_url = f"{self._base_url}/rest/api/1.0"
        self._project_keys = settings.project_keys
        self._timeout = timeout
        #: repo name → project key, filled by :meth:`list_repos` so that
        #: :meth:`fetch_manifest` knows where each repo lives.
        self._project_by_repo: dict[str, str] = {}
        self._session = requests.Session()
        token = config.bitbucket_token()
        if token:
            self._session.headers["Authorization"] = f"Bearer {token}"

    # --- RepoSource interface -------------------------------------------------

    def list_repos(self) -> list[Repo]:
        """All repos of the configured project keys, in deterministic order."""
        repos: list[Repo] = []
        for project_key in self._project_keys:
            for record in self._list_project_repos(project_key):
                repo = self._build_repo(project_key, record)
                self._project_by_repo[repo.name] = project_key
                repos.append(repo)
        repos.sort(key=lambda repo: repo.name)
        return repos

    def fetch_manifest(self, repo: Repo) -> str | None:
        """Raw ``catalog-info.yaml`` from the repo's default branch; 404 → None."""
        project_key = self._project_key_for(repo)
        url = f"{self._api_url}/projects/{project_key}/repos/{repo.name}/raw/{MANIFEST_PATH}"
        params = {"at": f"refs/heads/{repo.default_branch}"} if repo.default_branch else None
        try:
            response = self._session.get(url, params=params, timeout=self._timeout)
        except requests.RequestException as exc:
            raise RepoSourceUnavailableError(
                f"cannot fetch manifest for repo {repo.name!r}: {exc}"
            ) from exc
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise RepoSourceUnavailableError(
                f"Bitbucket returned HTTP {response.status_code} fetching the manifest "
                f"of repo {repo.name!r}"
            )
        return response.text

    # --- listing / pagination ---------------------------------------------------

    def _list_project_repos(self, project_key: str) -> list[dict]:
        """One project's repo records, following isLastPage/nextPageStart paging.

        Any failure here means the repository listing — the generator's
        primary input — is unavailable, so it raises
        :class:`RepoSourceUnavailableError`.
        """
        url = f"{self._api_url}/projects/{project_key}/repos"
        records: list[dict] = []
        start = 0
        while True:
            try:
                response = self._session.get(
                    url, params={"limit": PAGE_LIMIT, "start": start}, timeout=self._timeout
                )
            except requests.RequestException as exc:
                raise RepoSourceUnavailableError(
                    f"cannot reach Bitbucket at {self._base_url}: {exc}"
                ) from exc
            if response.status_code != 200:
                raise RepoSourceUnavailableError(
                    f"Bitbucket returned HTTP {response.status_code} listing repos "
                    f"of project {project_key!r}"
                )
            page = response.json()
            values = page.get("values")
            if not isinstance(values, list):
                raise RepoSourceUnavailableError(
                    f"unexpected Bitbucket response listing repos of project "
                    f"{project_key!r}: no 'values' list"
                )
            records.extend(record for record in values if isinstance(record, dict))
            if page.get("isLastPage", True):
                return records
            next_start = page.get("nextPageStart")
            if not isinstance(next_start, int) or next_start <= start:
                # Defensive: never loop forever on a malformed paging response.
                return records
            start = next_start

    # --- per-repo details (degrade gracefully, never break the run) -------------

    def _build_repo(self, project_key: str, record: dict) -> Repo:
        slug = record.get("slug") or record.get("name") or ""
        repo_api = f"{self._api_url}/projects/{project_key}/repos/{slug}"
        default_branch = self._default_branch(repo_api)
        return Repo(
            name=slug,
            url=self._browse_url(project_key, slug, record),
            default_branch=default_branch,
            last_commit_date=self._last_commit_date(repo_api, default_branch),
            has_readme=self._has_readme(repo_api, default_branch),
        )

    def _browse_url(self, project_key: str, slug: str, record: dict) -> str:
        links = record.get("links")
        if isinstance(links, dict):
            for link in links.get("self") or []:
                href = link.get("href") if isinstance(link, dict) else None
                if href:
                    return href
        return f"{self._base_url}/projects/{project_key}/repos/{slug}/browse"

    def _default_branch(self, repo_api: str) -> str:
        """The default branch's display name, or ``""`` when undeterminable."""
        data = self._get_json_or_none(f"{repo_api}/branches/default")
        if data is None:
            return ""
        display_id = data.get("displayId")
        return display_id if isinstance(display_id, str) else ""

    def _last_commit_date(self, repo_api: str, default_branch: str) -> datetime | None:
        """Timestamp of the newest commit on the default branch, if any."""
        if not default_branch:
            return None
        data = self._get_json_or_none(
            f"{repo_api}/commits",
            params={"until": f"refs/heads/{default_branch}", "limit": 1},
        )
        if not data:
            return None
        values = data.get("values")
        if not isinstance(values, list) or not values:
            return None
        commit = values[0]
        if not isinstance(commit, dict):
            return None
        # Bitbucket reports epoch milliseconds.
        timestamp_ms = commit.get("committerTimestamp", commit.get("authorTimestamp"))
        if not isinstance(timestamp_ms, (int, float)):
            return None
        return datetime.fromtimestamp(timestamp_ms / 1000.0, tz=UTC)

    def _has_readme(self, repo_api: str, default_branch: str) -> bool:
        """Whether ``README.md`` exists on the default branch (for the docs link)."""
        if not default_branch:
            return False
        try:
            response = self._session.get(
                f"{repo_api}/browse/{README_PATH}",
                params={"type": "true", "at": f"refs/heads/{default_branch}"},
                timeout=self._timeout,
            )
        except requests.RequestException:
            return False
        return response.status_code == 200

    # --- helpers -----------------------------------------------------------------

    def _get_json_or_none(self, url: str, params: dict | None = None) -> dict | None:
        """GET a JSON document; any per-repo failure degrades to ``None``."""
        try:
            response = self._session.get(url, params=params, timeout=self._timeout)
        except requests.RequestException:
            return None
        if response.status_code != 200:
            return None
        try:
            data = response.json()
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def _project_key_for(self, repo: Repo) -> str:
        project_key = self._project_by_repo.get(repo.name)
        if project_key:
            return project_key
        match = _PROJECT_KEY_IN_URL.search(repo.url)
        if match:
            return match.group(1)
        raise RepoSourceUnavailableError(
            f"cannot determine the Bitbucket project of repo {repo.name!r} "
            "(repo was not returned by list_repos and its URL has no project key)"
        )
