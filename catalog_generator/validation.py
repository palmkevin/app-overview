"""Manifest validation rules — the single source of truth.

All ``catalog-info.yaml`` validation rules (PRD §6.1) live in this module
ONLY. It is reused unchanged by the future Jenkins pipeline step, so it must
stay standalone: no imports from the generator's config/model modules. The
annotation prefix is always passed in as a parameter (default is only a
placeholder — never rely on the literal value elsewhere).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import yaml

DEFAULT_ANNOTATION_PREFIX = "ourcompany.io"

ALLOWED_LIFECYCLES = frozenset({"experimental", "production", "deprecated"})
ALLOWED_TYPES = frozenset({"website", "service", "tool"})

NAME_MAX_LENGTH = 50
_KEBAB_CASE_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


@dataclass(frozen=True)
class ValidationError:
    """A single validation problem: the offending field path and a readable message."""

    field: str
    message: str

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return f"{self.field}: {self.message}"


def parse_manifest(text: str) -> tuple[dict | None, list[ValidationError]]:
    """Parse ``catalog-info.yaml`` text.

    Returns ``(manifest_dict, [])`` on success, or ``(None, [error])`` when the
    YAML does not parse or is not a mapping. Never raises.
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return None, [ValidationError("<file>", f"YAML parse error: {_summarize_yaml_error(exc)}")]
    if data is None:
        return None, [ValidationError("<file>", "manifest is empty")]
    if not isinstance(data, dict):
        return None, [
            ValidationError(
                "<file>",
                f"manifest must be a YAML mapping, got {type(data).__name__}",
            )
        ]
    return data, []


def validate_manifest(
    manifest: dict,
    annotation_prefix: str = DEFAULT_ANNOTATION_PREFIX,
) -> list[ValidationError]:
    """Validate a parsed manifest against the PRD §6.1 rules.

    Unknown extra fields are allowed (forward compatibility) — only the rules
    below are checked:

    * required: ``metadata.name``, ``metadata.description``, ``spec.type``,
      ``spec.lifecycle``, ``spec.owner``, and both
      ``{prefix}/k8s-deployment`` / ``{prefix}/k8s-namespace`` annotations
      (exception: ``lifecycle: experimental`` MAY omit the k8s annotations);
    * ``metadata.name``: kebab-case, max 50 chars;
    * ``spec.lifecycle`` in {experimental, production, deprecated};
    * ``spec.type`` in {website, service, tool}.
    """
    errors: list[ValidationError] = []

    metadata = _require_mapping(manifest, "metadata", errors)
    spec = _require_mapping(manifest, "spec", errors)

    name = _require_string(metadata, "metadata", "name", errors)
    if name is not None:
        errors.extend(_check_name(name))
    _require_string(metadata, "metadata", "description", errors)

    type_ = _require_string(spec, "spec", "type", errors)
    if type_ is not None and type_ not in ALLOWED_TYPES:
        errors.append(
            ValidationError(
                "spec.type",
                f"invalid value {type_!r}; must be one of: {_choices(ALLOWED_TYPES)}",
            )
        )

    lifecycle = _require_string(spec, "spec", "lifecycle", errors)
    if lifecycle is not None and lifecycle not in ALLOWED_LIFECYCLES:
        errors.append(
            ValidationError(
                "spec.lifecycle",
                f"invalid value {lifecycle!r}; must be one of: {_choices(ALLOWED_LIFECYCLES)}",
            )
        )

    _require_string(spec, "spec", "owner", errors)

    if lifecycle != "experimental":
        errors.extend(_check_k8s_annotations(metadata, annotation_prefix))

    return errors


def check_name_uniqueness(manifests: list[dict]) -> list[ValidationError]:
    """Cross-repo rule: ``metadata.name`` must be unique across all manifests.

    Returns one error per duplicated name, listing how often it occurs.
    Manifests without a usable name are ignored here (flagged by
    :func:`validate_manifest` individually).
    """
    counts: dict[str, int] = {}
    for manifest in manifests:
        metadata = manifest.get("metadata")
        if not isinstance(metadata, dict):
            continue
        name = metadata.get("name")
        if isinstance(name, str) and name:
            counts[name] = counts.get(name, 0) + 1
    return [
        ValidationError(
            "metadata.name",
            f"name {name!r} is used by {count} manifests; names must be unique across repos",
        )
        for name, count in sorted(counts.items())
        if count > 1
    ]


# --- helpers ---------------------------------------------------------------


def _summarize_yaml_error(exc: yaml.YAMLError) -> str:
    """A one-line, human-readable summary of a PyYAML error."""
    if isinstance(exc, yaml.MarkedYAMLError) and exc.problem is not None:
        mark = exc.problem_mark
        location = f" (line {mark.line + 1}, column {mark.column + 1})" if mark else ""
        return f"{exc.problem}{location}"
    return " ".join(str(exc).split()) or type(exc).__name__


def _require_mapping(parent: dict, key: str, errors: list[ValidationError]) -> dict:
    """Return ``parent[key]`` if it is a mapping; record an error and return {} otherwise."""
    value = parent.get(key)
    if isinstance(value, dict):
        return value
    if value is None:
        errors.append(ValidationError(key, "required section is missing"))
    else:
        errors.append(ValidationError(key, f"must be a mapping, got {type(value).__name__}"))
    return {}


def _require_string(
    section: dict, section_name: str, key: str, errors: list[ValidationError]
) -> str | None:
    """Return ``section[key]`` if it is a non-empty string; record an error otherwise."""
    field = f"{section_name}.{key}"
    value = section.get(key)
    if isinstance(value, str) and value.strip():
        return value
    if value is None or (isinstance(value, str) and not value.strip()):
        errors.append(ValidationError(field, "required field is missing or empty"))
    else:
        errors.append(ValidationError(field, f"must be a string, got {type(value).__name__}"))
    return None


def _check_name(name: str) -> list[ValidationError]:
    errors: list[ValidationError] = []
    if len(name) > NAME_MAX_LENGTH:
        errors.append(
            ValidationError(
                "metadata.name",
                f"is {len(name)} chars long; maximum is {NAME_MAX_LENGTH}",
            )
        )
    if not _KEBAB_CASE_RE.match(name):
        errors.append(
            ValidationError(
                "metadata.name",
                f"{name!r} is not kebab-case (lowercase letters/digits separated by"
                " single hyphens, no leading/trailing hyphen)",
            )
        )
    return errors


def _check_k8s_annotations(metadata: dict, annotation_prefix: str) -> list[ValidationError]:
    errors: list[ValidationError] = []
    annotations = metadata.get("annotations")
    if not isinstance(annotations, dict):
        annotations = {}
    for suffix in ("k8s-deployment", "k8s-namespace"):
        key = f"{annotation_prefix}/{suffix}"
        value = annotations.get(key)
        if not (isinstance(value, str) and value.strip()):
            errors.append(
                ValidationError(
                    f"metadata.annotations.{key}",
                    "required annotation is missing or empty"
                    " (only 'lifecycle: experimental' apps may omit the k8s annotations)",
                )
            )
    return errors


def _choices(values: frozenset[str]) -> str:
    return ", ".join(sorted(values))
