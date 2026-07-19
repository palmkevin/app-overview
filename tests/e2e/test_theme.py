"""Browser e2e tests for the light/dark theme toggle (issue #21).

Same harness as ``test_acceptance.py``: the generated page is loaded from a
``file://`` URL and everything runs client-side — no server, no network.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e

TOGGLE = "#theme-toggle"


def theme(page) -> str:
    return page.evaluate("document.documentElement.getAttribute('data-theme')")


def body_background(page) -> str:
    return page.evaluate("getComputedStyle(document.body).backgroundColor")


class TestThemeToggle:
    def test_defaults_to_light_without_saved_choice(self, page, default_page_url):
        page.goto(default_page_url)  # Playwright's default emulation is light
        assert theme(page) == "light"

    def test_toggle_switches_theme_and_restyles_page(self, page, default_page_url):
        page.goto(default_page_url)
        light_bg = body_background(page)
        page.click(TOGGLE)
        assert theme(page) == "dark"
        assert body_background(page) == "rgb(18, 18, 18)"  # #121212, UT-Analyzer dark
        page.click(TOGGLE)
        assert theme(page) == "light"
        assert body_background(page) == light_bg

    def test_choice_persists_across_reloads(self, page, default_page_url):
        page.goto(default_page_url)
        page.click(TOGGLE)
        assert page.evaluate("localStorage.getItem('catalog-theme')") == "dark"
        page.reload()
        assert theme(page) == "dark"

    def test_first_visit_follows_os_dark_preference(self, browser, default_page_url):
        context = browser.new_context(color_scheme="dark")
        page = context.new_page()
        try:
            page.goto(default_page_url)
            assert theme(page) == "dark"
            assert body_background(page) == "rgb(18, 18, 18)"
        finally:
            context.close()

    def test_saved_choice_beats_os_preference(self, browser, default_page_url):
        context = browser.new_context(color_scheme="dark")
        page = context.new_page()
        try:
            page.goto(default_page_url)
            page.click(TOGGLE)  # explicitly back to light, saved to localStorage
            assert theme(page) == "light"
            page.reload()
            assert theme(page) == "light"
        finally:
            context.close()
