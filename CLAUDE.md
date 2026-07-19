# CLAUDE.md — App Catalog

Guidance for Claude Code (and its sub-agents) working in this repository.

## What this is

A statically generated catalog page for our internal web apps. A Python generator
aggregates repo metadata (`catalog-info.yaml` per app repo) and Kubernetes runtime
state into one self-contained `catalog.html`. Full spec: **[PRD.md](PRD.md)**.
Binding decisions made with the product owner: **[docs/decisions.md](docs/decisions.md)** —
read both before implementing anything.

## Current phase: GitHub / testrun

The company infrastructure (Bitbucket, Jenkins, Rancher/k3s) is NOT reachable.
The repo temporarily lives on GitHub and will migrate later. Therefore:

- The generator has two modes: **`testrun`** (fake adapters + deterministic fixtures
  in `testdata/`, no network) and **`live`** (real Bitbucket/k8s adapters, unusable
  until migration). Everything downstream of the source interfaces is identical.
- The demo page is generated in testrun mode and published to GitHub Pages
  (`https://palmkevin.github.io/app-overview/`) by `.github/workflows/pages.yml`.
- Issues labeled **`migration`** must NOT be implemented now (#16 is manifests-only,
  #17/#18 are fully deferred).

## Implementation plan

The plan is the GitHub issue tracker (18 issues, labels `M1-schema`, `M2-generator`,
`M3-ui`, `M4-rollout`, `migration`). Each issue lists its scope, acceptance criteria,
**owned files**, and `Depends on: #N`. Rules:

- Respect the dependency order. Parallel work is fine only across issues whose
  owned-files sets don't overlap.
- Stay inside an issue's owned files; coordinate (or sequence) when touching shared
  files like `templates/catalog.html.j2` or `README.md`.
- Dependency waves: #1 → #2/#4 → #5 → #6/#7/#8 → #9 → #10 → #11/#12/#15 → #13/#14/#16.

## Hard rules (from PRD + decisions — do not violate)

- Python 3.14; runtime deps only `PyYAML`, `Jinja2`, `requests`; dev: `pytest`, `ruff`
  (+ Playwright for e2e, #13).
- `catalog.html` must be fully self-contained: inline CSS/JS, **zero external
  requests** (no CDN, no webfonts). PRD acceptance criterion 5.
- The annotation prefix (`ourcompany.io` default) is **configurable** — never
  hardcode it in code, templates, or tests.
- Deployment matching only via the declared k8s annotations — never guess by name.
- Validation rules live ONLY in `catalog_generator/validation.py` (single source of
  truth for the future Jenkins step). No duplicated rule logic anywhere.
- Testrun fixtures are fully deterministic: no randomness, no clock reads except an
  injectable `generated_at`.
- One broken manifest must never break a generator run; runtime-API failure still
  renders the page (banner) and exits 0. Non-zero exit only when the repo source is
  entirely unreachable.
- Secrets (tokens) come from env vars only, never from config files. The generated
  page must never contain secrets, env vars, or full pod specs.
- No real company hostnames/tokens anywhere — placeholders only until migration.
- Tests must never make live network calls (mock HTTP for the real adapters).

## Commands

```bash
pip install -e .[dev]                                    # setup
pytest                                                   # unit tests
pytest -m e2e                                            # browser e2e (after #13)
ruff check .                                             # lint
python -m catalog_generator --mode testrun --output out/ # generate the page
python -m catalog_generator --mode testrun --scenario runtime-unavailable --output out/
```

CI (`.github/workflows/ci.yml`) runs ruff + pytest; `pages.yml` publishes the
testrun page on push to `main` and manual dispatch (no cron — decided).

## Conventions

- Kebab-case app names; Backstage-compatible manifest schema (PRD §6.1) — keep the
  migration path to Backstage open (forward-compatible: unknown fields allowed).
- Keep the generator small and boring (PRD suggests ~300 lines of core logic);
  prefer stdlib + the three allowed deps over new dependencies.
- When an issue is done: all its acceptance-criteria checkboxes are satisfiable,
  tests green, then reference the issue number in the commit message
  (e.g. `Implement validation module (#2)`).
