"""Dogfood guard (issue #15): the catalog's own manifest stays valid.

The root ``catalog-info.yaml`` describes this repo itself. It must always
parse and pass validation with the default annotation prefix (the prefix
literal is never written out here — the default comes from the validation
module, the single source of truth). A second check keeps the testdata
fixture copy in sync with the root manifest.
"""

from pathlib import Path

from catalog_generator.validation import parse_manifest, validate_manifest

REPO_ROOT = Path(__file__).resolve().parent.parent
ROOT_MANIFEST = REPO_ROOT / "catalog-info.yaml"
FIXTURE_MANIFEST = REPO_ROOT / "testdata" / "repos" / "app-catalog" / "catalog-info.yaml"


def test_root_manifest_parses_and_validates_with_default_prefix():
    manifest, parse_errors = parse_manifest(ROOT_MANIFEST.read_text(encoding="utf-8"))
    assert parse_errors == []
    assert manifest is not None
    assert validate_manifest(manifest) == []  # default prefix — zero errors


def test_root_manifest_identifies_this_repo():
    manifest, _ = parse_manifest(ROOT_MANIFEST.read_text(encoding="utf-8"))
    assert manifest["metadata"]["name"] == "app-catalog"
    assert manifest["spec"] == {"type": "tool", "lifecycle": "production", "owner": "palmkevin"}
    urls = {link["url"] for link in manifest["metadata"]["links"]}
    assert "https://palmkevin.github.io/app-overview/" in urls
    assert "https://github.com/palmkevin/app-overview" in urls


def test_fixture_copy_matches_root_manifest():
    root, _ = parse_manifest(ROOT_MANIFEST.read_text(encoding="utf-8"))
    fixture, _ = parse_manifest(FIXTURE_MANIFEST.read_text(encoding="utf-8"))
    assert fixture["metadata"]["name"] == root["metadata"]["name"]
    assert fixture["metadata"]["annotations"] == root["metadata"]["annotations"]
    assert fixture["spec"] == root["spec"]
