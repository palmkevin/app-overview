# App Catalog

A statically generated overview page for our internal web applications: what exists,
whether it is running, who owns it, and where to find code and docs. One page,
no backend, no database.

> **Status: under construction.** This repo is being built issue by issue — see the
> [issue tracker](https://github.com/palmkevin/app-overview/issues) for the plan.
> It currently lives on GitHub and will migrate to the company Bitbucket/Jenkins/k3s
> infrastructure later.

## How it works

1. Every app repo declares metadata in a `catalog-info.yaml` at its root
   (Backstage-compatible schema — see `docs/schema.md` once available).
2. A Python generator aggregates that metadata with observed Kubernetes runtime
   state (health, deployed version, last deploy) and renders one self-contained
   `catalog.html`, including a "gaps" section (deployed-but-uncataloged,
   cataloged-but-not-deployed, invalid metadata).
3. The page is served statically — nginx on k3s in production; **GitHub Pages
   during the current phase**: <https://palmkevin.github.io/app-overview/>
   (testrun-mode demo data, not real apps).

### Operating modes

- **`testrun`** — data sources are faked from deterministic fixtures in `testdata/`;
  no network access. Used for the hosted demo, CI, and tests.
- **`live`** — real Bitbucket + Kubernetes APIs. Usable only after migration to the
  company infrastructure.

## Documentation

- [PRD.md](PRD.md) — full product requirements
- [docs/decisions.md](docs/decisions.md) — binding implementation decisions and the
  testrun-mode specification
- `docs/schema.md` — `catalog-info.yaml` reference for app developers (issue #3)

## Development

```bash
pip install -e .[dev]
pytest
ruff check .
python -m catalog_generator --mode testrun --output out/
```

Requires Python 3.14. See [CLAUDE.md](CLAUDE.md) for the working conventions and
hard rules that apply to all changes in this repo.

## Maintainer

Kevin Palm (catalog owner)
