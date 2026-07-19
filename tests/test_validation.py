"""Tests for catalog_generator.validation — every PRD §6.1 rule, pass and fail."""

import copy
from pathlib import Path

import pytest

from catalog_generator.validation import (
    DEFAULT_ANNOTATION_PREFIX,
    ValidationError,
    check_name_uniqueness,
    parse_manifest,
    validate_manifest,
)

EXAMPLE_MANIFEST_PATH = Path(__file__).parent.parent / "examples" / "catalog-info.yaml"

# The annotation prefix is configurable; build test manifests for any prefix
# instead of hardcoding one.
NON_DEFAULT_PREFIX = "example-corp.dev"


def make_manifest(prefix: str = DEFAULT_ANNOTATION_PREFIX, **overrides) -> dict:
    """A minimal valid manifest for the given annotation prefix."""
    manifest = {
        "apiVersion": "backstage.io/v1alpha1",
        "kind": "Component",
        "metadata": {
            "name": "time-tracker",
            "description": "Internal time tracking for project billing",
            "annotations": {
                f"{prefix}/k8s-deployment": "time-tracker",
                f"{prefix}/k8s-namespace": "tools",
            },
        },
        "spec": {
            "type": "website",
            "lifecycle": "production",
            "owner": "jane.doe",
        },
    }
    manifest = copy.deepcopy(manifest)
    for dotted, value in overrides.items():
        section, key = dotted.split("__")
        if value is _DELETE:
            manifest[section].pop(key, None)
        else:
            manifest[section][key] = value
    return manifest


_DELETE = object()


def fields_of(errors: list[ValidationError]) -> set[str]:
    return {error.field for error in errors}


# --- parse_manifest ---------------------------------------------------------


def test_parse_valid_yaml() -> None:
    manifest, errors = parse_manifest("metadata:\n  name: foo\n")
    assert errors == []
    assert manifest == {"metadata": {"name": "foo"}}


def test_parse_broken_yaml_returns_error_never_raises() -> None:
    manifest, errors = parse_manifest("metadata: [unclosed\n  name: foo\n")
    assert manifest is None
    assert len(errors) == 1
    assert "YAML parse error" in errors[0].message


def test_parse_empty_document() -> None:
    manifest, errors = parse_manifest("")
    assert manifest is None
    assert errors and "empty" in errors[0].message


def test_parse_non_mapping_document() -> None:
    manifest, errors = parse_manifest("- just\n- a\n- list\n")
    assert manifest is None
    assert errors and "mapping" in errors[0].message


# --- example manifest --------------------------------------------------------


def test_example_manifest_validates_cleanly_with_default_prefix() -> None:
    manifest, errors = parse_manifest(EXAMPLE_MANIFEST_PATH.read_text(encoding="utf-8"))
    assert errors == []
    assert manifest is not None
    assert validate_manifest(manifest) == []
    assert validate_manifest(manifest, annotation_prefix=DEFAULT_ANNOTATION_PREFIX) == []


# --- required fields ---------------------------------------------------------


def test_minimal_valid_manifest_passes() -> None:
    assert validate_manifest(make_manifest()) == []


@pytest.mark.parametrize(
    "override, expected_field",
    [
        ({"metadata__name": _DELETE}, "metadata.name"),
        ({"metadata__description": _DELETE}, "metadata.description"),
        ({"spec__type": _DELETE}, "spec.type"),
        ({"spec__lifecycle": _DELETE}, "spec.lifecycle"),
        ({"spec__owner": _DELETE}, "spec.owner"),
    ],
)
def test_missing_required_field_fails(override: dict, expected_field: str) -> None:
    errors = validate_manifest(make_manifest(**override))
    assert expected_field in fields_of(errors)


def test_missing_metadata_and_spec_sections() -> None:
    errors = validate_manifest({})
    assert {"metadata", "spec"} <= fields_of(errors)


def test_non_string_required_field_fails() -> None:
    errors = validate_manifest(make_manifest(spec__owner=["jane.doe"]))
    assert "spec.owner" in fields_of(errors)


# --- k8s annotations + experimental exception --------------------------------


def annotation_fields(prefix: str) -> set[str]:
    return {
        f"metadata.annotations.{prefix}/k8s-deployment",
        f"metadata.annotations.{prefix}/k8s-namespace",
    }


def test_missing_k8s_annotations_fail_for_production() -> None:
    errors = validate_manifest(make_manifest(metadata__annotations=_DELETE))
    assert annotation_fields(DEFAULT_ANNOTATION_PREFIX) <= fields_of(errors)


def test_experimental_may_omit_k8s_annotations() -> None:
    manifest = make_manifest(metadata__annotations=_DELETE, spec__lifecycle="experimental")
    assert validate_manifest(manifest) == []


def test_experimental_with_annotations_also_passes() -> None:
    assert validate_manifest(make_manifest(spec__lifecycle="experimental")) == []


@pytest.mark.parametrize("lifecycle", ["production", "deprecated"])
def test_non_experimental_requires_annotations(lifecycle: str) -> None:
    manifest = make_manifest(metadata__annotations={}, spec__lifecycle=lifecycle)
    assert annotation_fields(DEFAULT_ANNOTATION_PREFIX) <= fields_of(validate_manifest(manifest))


def test_one_missing_annotation_is_reported_individually() -> None:
    prefix = DEFAULT_ANNOTATION_PREFIX
    manifest = make_manifest(metadata__annotations={f"{prefix}/k8s-deployment": "time-tracker"})
    fields = fields_of(validate_manifest(manifest))
    assert f"metadata.annotations.{prefix}/k8s-namespace" in fields
    assert f"metadata.annotations.{prefix}/k8s-deployment" not in fields


# --- configurable, non-default prefix ----------------------------------------


def test_non_default_prefix_valid_manifest_passes() -> None:
    manifest = make_manifest(prefix=NON_DEFAULT_PREFIX)
    assert validate_manifest(manifest, annotation_prefix=NON_DEFAULT_PREFIX) == []


def test_non_default_prefix_missing_annotations_fail() -> None:
    manifest = make_manifest(prefix=NON_DEFAULT_PREFIX, metadata__annotations={})
    errors = validate_manifest(manifest, annotation_prefix=NON_DEFAULT_PREFIX)
    assert annotation_fields(NON_DEFAULT_PREFIX) <= fields_of(errors)


def test_default_prefix_annotations_do_not_satisfy_other_prefix() -> None:
    # Annotations under the default prefix must NOT count when a different
    # prefix is configured — proves the prefix is not hardcoded internally.
    manifest = make_manifest(prefix=DEFAULT_ANNOTATION_PREFIX)
    errors = validate_manifest(manifest, annotation_prefix=NON_DEFAULT_PREFIX)
    assert annotation_fields(NON_DEFAULT_PREFIX) <= fields_of(errors)


# --- name rules ---------------------------------------------------------------


@pytest.mark.parametrize("name", ["a", "time-tracker", "app2", "a1-b2-c3", "x" * 50])
def test_valid_kebab_case_names(name: str) -> None:
    assert validate_manifest(make_manifest(metadata__name=name)) == []


@pytest.mark.parametrize(
    "name",
    ["Time-Tracker", "time_tracker", "-leading", "trailing-", "double--hyphen", "a b", "ümlaut"],
)
def test_invalid_kebab_case_names(name: str) -> None:
    errors = validate_manifest(make_manifest(metadata__name=name))
    assert "metadata.name" in fields_of(errors)


def test_name_longer_than_50_chars_fails() -> None:
    errors = validate_manifest(make_manifest(metadata__name="x" * 51))
    assert any(e.field == "metadata.name" and "50" in e.message for e in errors)


# --- enums ---------------------------------------------------------------------


@pytest.mark.parametrize("lifecycle", ["experimental", "production", "deprecated"])
def test_allowed_lifecycles(lifecycle: str) -> None:
    assert validate_manifest(make_manifest(spec__lifecycle=lifecycle)) == []


def test_invalid_lifecycle_fails() -> None:
    errors = validate_manifest(make_manifest(spec__lifecycle="retired"))
    assert "spec.lifecycle" in fields_of(errors)


@pytest.mark.parametrize("type_", ["website", "service", "tool"])
def test_allowed_types(type_: str) -> None:
    assert validate_manifest(make_manifest(spec__type=type_)) == []


def test_invalid_type_fails() -> None:
    errors = validate_manifest(make_manifest(spec__type="library"))
    assert "spec.type" in fields_of(errors)


# --- forward compatibility ------------------------------------------------------


def test_unknown_extra_fields_are_allowed() -> None:
    manifest = make_manifest()
    manifest["futureTopLevel"] = {"anything": True}
    manifest["metadata"]["labels"] = {"team": "core"}
    manifest["metadata"]["annotations"]["backstage.io/techdocs-ref"] = "dir:."
    manifest["spec"]["system"] = "billing"
    assert validate_manifest(manifest) == []


# --- name uniqueness -------------------------------------------------------------


def test_unique_names_produce_no_errors() -> None:
    manifests = [make_manifest(metadata__name=n) for n in ["app-a", "app-b", "app-c"]]
    assert check_name_uniqueness(manifests) == []


def test_duplicate_names_are_reported() -> None:
    manifests = [make_manifest(metadata__name=n) for n in ["app-a", "app-b", "app-a"]]
    errors = check_name_uniqueness(manifests)
    assert len(errors) == 1
    assert errors[0].field == "metadata.name"
    assert "'app-a'" in errors[0].message and "unique" in errors[0].message


def test_uniqueness_ignores_manifests_without_a_name() -> None:
    manifests = [make_manifest(), {"metadata": {}}, {"not-even": "metadata"}]
    assert check_name_uniqueness(manifests) == []


# --- ValidationError shape --------------------------------------------------------


def test_validation_error_carries_field_and_message() -> None:
    error = ValidationError(field="metadata.name", message="required field is missing or empty")
    assert error.field == "metadata.name"
    assert "missing" in error.message
    assert str(error) == "metadata.name: required field is missing or empty"
