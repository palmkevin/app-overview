# App Catalog — Decisions & Testrun-Mode Spec

This document records the decisions made with the product owner (Kevin Palm) during
planning, on top of [PRD.md](../PRD.md). It is the reference for anyone (human or
agent) implementing the GitHub issues.

## Confirmed decisions

| Topic | Decision |
|---|---|
| Hosting (GitHub phase) | GitHub Pages, deployed by a GitHub Actions workflow. Target URL: `https://palmkevin.github.io/app-overview/` |
| Workflow trigger | On push to `main` + `workflow_dispatch` only — **no cron** during the GitHub phase |
| Jenkins `validateCatalogInfo()` step (PRD 6.4) | **Deferred to the Bitbucket/Jenkins migration.** Only the shared Python validation module is built now; the Groovy step will wrap it later |
| Annotation prefix | Configurable (config key `annotation_prefix`), default placeholder `ourcompany.io`. Never hardcode the prefix in code, templates, or tests |
| Real API adapters | Real Bitbucket REST + Kubernetes API adapters are written **now**, behind the same interface as the fakes, unit-tested with mocked HTTP only |
| Testrun fake data | **Fully deterministic** fixtures — same input, same output on every run |
| Stack | Python 3.14; PyYAML, Jinja2, requests; pytest for tests; ruff for lint |

## Operating modes

The generator has a mode switch (config key `mode`, CLI `--mode`):

- **`testrun`** — data sources are fake adapters that read deterministic fixtures
  from `testdata/`. No network access. Used for: the GitHub-phase hosted demo page,
  CI, and e2e tests. Exercises the *full* pipeline (aggregation, status derivation,
  rendering) — only the source layer is faked.
- **`live`** — data sources are the real Bitbucket REST and Kubernetes API adapters.
  Used after migration to the company infrastructure. Cannot be exercised until then.

Everything downstream of the source interfaces must be identical in both modes.

## Testrun fixture scenarios (required coverage)

The deterministic fixture set in `testdata/` must contain at least:

1. A **valid, healthy** app (valid manifest, deployment ready x/x) — several of these to make the page look realistic.
2. An app with **invalid YAML** in `catalog-info.yaml` (parse error → "invalid metadata" badge).
3. An app with a manifest that **fails validation** (e.g. missing required field).
4. A repo with **no manifest** (counts as uncataloged in coverage).
5. A **deployed-but-uncataloged** workload (Deployment with no matching repo).
6. A **degraded** app (0 < ready < desired).
7. A **down** app (ready == 0).
8. An **experimental** app that is **not deployed** (allowed, ⚪ badge, no gap entry).
9. A **production** app that is **not deployed** (gap: "cataloged but not deployed").
10. A fixture flag to simulate the **runtime API being unreachable** (banner case, PRD acceptance criterion 3).

These map directly onto PRD §9 acceptance criteria 1 and 3.

## GitHub-phase deliverable

A GitHub Actions workflow runs tests, generates the page in testrun-mode, and
publishes it to GitHub Pages so the HTML is reachable at a GitHub URL. GitHub
Pages must be configured with source **"GitHub Actions"** (the workflow attempts
`actions/configure-pages` with `enablement: true`; if the token lacks permission,
enable it once manually in *Settings → Pages*).

## Issue conventions

- Labels encode milestones: `M1-schema`, `M2-generator`, `M3-ui`, `M4-rollout`, `migration`.
- Each issue body lists: goal, scope, PRD references, acceptance criteria,
  `Depends on: #N`, and the files it owns (to minimize collisions when issues are
  implemented in parallel by sub-agents).
- Issues labeled `migration` are intentionally **not** implementable in the GitHub
  phase (they need Bitbucket/Jenkins/k3s access) — do not start them until the
  migration.
