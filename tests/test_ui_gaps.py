"""Tests for the gaps section of the catalog page (issue #12).

The page is rendered in-process from the deterministic testrun fixtures
(config -> fake sources -> aggregate -> render_catalog) — no CLI, no
network. The repo's default config (``config/config.yaml``) is used so the
ignore-list matches the published page (infra deployments such as the
ingress controller must not appear as gaps).

Assertions avoid hardcoded totals that depend on the full fixture set:
per-list counts are compared against the entries actually rendered.
"""

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from catalog_generator.aggregate import aggregate
from catalog_generator.config import load_config
from catalog_generator.render import render_catalog
from catalog_generator.sources.fake import DEFAULT_SCENARIO, FakeRepoSource, FakeRuntimeSource

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTDATA_DIR = REPO_ROOT / "testdata"
CONFIG_FILE = REPO_ROOT / "config" / "config.yaml"

#: Injected generation timestamp — deterministic rendering.
GENERATED_AT = datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)

EMPTY_STATE = "No gaps 🎉"


def _render(scenario: str = DEFAULT_SCENARIO) -> str:
    config = load_config(CONFIG_FILE)
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


def gaps_section(html: str) -> str:
    """The full <section id="gaps"> markup."""
    match = re.search(r'<section id="gaps">.*?</section>', html, re.DOTALL)
    assert match, "gaps section not found"
    return match.group(0)


def gap_card(html: str, card_id: str) -> str:
    """One gap sub-list card by its id."""
    match = re.search(
        rf'<article[^>]*id="{re.escape(card_id)}"[^>]*>.*?</article>',
        html,
        re.DOTALL,
    )
    assert match, f"gap card {card_id!r} not found"
    return match.group(0)


def heading_count(card_html: str) -> int:
    """The per-sub-list count shown in the card's heading."""
    match = re.search(r'<span class="gap-count">(\d+)</span>', card_html)
    assert match, "no count found in the gap heading"
    return int(match.group(1))


def entry_count(card_html: str) -> int:
    """Number of rendered entries (list items) in a gap card."""
    return len(re.findall(r"<li[\s>]", card_html))


# --- deployed but not cataloged ------------------------------------------------


def test_deployed_uncataloged_lists_status_page(html):
    card = gap_card(html, "gap-deployed-uncataloged")
    assert "Deployed but not cataloged" in card
    assert "tools/status-page" in card
    assert "ready 1/1" in card  # runtime detail shown per entry


def test_deployed_uncataloged_count_matches_entries(html):
    card = gap_card(html, "gap-deployed-uncataloged")
    assert heading_count(card) == entry_count(card) >= 1


def test_deployed_uncataloged_respects_ignore_list(html):
    # Infra workloads on the configured ignore list never surface as gaps.
    card = gap_card(html, "gap-deployed-uncataloged")
    assert "nginx-ingress" not in card
    assert "cert-manager" not in card


# --- cataloged but not deployed ------------------------------------------------


def test_cataloged_not_deployed_lists_hr_portal_with_repo_link(html):
    card = gap_card(html, "gap-cataloged-not-deployed")
    assert "Cataloged but not deployed" in card
    assert re.search(r'<a href="[^"]*/hr-portal">hr-portal</a>', card)


def test_cataloged_not_deployed_count_matches_entries(html):
    card = gap_card(html, "gap-cataloged-not-deployed")
    assert heading_count(card) == entry_count(card) >= 1


def test_experimental_apps_are_not_cataloged_not_deployed_gaps(html):
    # chat-prototype is experimental and not deployed — by design, not a gap.
    card = gap_card(html, "gap-cataloged-not-deployed")
    assert "chat-prototype" not in card


# --- invalid metadata -----------------------------------------------------------


def test_invalid_metadata_lists_repos_with_links_and_errors(html):
    card = gap_card(html, "gap-invalid-metadata")
    assert "Invalid metadata" in card
    assert re.search(r'<a href="[^"]*/legacy-wiki">legacy-wiki</a>', card)
    assert re.search(r'<a href="[^"]*/metrics-dashboard">metrics-dashboard</a>', card)
    assert "YAML parse error" in card  # legacy-wiki's parse error
    assert "spec.owner" in card  # metrics-dashboard's validation error


def test_invalid_metadata_count_matches_entries(html):
    card = gap_card(html, "gap-invalid-metadata")
    assert heading_count(card) == entry_count(card) >= 2


# --- always visible, no interaction required -----------------------------------


def test_gaps_section_visible_without_interaction(html):
    section = gaps_section(html)
    assert "<details" not in section  # no collapse/toggle wrapper
    # No element inside the section carries a hidden attribute.
    assert not re.search(r"<\w+[^>]*\shidden[\s>]", section)
    # No CSS rule hides the section or its parts.
    for selector in ("#gaps", ".gap-"):
        assert not re.search(
            rf"{re.escape(selector)}[^{{}}]*\{{[^}}]*display\s*:\s*none",
            html,
        ), f"a CSS rule hides {selector}"


def test_gap_cards_use_soft_warning_tones(html):
    assert 'class="gap-card tone-amber" id="gap-deployed-uncataloged"' in html
    assert 'class="gap-card tone-amber" id="gap-cataloged-not-deployed"' in html
    assert 'class="gap-card tone-red" id="gap-invalid-metadata"' in html


# --- empty ("no gaps") state ----------------------------------------------------


def test_runtime_unavailable_renders_no_gaps_state(html_unavailable):
    # Runtime-dependent gap lists are empty when the runtime API is down.
    for card_id in ("gap-deployed-uncataloged", "gap-cataloged-not-deployed"):
        card = gap_card(html_unavailable, card_id)
        assert heading_count(card) == 0
        assert entry_count(card) == 0
        assert EMPTY_STATE in card
    # Invalid metadata is repo-side and still reported.
    invalid = gap_card(html_unavailable, "gap-invalid-metadata")
    assert EMPTY_STATE not in invalid
    assert heading_count(invalid) == entry_count(invalid) >= 2


def test_empty_state_is_green(html):
    # The all-clear pill is styled green (consistent with the healthy badge).
    match = re.search(r"\.gap-empty\s*\{([^}]*)\}", html)
    assert match, ".gap-empty rule missing"
    assert "#1b6e2f" in match.group(1)  # green text
    assert "#e6f5e9" in match.group(1)  # green background


def test_no_empty_state_when_all_lists_have_entries(html):
    assert EMPTY_STATE not in gaps_section(html)
