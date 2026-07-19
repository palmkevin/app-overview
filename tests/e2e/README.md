# Browser e2e tests (issue #13)

Playwright (Chromium) tests that drive the **generated** `catalog.html` via a
`file://` URL — no web server and zero network, exactly how the self-contained
page must work.

## What is covered

PRD §9 acceptance criteria, in testrun mode (deterministic `testdata/` fixtures):

| Criterion | Test class | Checks |
|---|---|---|
| 1 | `TestCriterion1DefaultPage` | ≥1 healthy cataloged app card; invalid-metadata entries with their error text (legacy-wiki parse error, metrics-dashboard missing `spec.owner`); coverage stat counts the uncataloged repo ("12 of 13 repos cataloged"); "deployed but not cataloged" gap entry (status-page) |
| 3 | `TestCriterion3RuntimeUnavailable` | `--scenario runtime-unavailable`: all declared metadata still renders, plus the "runtime data unavailable" banner |
| 4 | `TestCriterion4SearchFilterSort` | search by tag, owner, and partial app name each filters correctly with **no page reload** (URL unchanged, no navigation events); lifecycle chip + status chip combine (AND); sort select reorders the cards |
| 5 | `TestCriterion5SelfContained` | every network request during page load is the `file://` document itself — zero external hosts (both scenarios) |

## Not covered here (verifiable only after the Bitbucket/Jenkins/k3s migration)

- **Criterion 2** — killing an app's pods flips its badge within one refresh
  cycle: needs a real cluster.
- **Criterion 6** — `validateCatalogInfo()` marks builds UNSTABLE/FAILED:
  needs the Jenkins shared-library step (issue #17, `migration`).
- **Criterion 7** — generator completes in < 2 min for 40 repos: needs the
  real Bitbucket project.

## Running locally

```bash
pip install -e .[dev]                    # includes playwright
playwright install chromium             # once, if no browser is available yet
pytest -m e2e
```

The suite is excluded from a plain `pytest` run (pyproject `addopts`
deselects the `e2e` marker). The session fixture in `conftest.py` runs the
generator (via the current Python interpreter) into a temp directory for both
scenarios and launches Chromium once. If the plain launch fails (browser
revision mismatch), it falls back to a preinstalled executable at
`/opt/pw-browsers/chromium` when present.

In CI (`.github/workflows/ci.yml`), a dedicated `e2e` job installs the
browser with `playwright install --with-deps chromium` and runs `pytest -m e2e`.
