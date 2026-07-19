"""Abstract source interfaces implemented by both fake and real adapters.

Two source kinds feed the aggregation step (PRD §6.2):

* :class:`RepoSource` — yields repositories and their raw
  ``catalog-info.yaml`` text (Bitbucket in live mode, fixtures in testrun).
* :class:`RuntimeSource` — yields observed Kubernetes Deployment state
  (Rancher/k3s API in live mode, fixtures in testrun).

Everything downstream of these interfaces is identical in both modes.
Adapters do NOT parse or validate manifests — they hand back raw text;
parsing/validation is owned by :mod:`catalog_generator.validation`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from datetime import datetime

from catalog_generator.model import Repo, RuntimeState


class RuntimeUnavailableError(Exception):
    """The runtime API is entirely unreachable (not a per-app lookup error).

    Aggregation catches this to render the "runtime data unavailable since
    <timestamp>" banner while still producing the page from repo data
    (PRD acceptance criterion 3). ``since`` is the moment the outage was
    observed (injectable/deterministic in testrun mode).
    """

    def __init__(self, message: str, since: datetime | None = None) -> None:
        super().__init__(message)
        self.since = since


class RepoSource(ABC):
    """Source of repositories and their raw catalog manifests."""

    @abstractmethod
    def list_repos(self) -> list[Repo]:
        """Return all known repositories (deterministic order)."""

    @abstractmethod
    def fetch_manifest(self, repo: Repo) -> str | None:
        """Return the raw ``catalog-info.yaml`` text for ``repo``.

        Returns ``None`` when the repo has no manifest file (the repo then
        counts as *uncataloged* in coverage). The text is returned verbatim
        — even if it is invalid YAML — so validation can report the error.
        """


class RuntimeSource(ABC):
    """Source of observed Kubernetes Deployment state."""

    @abstractmethod
    def get_deployment(self, namespace: str, name: str) -> RuntimeState | None:
        """Look up one declared Deployment.

        Returns the observed :class:`~catalog_generator.model.RuntimeState`,
        or ``None`` when no such Deployment exists (→ *not deployed*).
        A per-app lookup failure is reported via ``RuntimeState.lookup_error``;
        raises :class:`RuntimeUnavailableError` only when the runtime API as a
        whole is unreachable.
        """

    @abstractmethod
    def list_deployments(self, namespaces: Sequence[str]) -> list[RuntimeState]:
        """List all Deployments in the given namespaces (deterministic order).

        Feeds the *deployed-but-uncataloged* cross-check: each record carries
        at least namespace, name, replica counts, and image tags. Raises
        :class:`RuntimeUnavailableError` when the runtime API is unreachable.
        """
