# `catalog-info.yaml` — schema reference

Every internal app repository declares itself to the App Catalog with a single
`catalog-info.yaml` file at its repository root. The catalog generator reads these
manifests, combines them with observed Kubernetes runtime state, and renders the
company-wide catalog page — so a missing or invalid manifest means your app shows up
as a gap (or not at all). The schema is a Backstage-compatible Component subset;
unknown extra fields are allowed, so you can add more later without breaking anything.

## Fields

| Field | Required | Allowed values / format | Example |
|---|---|---|---|
| `metadata` | yes | mapping | — |
| `metadata.name` | yes | kebab-case (lowercase letters/digits separated by single hyphens, no leading/trailing hyphen), max 50 chars, **unique across all repos** | `time-tracker` |
| `metadata.description` | yes | non-empty string | `Internal time tracking for project billing` |
| `metadata.annotations.<prefix>/k8s-deployment` | yes¹ | non-empty string — the exact Kubernetes **Deployment name** | `time-tracker` |
| `metadata.annotations.<prefix>/k8s-namespace` | yes¹ | non-empty string — the Kubernetes **namespace** of that Deployment | `tools` |
| `spec` | yes | mapping | — |
| `spec.type` | yes | one of `website`, `service`, `tool` | `website` |
| `spec.lifecycle` | yes | one of `experimental`, `production`, `deprecated` | `production` |
| `spec.owner` | yes | non-empty string — Bitbucket username of the main maintainer | `jane.doe` |
| `apiVersion`, `kind` | no (not validated) | keep `backstage.io/v1alpha1` / `Component` for Backstage compatibility | — |
| `metadata.links` | no (not validated) | list of `{url, title}` — shown on the catalog page | see example |
| `metadata.tags` | no (not validated) | list of strings | `[react, internal-tool]` |

¹ **Experimental exception:** apps with `lifecycle: experimental` may omit both k8s
annotations (an experimental app doesn't have to be deployed). For every other
lifecycle value they are required — deployment matching happens **only** via these
annotations, never by guessing from the app name.

**Annotation prefix:** `ourcompany.io` is the default, but the prefix is configurable
per catalog installation. Use whatever prefix your installation is configured with;
the field suffixes (`/k8s-deployment`, `/k8s-namespace`) never change.

The file itself must be valid YAML and a mapping at the top level (an empty file or a
bare list is rejected).

## Full example

Copy-paste and adapt ([examples/catalog-info.yaml](../examples/catalog-info.yaml)):

```yaml
# Reference catalog-info.yaml (PRD §6.1) — place this file at the root of
# every app repository. Backstage-compatible Component schema (subset);
# unknown extra fields are allowed for forward compatibility.
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

## Common mistakes

- **Bad kebab-case name** — `TimeTracker`, `time_tracker`, `time--tracker`, or
  `-time-tracker` are all rejected. Use lowercase letters/digits with single hyphens:
  `time-tracker`.
- **Name too long** — `metadata.name` is capped at 50 characters.
- **Duplicate name** — `metadata.name` must be unique across *all* repos; picking a
  name that another app already uses flags both manifests.
- **Missing k8s annotations** — a non-experimental app without
  `<prefix>/k8s-deployment` and `<prefix>/k8s-namespace` fails validation, and the
  catalog cannot match it to its running Deployment.
- **Wrong lifecycle value** — `prod`, `live`, or `beta` are not accepted; only
  `experimental`, `production`, `deprecated` (same idea for `spec.type`:
  only `website`, `service`, `tool`).

Validation is implemented in
[`catalog_generator/validation.py`](../catalog_generator/validation.py) — the single
source of truth; this page documents exactly those rules.
