"""Fixtures for the browser e2e suite (issue #13).

The suite drives the *generated* ``catalog.html`` (loaded via a ``file://``
URL — no web server, no network) with Playwright Chromium, in both testrun
scenarios:

* default            — full fixture set, runtime data available
* runtime-unavailable — runtime API unreachable (banner case)

Browser resolution: a plain ``p.chromium.launch()`` works wherever a
matching browser build is installed (``PLAYWRIGHT_BROWSERS_PATH`` locally,
``playwright install --with-deps chromium`` in CI). If that fails — e.g. a
preinstalled browser revision that does not match the installed playwright
package — we fall back to the well-known preinstalled executable.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip(
    "playwright.sync_api",
    reason="playwright is not installed (pip install -e .[dev]); skipping e2e suite",
)

from playwright.sync_api import sync_playwright  # noqa: E402

#: Repo root — the generator must run with this cwd so it finds
#: ``config/config.yaml``, ``templates/`` and ``testdata/``.
REPO_ROOT = Path(__file__).resolve().parents[2]

#: Preinstalled Chromium fallback (sandbox images); only used when the
#: plain launch fails, see module docstring.
FALLBACK_CHROMIUM = Path("/opt/pw-browsers/chromium")


def _generate(output_dir: Path, scenario: str | None = None) -> Path:
    """Run the generator (testrun mode) into *output_dir*; return the HTML path."""
    cmd = [
        sys.executable,
        "-m",
        "catalog_generator",
        "--mode",
        "testrun",
        "--output",
        str(output_dir),
    ]
    if scenario:
        cmd += ["--scenario", scenario]
    result = subprocess.run(
        cmd, cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, (
        f"generator failed ({result.returncode}):\n{result.stdout}\n{result.stderr}"
    )
    html = output_dir / "catalog.html"
    assert html.is_file(), f"generator did not produce {html}"
    return html


@pytest.fixture(scope="session")
def default_page_url(tmp_path_factory: pytest.TempPathFactory) -> str:
    """file:// URL of the generated page, default scenario."""
    html = _generate(tmp_path_factory.mktemp("catalog-default"))
    return html.resolve().as_uri()


@pytest.fixture(scope="session")
def unavailable_page_url(tmp_path_factory: pytest.TempPathFactory) -> str:
    """file:// URL of the generated page, runtime-unavailable scenario."""
    html = _generate(
        tmp_path_factory.mktemp("catalog-runtime-unavailable"),
        scenario="runtime-unavailable",
    )
    return html.resolve().as_uri()


@pytest.fixture(scope="session")
def browser():
    """One Chromium instance for the whole session."""
    with sync_playwright() as pw:
        try:
            instance = pw.chromium.launch()
        except Exception:
            if not FALLBACK_CHROMIUM.exists():
                raise
            instance = pw.chromium.launch(executable_path=str(FALLBACK_CHROMIUM))
        yield instance
        instance.close()


@pytest.fixture
def page(browser):
    """A fresh page in an isolated context per test (no state bleed)."""
    context = browser.new_context()
    page = context.new_page()
    yield page
    context.close()
