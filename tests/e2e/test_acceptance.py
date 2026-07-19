"""Browser e2e tests for PRD §9 acceptance criteria 1, 3, 4 and 5 (testrun mode).

All expected values below mirror the deterministic fixtures in ``testdata/``
(see docs/decisions.md — same input, same output on every run):

* 13 repos, 12 cataloged (ops-scripts has no manifest) → "12 of 13 repos cataloged"
* invalid metadata: legacy-wiki (YAML parse error),
  metrics-dashboard (spec.owner missing)
* deployed-but-uncataloged workload: status-page
* cataloged-but-not-deployed (production): hr-portal

The page is loaded from a ``file://`` URL; every interaction is client-side
JS in the self-contained HTML — no server, no network.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e

CARD = "#app-cards article.card"
VISIBLE_CARD = f"{CARD}:not([hidden])"

#: All 12 cataloged app names, i.e. one card each (12 of 13 repos).
ALL_APPS = {
    "app-catalog",
    "asset-inventory",
    "chat-prototype",
    "holiday-planner",
    "hr-portal",
    "invoice-portal",
    "ldap-sync",
    "legacy-wiki",
    "meeting-room-booker",
    "metrics-dashboard",
    "pdf-renderer",
    "time-tracker",
}


def visible_names(page) -> list[str]:
    """data-name of the currently visible cards, in DOM (= sorted) order."""
    return page.eval_on_selector_all(
        VISIBLE_CARD, "cards => cards.map(c => c.dataset.name)"
    )


def track_navigations(page) -> list[str]:
    """Record main-frame navigations happening AFTER the initial load."""
    navigations: list[str] = []
    page.on(
        "framenavigated",
        lambda frame: navigations.append(frame.url)
        if frame == page.main_frame
        else None,
    )
    return navigations


# --------------------------------------------------------------------------
# Criterion 1 — cataloged/invalid/uncataloged/gap entries on the default page
# --------------------------------------------------------------------------


class TestCriterion1DefaultPage:
    def test_healthy_cataloged_app_card_present(self, page, default_page_url):
        page.goto(default_page_url)
        healthy = page.locator(f'{CARD}[data-status="healthy"]')
        assert healthy.count() >= 1
        # A healthy card shows declared metadata AND observed runtime state.
        card = page.locator(f'{CARD}[data-name="time-tracker"]')
        assert card.locator(".badge.status-healthy").inner_text().endswith("healthy")
        assert "ready" in card.locator(".ready").inner_text()

    def test_invalid_metadata_entries_with_error_text(self, page, default_page_url):
        page.goto(default_page_url)
        gap = page.locator("#gap-invalid-metadata")
        assert gap.is_visible()
        text = gap.inner_text()
        assert "legacy-wiki" in text
        assert "YAML parse error" in text
        assert "metrics-dashboard" in text
        assert "spec.owner" in text
        # The affected cards carry the badge + error summary too.
        for name in ("legacy-wiki", "metrics-dashboard"):
            card = page.locator(f'{CARD}[data-name="{name}"]')
            assert card.locator(".badge.invalid").inner_text() == "invalid metadata"
            assert card.locator(".error").first.inner_text().strip()

    def test_coverage_stat_counts_uncataloged_repo(self, page, default_page_url):
        page.goto(default_page_url)
        stats = page.locator("#stats").inner_text()
        assert "12 of 13 repos cataloged" in stats

    def test_deployed_but_not_cataloged_gap_entry(self, page, default_page_url):
        page.goto(default_page_url)
        gap = page.locator("#gap-deployed-uncataloged")
        assert gap.is_visible()
        assert "status-page" in gap.inner_text()


# --------------------------------------------------------------------------
# Criterion 3 — runtime API unreachable: metadata still renders, plus banner
# --------------------------------------------------------------------------


class TestCriterion3RuntimeUnavailable:
    def test_banner_shown(self, page, unavailable_page_url):
        page.goto(unavailable_page_url)
        banner = page.locator("#runtime-banner")
        assert banner.is_visible()
        assert "Runtime data unavailable since" in banner.inner_text()

    def test_all_declared_metadata_still_rendered(self, page, unavailable_page_url):
        page.goto(unavailable_page_url)
        assert set(visible_names(page)) == ALL_APPS
        # Declared metadata (description, owner, tags) is intact on a card.
        card = page.locator(f'{CARD}[data-name="time-tracker"]')
        assert card.locator(".desc").inner_text().strip()
        assert card.locator(".owner").inner_text().strip() not in ("", "—")
        assert card.locator(".tag").count() >= 1

    def test_banner_absent_on_default_page(self, page, default_page_url):
        page.goto(default_page_url)
        assert page.locator("#runtime-banner").count() == 0


# --------------------------------------------------------------------------
# Criterion 4 — search / filter / sort, all client-side (no page reload)
# --------------------------------------------------------------------------


class TestCriterion4SearchFilterSort:
    def test_search_by_tag(self, page, default_page_url):
        page.goto(default_page_url)
        navigations = track_navigations(page)
        page.fill("#search", "finance")  # tag of invoice-portal
        assert visible_names(page) == ["invoice-portal"]
        assert page.url == default_page_url
        assert navigations == []

    def test_search_by_owner(self, page, default_page_url):
        page.goto(default_page_url)
        navigations = track_navigations(page)
        page.fill("#search", "jane.doe")  # owner of time-tracker
        assert visible_names(page) == ["time-tracker"]
        assert page.url == default_page_url
        assert navigations == []

    def test_search_by_partial_app_name(self, page, default_page_url):
        page.goto(default_page_url)
        navigations = track_navigations(page)
        page.fill("#search", "invoi")
        # Substring search across name/description/tags/owner: matches the
        # invoice-portal name AND pdf-renderer (description mentions invoices).
        assert set(visible_names(page)) == {"invoice-portal", "pdf-renderer"}
        assert "invoice-portal" in visible_names(page)
        # Clearing the search restores every card — still without a reload.
        page.fill("#search", "")
        assert set(visible_names(page)) == ALL_APPS
        assert page.url == default_page_url
        assert navigations == []

    def test_lifecycle_and_status_chips_combine(self, page, default_page_url):
        page.goto(default_page_url)
        navigations = track_navigations(page)
        production_chip = page.locator('.chip[data-group="lifecycle"][data-value="production"]')
        healthy_chip = page.locator('.chip[data-group="status"][data-value="healthy"]')

        production_chip.click()
        assert production_chip.get_attribute("aria-pressed") == "true"
        production_only = set(visible_names(page))
        assert production_only == {
            "app-catalog",
            "asset-inventory",
            "holiday-planner",
            "hr-portal",
            "invoice-portal",
            "ldap-sync",
            "metrics-dashboard",
            "pdf-renderer",
            "time-tracker",
        }

        healthy_chip.click()  # AND between chip groups
        assert set(visible_names(page)) == {
            "app-catalog",
            "asset-inventory",
            "holiday-planner",
            "invoice-portal",
            "time-tracker",
        }
        assert page.locator("#result-count").inner_text() == "5 of 12 apps"
        assert page.url == default_page_url
        assert navigations == []

    def test_sort_by_status_reorders_cards(self, page, default_page_url):
        page.goto(default_page_url)
        navigations = track_navigations(page)
        # Default sort: name ascending.
        assert visible_names(page)[0] == "app-catalog"
        page.select_option("#sort", "status")
        # Status sort puts the worst problems first: the down app leads.
        reordered = visible_names(page)
        assert reordered[0] == "ldap-sync"
        assert set(reordered) == ALL_APPS  # reordered, nothing filtered out
        assert page.url == default_page_url
        assert navigations == []


# --------------------------------------------------------------------------
# Criterion 5 — zero external network requests (fully self-contained page)
# --------------------------------------------------------------------------


class TestCriterion5SelfContained:
    @pytest.mark.parametrize("url_fixture", ["default_page_url", "unavailable_page_url"])
    def test_page_load_makes_no_external_requests(self, page, request, url_fixture):
        url = request.getfixturevalue(url_fixture)
        requests_seen: list[str] = []
        page.on("request", lambda req: requests_seen.append(req.url))
        page.goto(url, wait_until="networkidle")
        assert requests_seen, "expected at least the document request itself"
        external = [u for u in requests_seen if u != url]
        assert external == [], f"unexpected requests beyond the file:// document: {external}"
