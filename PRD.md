# PRD: Internal Web App Catalog ("App Catalog")

**Status:** Draft v1.0
**Owner / Maintainer:** [Your Name] — also the named maintainer of the generator itself
**Date:** 2026-07-19

---

## 1. Background & Problem

Our company runs a growing number of internal web applications (currently ~15–40 expected over the next 2 years), some of them vibe-coded. They live in Bitbucket repositories (one repo = one app) and are deployed to an internal Rancher-managed k3s cluster via existing Jenkins CI/CD pipelines.

There is no central overview. New team members cannot discover what apps exist, what state they are in, or who owns them.

**Primary goal: onboarding & discoverability.** A newcomer should be able to open one page and understand what apps exist, whether they are running, who maintains them, and where to find code and docs.

## 2. Solution Overview

A **statically generated catalog page**, rebuilt on a schedule by Jenkins. No new long-running backend service.

Three components:

1. **Metadata convention** — every app repo contains a `catalog-info.yaml` at repo root (Backstage-compatible schema).
2. **Generator** — a single script (suggested: Python, ~300 lines) run by a scheduled Jenkins job. It aggregates Bitbucket metadata + Rancher/k8s runtime state and renders one self-contained static HTML page.
3. **Hosting** — the HTML page is served by a minimal nginx pod on the existing k3s cluster.

### Architecture

```
Bitbucket API ──┐
                ├──► Generator (Jenkins scheduled job, every 30 min)
Rancher/k8s API ┘          │
                           ▼
                 catalog.html (static, self-contained)
                           │
                           ▼
                 nginx pod on k3s ──► users' browsers
```

## 3. Goals

- G1: One searchable page listing all internal web apps.
- G2: Show declared metadata (owner, description, lifecycle) AND observed runtime state (health, deployed version, last deploy date) side by side.
- G3: Surface data-quality gaps: apps deployed but not cataloged, and cataloged but not deployed.
- G4: Near-zero operational maintenance: no database, no backend, no auth service of its own.
- G5: Drive metadata adoption via visibility first (warnings), hard pipeline gate later.

## 4. Non-Goals

- NG1: Not a deployment tool, service mesh UI, or replacement for Rancher's own dashboards.
- NG2: No write operations anywhere — the catalog is strictly read-only.
- NG3: No real-time (< 1 min) health data. Refresh interval of 15–60 min is acceptable ("near-live").
- NG4: Not Backstage. But the metadata schema must stay Backstage-compatible to keep a migration path open.
- NG5: No per-app documentation hosting. The catalog links out to each repo's README.

## 5. Users

- **New employees / newcomers** (primary): browse and search apps, find owners, find code and running instances.
- **Developers**: check whether their `catalog-info.yaml` is valid; see their app listed correctly.
- **Team leads / the catalog owner**: monitor coverage and gaps (uncataloged deployments, orphaned entries).

## 6. Functional Requirements

### 6.1 Metadata file: `catalog-info.yaml`

Location: repo root of every app repository. Schema: Backstage `Component` kind (subset). One repo = one app = one manifest (multi-component manifests are out of scope for v1).

```yaml
apiVersion: backstage.io/v1alpha1
kind: Component
metadata:
  name: time-tracker            # unique, kebab-case, required
  description: Internal time tracking for project billing   # required
  annotations:
    ourcompany.io/k8s-deployment: time-tracker    # required: k8s Deployment name
    ourcompany.io/k8s-namespace: tools            # required: k8s namespace
  links:                        # optional
    - url: https://time.internal.example.com
      title: App URL
    - url: https://confluence.example.com/x/abc
      title: Runbook
  tags: [react, internal-tool]  # optional
spec:
  type: website                 # required: website | service | tool
  lifecycle: production         # required: experimental | production | deprecated
  owner: jane.doe               # required: Bitbucket username of main maintainer
```

**Explicit deployment mapping (decided):** the k8s Deployment name and namespace are declared in annotations. The generator MUST NOT guess by name matching.

**Validation rules** (used by both the generator and the pipeline check):
- Required fields present: `metadata.name`, `metadata.description`, `spec.type`, `spec.lifecycle`, `spec.owner`, both `ourcompany.io/k8s-*` annotations (exception: `lifecycle: experimental` apps MAY omit the k8s annotations if not yet deployed).
- `metadata.name` unique across all repos; kebab-case; max 50 chars.
- `spec.lifecycle` ∈ {experimental, production, deprecated}.
- YAML must parse; unknown extra fields are allowed (forward compatibility).

### 6.2 Generator

Runs as a scheduled Jenkins job **every 30 minutes**. Stateless: every run rebuilds from scratch.

**Inputs / data collection:**
1. **Bitbucket** (REST API, read-only token):
   - List all repositories in the designated Bitbucket project(s) (project keys configurable).
   - For each repo, fetch `catalog-info.yaml` from the default branch. Missing file → repo is recorded as "uncataloged".
   - Also fetch: repo URL, last commit date on default branch, whether a `README.md` exists (for the docs link).
2. **Rancher/k8s** (read-only ServiceAccount token, cluster-wide list/get on Deployments and Pods):
   - For every cataloged app: look up the declared Deployment in the declared namespace. Collect: replica readiness (ready/desired), image tag(s), last rollout timestamp (`status.conditions` / ReplicaSet creation), namespace.
   - Additionally: list ALL Deployments in configurable namespaces and cross-check against the catalog to find **deployed-but-uncataloged** workloads. Configurable ignore-list for infra workloads (nginx-ingress, cert-manager, the catalog's own nginx, etc.).

**Derived status per app:**

| Status | Condition |
|---|---|
| 🟢 Healthy | Deployment found, readyReplicas == desiredReplicas ≥ 1 |
| 🟡 Degraded | Deployment found, 0 < ready < desired, or recent restarts |
| 🔴 Down | Deployment found, readyReplicas == 0 |
| ⚪ Not deployed | Cataloged but declared Deployment not found (OK for `experimental`) |
| ❓ Unknown | k8s API unreachable or lookup error (show error, don't hide) |

**Error handling:**
- One repo's broken YAML must never break the whole run: render the app with a visible "invalid metadata" badge + parse error summary.
- If the Rancher API is fully unreachable, still render the page from Bitbucket data, with a banner "runtime data unavailable since <timestamp>".
- Generator exits non-zero only on total failure (cannot reach Bitbucket at all); Jenkins job failure should notify the catalog owner (email/Slack — reuse existing notification setup).

**Output:** a single self-contained `catalog.html` (inline CSS/JS, no external CDN dependencies — must work on a restricted internal network). Optionally also `catalog.json` with the raw aggregated data for future tooling.

### 6.3 Catalog page (UI)

Single static HTML page. All interactivity client-side (vanilla JS or a small inlined library).

- **Header:** title, total app count, coverage stat ("34 of 38 repos cataloged"), timestamp of last generation.
- **Search & filter:** instant text search across name/description/tags/owner; filter chips for lifecycle (experimental/production/deprecated) and status (healthy/degraded/down/not deployed).
- **App cards or table rows**, each showing:
  - Name + description
  - Status badge (from table above) + "ready x/y replicas"
  - Lifecycle badge
  - Owner (link to Bitbucket profile or `mailto:` if resolvable)
  - Deployed version (image tag) + last deploy date + last commit date
  - Links: App URL, Bitbucket repo, README/docs, any extra links from the manifest
- **Gaps section** (visible by default — visibility drives adoption):
  - "Deployed but not cataloged": list of unmatched Deployments (namespace/name).
  - "Cataloged but not deployed": non-experimental apps whose Deployment is missing.
  - "Invalid metadata": repos with parse/validation errors and the error message.
- Sort options: name (default), status, last deploy date, lifecycle.
- Must be usable on a laptop screen; mobile is nice-to-have.
- No authentication in the page itself; access control is network-level (internal cluster/ingress only).

### 6.4 Pipeline validation (shared Jenkins library step)

A reusable step `validateCatalogInfo()` added to app pipelines:

- **Phase 1 (now — WARN):** missing or invalid `catalog-info.yaml` prints a prominent warning in the build log and marks the build UNSTABLE (not failed). Warning links to the schema docs and an example file.
- **Phase 2 (later — GATE):** flip a single config flag to make the step fail the build. Trigger criterion: catalog coverage ≥ 80% (readable from `catalog.json`). The flip is a manual decision by the catalog owner.
- The validation logic must be the same code/rules as the generator uses (share a schema definition or the validation script itself) to avoid drift.

### 6.5 Hosting

- Minimal nginx (or equivalent) Deployment on the k3s cluster serving the generated files.
- The Jenkins generator job publishes the output. Two acceptable mechanisms (implementer's choice, prefer whatever fits existing patterns):
  - a) `kubectl cp` / ConfigMap update + reload, or
  - b) push to a tiny writable volume / object store nginx serves from.
- Exposed via existing internal ingress, e.g. `https://apps.internal.example.com`. Internal network only.
- The catalog itself gets its own repo with its own `catalog-info.yaml` (owner: [Your Name], lifecycle: production, type: tool). Eat your own dog food.

## 7. Security & Access

- **Bitbucket token:** read-only, scoped to the relevant project(s). Stored in Jenkins credentials store.
- **k8s access:** dedicated ServiceAccount with a ClusterRole granting only `get`/`list` on `deployments`, `replicasets`, `pods` (and `namespaces`). No secrets access, no write verbs. Token stored in Jenkins credentials store. (Read-only cluster-wide access approved.)
- The generated page must never include secrets, env vars, or full pod specs — only the fields listed in 6.2/6.3.
- Page served on internal network only; no public exposure.

## 8. Configuration (externalized, not hardcoded)

- Bitbucket base URL + project key(s)
- k8s API endpoint / kubeconfig context
- Namespaces to scan for uncataloged deployments + ignore-list (regex or explicit names)
- Refresh schedule (default: `H/30 * * * *`)
- Output location / publish target
- Warn-vs-gate flag for the pipeline step

## 9. Acceptance Criteria

1. Given 3 test repos (valid manifest, invalid YAML, no manifest) and 1 deployment without any repo, the generated page shows: 1 healthy cataloged app, 1 "invalid metadata" entry with the error, 1 uncataloged repo counted in coverage, and 1 "deployed but not cataloged" gap entry.
2. Killing the app's pods flips its badge to 🔴 within one refresh cycle (≤ 30 min).
3. With the Rancher API blocked, the page still renders all declared metadata plus the "runtime data unavailable" banner.
4. Search for a tag, an owner name, and a partial app name each filters correctly with no page reload.
5. The page loads with zero external network requests (fully self-contained).
6. `validateCatalogInfo()` marks a build UNSTABLE for a repo missing the manifest (phase 1) and FAILED after the gate flag is flipped (phase 2).
7. The generator run completes in < 2 minutes for 40 repos.

## 10. Milestones

1. **M1 — Schema & validation:** finalize `catalog-info.yaml` schema, validation script, example file, one-page schema doc for developers.
2. **M2 — Generator MVP:** Bitbucket + k8s aggregation, JSON output, plain HTML table. Deployed and running on schedule.
3. **M3 — UI polish:** search, filters, badges, gaps section.
4. **M4 — Rollout:** announce convention, add warn-mode pipeline step, drive coverage.
5. **M5 — Gate:** flip pipeline step to hard gate at ≥ 80% coverage.

## 11. Risks & Mitigations

- **Metadata rot** (owners leave, lifecycle stale): the gaps section + pipeline gate mitigate; additionally recommend a quarterly review by the catalog owner (process, not code).
- **Half-empty catalog hurts onboarding trust:** don't announce broadly until coverage > ~70%; the coverage stat in the header keeps this honest.
- **Generator becomes an unowned app:** explicitly owned by [Your Name]; it is small, stateless, and rebuildable — worst case, the page goes stale but nothing breaks.
- **Rancher API quirks (auth, pagination, RBAC):** validated early in M2 with a spike against the real cluster.

## 12. Out of Scope / Future Ideas (do not build in v1)

- Multiple components per repo / monorepo support
- Historical uptime or deploy-frequency charts
- Scorecards (security review passed, SSO enabled, backup status)
- API-driven integrations (Slack bot: "who owns app X?")
- Migration to Backstage (the compatible schema keeps this open)
