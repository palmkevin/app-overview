"""Tests for the catalog page markup (issue #10): cards, badges, banner.

The page is rendered in-process from the deterministic testrun fixtures
(config -> fake sources -> aggregate -> render_catalog) — no CLI, no
network, no files. Assertions target the stable DOM contract that issue
#11 (search/filter/sort) and #12 (gaps) build on: the ``data-*``
attributes per card and the ``#controls`` / ``#gaps`` anchors.
"""

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from catalog_generator.aggregate import aggregate
from catalog_generator.config import load_config
from catalog_generator.model import (
    AppEntry,
    CatalogData,
    Coverage,
    Gaps,
    Manifest,
    Repo,
    Status,
)
from catalog_generator.render import render_catalog
from catalog_generator.sources.fake import DEFAULT_SCENARIO, FakeRepoSource, FakeRuntimeSource

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTDATA_DIR = REPO_ROOT / "testdata"

#: Injected generation timestamp — deterministic rendering.
GENERATED_AT = datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)

#: Resource-loading tags with an http(s) URL — forbidden (self-contained
#: page). Plain <a href="http..."> links to repos are fine.
EXTERNAL_RESOURCE = re.compile(
    r"<(?:script|img|iframe|source|video|audio|embed|object)[^>]*\s(?:src|data)\s*=\s*"
    r"[\"']?https?://",
    re.IGNORECASE,
)


def _render(scenario: str = DEFAULT_SCENARIO) -> str:
    config = load_config(None)
    catalog = aggregate(
        FakeRepoSource(TESTDATA_DIR),
        FakeRuntimeSource(TESTDATA_DIR, scenario),
        config,
        GENERATED_AT,
    )
    return render_catalog(catalog)


@pytest.fixture(scope="module")
def html() -> str:
    return _render()


@pytest.fixture(scope="module")
def html_unavailable() -> str:
    return _render("runtime-unavailable")


def card(html: str, name: str) -> str:
    """Extract the full <article> card markup for one app by data-name."""
    match = re.search(
        rf'<article class="card"[^>]*data-name="{re.escape(name)}".*?</article>',
        html,
        re.DOTALL,
    )
    assert match, f"no card found for app {name!r}"
    return match.group(0)


def attr(card_html: str, name: str) -> str:
    """Value of an attribute on the card's opening <article> tag."""
    match = re.search(rf'{re.escape(name)}="([^"]*)"', card_html)
    assert match, f"attribute {name!r} not found on card"
    return match.group(1)


# --- header ------------------------------------------------------------------


def test_header_stats(html):
    assert "<h1>App Catalog</h1>" in html
    assert "12 apps" in html
    assert "12 of 13 repos cataloged" in html
    assert "(92%)" in html
    assert "generated 2026-07-15 12:00 UTC" in html


def test_controls_placeholder_and_gaps_anchor_present(html):
    assert 'id="controls"' in html  # issue #11 mounts here
    assert '<section id="gaps">' in html  # issue #12 restyles this


# --- healthy app card --------------------------------------------------------


def test_healthy_card_shows_all_fields(html):
    tracker = card(html, "time-tracker")
    assert "Internal time tracking for project billing" in tracker
    assert "\U0001f7e2" in tracker and "healthy" in tracker  # status badge
    assert "ready 2/2" in tracker
    assert 'class="badge lifecycle lifecycle-production"' in tracker
    assert "jane.doe" in tracker
    assert "mailto:" not in tracker  # plain-text owner (not an email)
    assert "1.8.2" in tracker  # deployed version (image tag)
    assert "2026-06-12 10:02" in tracker  # last deploy
    assert "2026-06-12 09:41" in tracker  # last commit
    # links: App URL + extra manifest link + repo + README (has_readme)
    assert 'href="https://time.internal.example.com"' in tracker
    assert "App URL" in tracker and "Runbook" in tracker
    assert 'href="https://git.internal.example.com/projects/TOOLS/repos/time-tracker"' in tracker
    assert ">README</a>" in tracker


def test_tags_rendered_as_chips(html):
    tracker = card(html, "time-tracker")
    assert '<li class="tag">react</li>' in tracker
    assert '<li class="tag">internal-tool</li>' in tracker


def test_card_without_readme_has_no_readme_link(html):
    # asset-inventory's repo fixture has has_readme: false
    assert ">README</a>" not in card(html, "asset-inventory")


# --- machine-readable data-* contract (issue #11) ----------------------------


def test_data_attributes_on_known_card(html):
    tracker = card(html, "time-tracker")
    assert attr(tracker, "data-name") == "time-tracker"
    assert attr(tracker, "data-description") == "Internal time tracking for project billing"
    assert attr(tracker, "data-tags") == "react internal-tool"
    assert attr(tracker, "data-owner") == "jane.doe"
    assert attr(tracker, "data-lifecycle") == "production"
    assert attr(tracker, "data-status") == "healthy"
    assert attr(tracker, "data-last-deploy") == "2026-06-12T10:02:00+00:00"


def test_data_attributes_empty_when_unknown(html):
    # legacy-wiki's manifest is unparseable: fields fall back to empty,
    # data-name falls back to the repo name.
    wiki = card(html, "legacy-wiki")
    assert attr(wiki, "data-name") == "legacy-wiki"
    assert attr(wiki, "data-description") == ""
    assert attr(wiki, "data-tags") == ""
    assert attr(wiki, "data-owner") == ""
    assert attr(wiki, "data-lifecycle") == ""
    assert attr(wiki, "data-status") == "unknown"
    assert attr(wiki, "data-last-deploy") == ""


def test_every_card_carries_the_full_data_contract(html):
    cards = re.findall(r'<article class="card"[^>]*>', html)
    assert len(cards) == 12
    for opening_tag in cards:
        for name in (
            "data-name",
            "data-description",
            "data-tags",
            "data-owner",
            "data-lifecycle",
            "data-status",
            "data-last-deploy",
        ):
            assert f'{name}="' in opening_tag, f"{name} missing on {opening_tag}"


# --- invalid metadata --------------------------------------------------------


def test_invalid_metadata_badge_with_error_summary(html):
    wiki = card(html, "legacy-wiki")
    assert ">invalid metadata</span>" in wiki
    assert "YAML parse error" in wiki  # the parse-error summary is shown
    dashboard = card(html, "metrics-dashboard")
    assert ">invalid metadata</span>" in dashboard
    assert "spec.owner" in dashboard  # validation-error summary


# --- status / lifecycle badges ----------------------------------------------


def test_ready_counts_for_degraded_and_down(html):
    renderer = card(html, "pdf-renderer")
    assert attr(renderer, "data-status") == "degraded"
    assert "ready 1/3" in renderer
    assert "\U0001f7e1" in renderer
    sync = card(html, "ldap-sync")
    assert attr(sync, "data-status") == "down"
    assert "ready 0/2" in sync
    assert "\U0001f534" in sync


def test_not_deployed_card_has_no_replica_text(html):
    prototype = card(html, "chat-prototype")
    assert attr(prototype, "data-status") == "not-deployed"
    assert "⚪" in prototype
    assert "ready " not in prototype


def test_all_lifecycle_badges_present(html):
    assert 'class="badge lifecycle lifecycle-production"' in card(html, "time-tracker")
    assert 'class="badge lifecycle lifecycle-experimental"' in card(html, "chat-prototype")
    assert 'class="badge lifecycle lifecycle-deprecated"' in card(html, "meeting-room-booker")


def test_owner_rendered_as_mailto_only_for_email_values():
    """An owner value that looks like an email becomes a mailto: link."""
    entry = AppEntry(
        repo=Repo(name="mail-app", url="https://repo.example.com/mail-app", default_branch="main"),
        manifest=Manifest(
            name="mail-app",
            description="Owner-by-email fixture",
            lifecycle="experimental",
            owner="jane.doe@example.com",
        ),
        status=Status.NOT_DEPLOYED,
    )
    html = render_catalog(
        CatalogData(
            entries=(entry,),
            gaps=Gaps(),
            coverage=Coverage(cataloged=1, total=1),
            generated_at=GENERATED_AT,
        )
    )
    assert '<a href="mailto:jane.doe@example.com">jane.doe@example.com</a>' in html


# --- runtime-unavailable banner ----------------------------------------------


def test_banner_absent_in_default_scenario(html):
    assert "Runtime data unavailable" not in html
    assert 'id="runtime-banner"' not in html


def test_banner_present_in_runtime_unavailable_scenario(html_unavailable):
    assert 'id="runtime-banner"' in html_unavailable
    assert "Runtime data unavailable since" in html_unavailable
    assert "2026-07-01 05:30" in html_unavailable  # scenario's 'since'
    # the page still renders the apps from repo data
    assert card(html_unavailable, "time-tracker")
    assert attr(card(html_unavailable, "time-tracker"), "data-status") == "unknown"


# --- self-containment (PRD acceptance criterion 5) ----------------------------


@pytest.mark.parametrize("scenario", [DEFAULT_SCENARIO, "runtime-unavailable"])
def test_no_external_resource_references(scenario):
    html = _render(scenario)
    assert "<script src=" not in html
    assert not re.search(r"<link[^>]+stylesheet", html, re.IGNORECASE)
    assert not re.search(r"<img[^>]+src=[\"']https?://", html, re.IGNORECASE)
    assert not EXTERNAL_RESOURCE.search(html)
    assert "@import" not in html and "url(http" not in html  # no CSS-side loads
