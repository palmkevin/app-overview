"""Static tests for the search/filter/sort controls (issue #11).

The page is rendered in-process from the deterministic testrun fixtures and
assertions are made on the HTML string only: controls markup, the single
inline script with its wiring, and self-containment. Behavioral coverage
(clicking chips, typing in the search box) lands in #13 with Playwright —
deliberately NOT here.
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

#: Injected generation timestamp — deterministic rendering.
GENERATED_AT = datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)

LIFECYCLES = ("experimental", "production", "deprecated")
#: Every data-status value the cards can carry (Status enum values).
STATUSES = ("healthy", "degraded", "down", "not-deployed", "unknown")
SORT_OPTIONS = ("name", "status", "last-deploy", "lifecycle")


@pytest.fixture(scope="module")
def catalog():
    config = load_config(None)
    return aggregate(
        FakeRepoSource(TESTDATA_DIR),
        FakeRuntimeSource(TESTDATA_DIR, DEFAULT_SCENARIO),
        config,
        GENERATED_AT,
    )


@pytest.fixture(scope="module")
def html(catalog) -> str:
    return render_catalog(catalog)


@pytest.fixture(scope="module")
def script(html: str) -> str:
    """Body of the page's single <script> block at the end of <body>.

    Since issue #21 the page carries exactly one more script: the tiny
    blocking theme script in <head>, which must run before first paint and
    therefore cannot be merged into the body script.
    """
    blocks = re.findall(r"<script>(.*?)</script>", html, re.DOTALL)
    assert len(blocks) == 2, "expected the head theme script + ONE body script"
    body_blocks = [b for b in blocks if "app-cards" in b]
    assert len(body_blocks) == 1, "controls script not found"
    return body_blocks[0]


# --- controls markup ----------------------------------------------------------


def test_search_input_present(html):
    match = re.search(r'<input id="search"[^>]*>', html)
    assert match, "search input missing"
    assert 'type="search"' in match.group(0)
    assert "aria-label" in match.group(0)


def test_result_count_element_present_with_initial_count(html, catalog):
    match = re.search(r'<span id="result-count"[^>]*>([^<]*)</span>', html)
    assert match, "result-count element missing"
    n = len(catalog.entries)
    assert match.group(1).strip() == f"{n} of {n} apps"  # server-rendered initial value


def test_empty_state_message_present_and_hidden_by_default(html):
    match = re.search(r'<p id="empty-state"[^>]*>', html)
    assert match, "empty-state element missing"
    assert "hidden" in match.group(0)


@pytest.mark.parametrize("lifecycle", LIFECYCLES)
def test_lifecycle_chip_present(html, lifecycle):
    pattern = (
        r'<button type="button" class="chip" data-group="lifecycle"\s*'
        rf'data-value="{lifecycle}" aria-pressed="false">{lifecycle}</button>'
    )
    assert re.search(pattern, html), f"lifecycle chip {lifecycle!r} missing"


@pytest.mark.parametrize("status", STATUSES)
def test_status_chip_present(html, status):
    pattern = (
        r'<button type="button" class="chip" data-group="status"\s*'
        rf'data-value="{status}" aria-pressed="false">{status}</button>'
    )
    assert re.search(pattern, html), f"status chip {status!r} missing"


def test_status_chips_match_emitted_data_status_values(html):
    """Every data-status a card carries has a chip (no orphaned statuses)."""
    emitted = set(re.findall(r'data-status="([^"]+)"', html))
    chip_values = set(
        re.findall(r'data-group="status"\s*data-value="([^"]+)"', html)
    )
    assert emitted <= chip_values
    assert chip_values == set(STATUSES)


def test_sort_select_with_four_options(html):
    select = re.search(r'<select id="sort">(.*?)</select>', html, re.DOTALL)
    assert select, "sort select missing"
    values = re.findall(r'<option value="([^"]+)"', select.group(1))
    assert tuple(values) == SORT_OPTIONS
    assert re.search(r'<option value="name" selected>', select.group(1)), (
        "name must be the default sort"
    )


def test_active_chip_styling_hooks_present(html):
    """Active chips must be visually distinct via the aria-pressed state."""
    assert '.chip[aria-pressed="true"]' in html


# --- inline script wiring -----------------------------------------------------


def test_script_wires_up_all_controls(script):
    assert script.count("addEventListener") >= 3  # input, change, chip clicks
    assert '"input"' in script and '"change"' in script and '"click"' in script
    for element_id in ("app-cards", "search", "sort", "result-count", "empty-state"):
        assert f'getElementById("{element_id}")' in script, f"script never reads #{element_id}"


def test_script_uses_the_data_attribute_contract(script):
    for accessor in (
        "dataset.name",
        "dataset.status",
        "dataset.lifecycle",
        "dataset.lastDeploy",  # camelCase accessor for data-last-deploy
        "dataset.group",
        "dataset.value",
    ):
        assert accessor in script, f"script never reads {accessor}"
    # search haystack covers description, tags and owner too
    assert "dataset.description" in script
    assert "dataset.tags" in script
    assert "dataset.owner" in script


def test_script_documents_fixed_sort_orders(script):
    assert "STATUS_ORDER" in script and "LIFECYCLE_ORDER" in script
    assert '"down", "degraded", "unknown", "not-deployed", "healthy"' in script
    assert '"experimental", "production", "deprecated"' in script


def test_script_reorders_dom_and_toggles_visibility(script):
    assert ".sort(" in script  # comparator-based sorting
    assert "appendChild" in script  # DOM reorder, not CSS order
    assert ".hidden" in script or "hidden =" in script


def test_script_is_plain_vanilla_js(script):
    # No modules, no network, no libraries — must work from file://.
    for forbidden in ("import ", "require(", "fetch(", "XMLHttpRequest", "location."):
        assert forbidden not in script, f"forbidden construct in inline script: {forbidden!r}"


# --- self-containment (PRD acceptance criterion 5) ----------------------------


def test_no_external_script_or_src_anywhere(html):
    assert "<script src" not in html
    assert not re.search(r"<script[^>]+src\s*=", html, re.IGNORECASE)
    assert not re.search(
        r"<(?:script|img|iframe|source|video|audio|embed|object)[^>]*\ssrc\s*=\s*[\"']?https?://",
        html,
        re.IGNORECASE,
    )
