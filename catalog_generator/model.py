"""Domain model: typed structures shared by all pipeline stages.

These dataclasses are the contract between the source adapters (fake or
real), the aggregation step, validation, and rendering. They hold plain
data only — no I/O, no validation rules (those live exclusively in
``catalog_generator.validation``).
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Status(Enum):
    """Derived per-app runtime status (PRD §6.2 status table)."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"
    NOT_DEPLOYED = "not-deployed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Repo:
    """A repository as reported by the repo source (Bitbucket or fake)."""

    name: str
    url: str
    default_branch: str
    last_commit_date: datetime | None = None
    has_readme: bool = False


@dataclass(frozen=True)
class Link:
    """An optional extra link declared in the manifest (``metadata.links``)."""

    url: str
    title: str | None = None


@dataclass(frozen=True)
class Manifest:
    """Parsed ``catalog-info.yaml`` fields (Backstage ``Component`` subset).

    All fields are optional at this layer: a manifest may be incomplete or
    invalid and is still represented here so the page can show it with an
    "invalid metadata" badge. Whether the content is *valid* is decided
    solely by ``catalog_generator.validation``.

    ``annotations`` holds the raw annotation mapping; the configurable
    annotation prefix (see ``catalog_generator.config``) is applied by the
    consumers, never assumed here.
    """

    name: str | None = None
    description: str | None = None
    annotations: dict[str, str] = field(default_factory=dict)
    links: tuple[Link, ...] = ()
    tags: tuple[str, ...] = ()
    type: str | None = None
    lifecycle: str | None = None
    owner: str | None = None


@dataclass(frozen=True)
class RuntimeState:
    """Observed Kubernetes state for one Deployment (or a lookup failure)."""

    deployment_name: str
    namespace: str
    ready_replicas: int = 0
    desired_replicas: int = 0
    image_tags: tuple[str, ...] = ()
    last_rollout: datetime | None = None
    #: Container restarts observed within the "recent" window; feeds the
    #: Degraded heuristic (PRD: "0 < ready < desired, or recent restarts").
    recent_restarts: int = 0
    #: Human-readable error if the declared Deployment could not be looked
    #: up (API error for this app only). ``None`` on success.
    lookup_error: str | None = None


@dataclass(frozen=True)
class AppEntry:
    """One catalog row: repo + manifest + runtime + derived status."""

    repo: Repo
    manifest: Manifest | None = None
    runtime: RuntimeState | None = None
    status: Status = Status.UNKNOWN
    #: Parse/validation error messages (empty when the manifest is valid).
    validation_errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class Gaps:
    """Data-quality gaps surfaced on the page (PRD §6.3 gaps section)."""

    #: Deployments found in the scanned namespaces with no cataloged app
    #: claiming them (after the ignore-list is applied).
    deployed_uncataloged: tuple[RuntimeState, ...] = ()
    #: Non-experimental cataloged apps whose declared Deployment is missing.
    cataloged_not_deployed: tuple[AppEntry, ...] = ()
    #: Repos whose manifest failed to parse or validate.
    invalid_metadata: tuple[AppEntry, ...] = ()


@dataclass(frozen=True)
class Coverage:
    """Catalog coverage stat ("34 of 38 repos cataloged")."""

    cataloged: int
    total: int

    @property
    def percent(self) -> float:
        """Coverage as a percentage (0.0 when there are no repos)."""
        if self.total <= 0:
            return 0.0
        return 100.0 * self.cataloged / self.total


@dataclass(frozen=True)
class CatalogData:
    """The fully aggregated dataset handed to the renderer."""

    entries: tuple[AppEntry, ...]
    gaps: Gaps
    coverage: Coverage
    #: Injected by the caller for determinism — never read the clock here.
    generated_at: datetime
    #: ``False`` when the runtime API was entirely unreachable; the page
    #: still renders from repo data, with a banner.
    runtime_available: bool = True
    #: Timestamp for the "runtime data unavailable since <ts>" banner.
    runtime_unavailable_since: datetime | None = None
