"""Tests for the CLI entrypoint and the MVP HTML rendering (issue #9).

The CLI is exercised in-process (``main()`` with an argv list) in testrun
mode — no network, deterministic fixtures from ``testdata/`` only.
"""

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from catalog_generator.cli import main

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Injected generation timestamp — the CLI's clock read is bypassed so the
#: whole run is deterministic.
GENERATED_AT = datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)

#: Resource-loading tags with an http(s) URL — forbidden (self-contained
#: page). Plain <a href="http..."> links to repos are fine.
EXTERNAL_RESOURCE = re.compile(
    r"<(?:script|img|iframe|source|video|audio|embed|object)[^>]*\s(?:src|data)\s*=\s*"
    r"[\"']?https?://",
    re.IGNORECASE,
)


@pytest.fixture
def outputs(tmp_path, monkeypatch):
    """Run the CLI once in default testrun mode; return (exit_code, out_dir)."""
    monkeypatch.chdir(REPO_ROOT)  # fixtures live in ./testdata
    out = tmp_path / "out"
    code = main(["--mode", "testrun", "--output", str(out)], generated_at=GENERATED_AT)
    return code, out


def test_writes_both_files_and_exits_zero(outputs):
    code, out = outputs
    assert code == 0
    assert (out / "catalog.html").is_file()
    assert (out / "catalog.json").is_file()


def test_html_contains_fixture_apps_and_statuses(outputs):
    _, out = outputs
    html = (out / "catalog.html").read_text(encoding="utf-8")
    # healthy, degraded, down, not-deployed, invalid apps from the fixtures
    for name in (
        "time-tracker",
        "pdf-renderer",  # degraded (1 of 3 ready)
        "ldap-sync",  # down (0 of 2 ready)
        "hr-portal",  # cataloged but not deployed
        "legacy-wiki",  # invalid YAML
        "metrics-dashboard",  # fails validation
    ):
        assert name in html, f"expected app {name!r} in the page"
    # status emoji actually rendered
    for emoji in ("\U0001f7e2", "\U0001f7e1", "\U0001f534", "⚪"):
        assert emoji in html
    assert "ready 2/2" in html  # time-tracker replicas
    assert "invalid metadata" in html  # marker for legacy-wiki/metrics-dashboard
    assert "of 13 repos cataloged" in html  # coverage stat in the header
    assert "2026-07-15 12:00" in html  # injected generated_at shown


def test_html_has_no_external_resource_loads(outputs):
    _, out = outputs
    html = (out / "catalog.html").read_text(encoding="utf-8")
    assert "<script src=" not in html
    assert not re.search(r"<link[^>]+stylesheet", html, re.IGNORECASE)
    assert not re.search(r"<img[^>]+src=[\"']https?://", html, re.IGNORECASE)
    assert not EXTERNAL_RESOURCE.search(html)
    assert "@import" not in html and "url(http" not in html  # no CSS-side loads


def test_runtime_unavailable_scenario_renders_banner_and_exits_zero(tmp_path, monkeypatch):
    monkeypatch.chdir(REPO_ROOT)
    out = tmp_path / "out"
    code = main(
        ["--mode", "testrun", "--scenario", "runtime-unavailable", "--output", str(out)],
        generated_at=GENERATED_AT,
    )
    assert code == 0
    html = (out / "catalog.html").read_text(encoding="utf-8")
    assert "Runtime data unavailable since" in html
    assert "2026-07-01" in html  # deterministic 'since' from the scenario fixture
    # the page still lists the apps from repo data
    assert "time-tracker" in html


def test_missing_testdata_dir_exits_nonzero(tmp_path, capsys):
    config = tmp_path / "config.yaml"
    config.write_text(f"testdata_dir: {tmp_path / 'does-not-exist'}\n", encoding="utf-8")
    code = main(
        ["--config", str(config), "--mode", "testrun", "--output", str(tmp_path / "out")],
        generated_at=GENERATED_AT,
    )
    assert code != 0
    assert "error" in capsys.readouterr().err


def test_unknown_scenario_exits_nonzero(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(REPO_ROOT)
    code = main(
        ["--mode", "testrun", "--scenario", "no-such-scenario", "--output", str(tmp_path / "out")],
        generated_at=GENERATED_AT,
    )
    assert code != 0
    assert "no-such-scenario" in capsys.readouterr().err


def test_json_output_matches_to_json_shape(outputs):
    _, out = outputs
    data = json.loads((out / "catalog.json").read_text(encoding="utf-8"))
    assert set(data) == {
        "schema_version",
        "generated_at",
        "runtime_available",
        "runtime_unavailable_since",
        "coverage",
        "apps",
        "gaps",
    }
    assert data["schema_version"] == 1
    assert data["generated_at"] == GENERATED_AT.isoformat()
    assert data["runtime_available"] is True
    assert data["coverage"]["total"] == 13
    assert data["coverage"]["cataloged"] == 12  # ops-scripts has no manifest
    names = [app["name"] for app in data["apps"]]
    assert "time-tracker" in names and "ldap-sync" in names
    statuses = {app["name"]: app["status"] for app in data["apps"]}
    assert statuses["ldap-sync"] == "down"
    assert statuses["pdf-renderer"] == "degraded"
    assert set(data["gaps"]) == {
        "deployed_uncataloged",
        "cataloged_not_deployed",
        "invalid_metadata",
    }


def test_logs_one_line_summary(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(REPO_ROOT)
    code = main(
        ["--mode", "testrun", "--output", str(tmp_path / "out")], generated_at=GENERATED_AT
    )
    assert code == 0
    err = capsys.readouterr().err
    lines = [line for line in err.splitlines() if line.strip()]
    assert len(lines) == 1
    assert "repos cataloged" in lines[0]
    assert "gaps:" in lines[0]
