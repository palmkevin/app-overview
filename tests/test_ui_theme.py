"""Tests for the light/dark theme markup (issue #21).

Static assertions on the rendered page: the blocking pre-paint theme script,
the header toggle button, the dark-theme CSS, and the self-containment /
determinism guarantees. Behavioral coverage (clicking the toggle, persistence
across reloads) lives in the browser suite (``tests/e2e/test_theme.py``).
"""

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


# --- pre-paint theme script ---------------------------------------------------


def test_blocking_theme_script_runs_before_body(html):
    """The theme must be applied in <head>, before any content paints."""
    script_pos = html.index('localStorage.getItem("catalog-theme")')
    assert script_pos < html.index("<body>")


def test_theme_script_falls_back_to_os_preference(html):
    assert "prefers-color-scheme: dark" in html
    assert 'setAttribute("data-theme"' in html


# --- toggle button --------------------------------------------------------------


def test_toggle_button_in_header_with_both_icons(html):
    header = html[html.index('<header id="header">') : html.index("</header>")]
    assert 'id="theme-toggle"' in header
    assert 'aria-label="Toggle dark/light theme"' in header
    assert 'class="icon-sun"' in header
    assert 'class="icon-moon"' in header


def test_toggle_persists_choice_to_local_storage(html):
    assert 'localStorage.setItem("catalog-theme", next)' in html


# --- dark theme CSS -------------------------------------------------------------


def test_dark_theme_tokens_present(html):
    """Material dark surfaces shared with Jenkins-UT-Analyzer."""
    assert '[data-theme="dark"]' in html
    assert "--bg: #121212" in html
    assert "--surface: #1e1e1e" in html


def test_color_scheme_follows_theme(html):
    """Native widgets (search box, select) must follow the active theme."""
    assert "color-scheme: light" in html
    assert "color-scheme: dark" in html


# --- hard rules still hold -------------------------------------------------------


def test_theme_assets_are_inline_only(html):
    """No external icon/CSS/JS was introduced (PRD acceptance criterion 5)."""
    assert "<script src=" not in html
    assert "<use href=" not in html and "xlink:href" not in html


def test_rendering_stays_deterministic():
    """Theme is client-side only — repeated renders are byte-identical."""
    assert _render() == _render()
