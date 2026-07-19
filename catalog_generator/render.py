"""Rendering: produce the self-contained catalog.html via Jinja2.

MVP (issue #9): a plain table page. Hard constraints (PRD acceptance
criterion 5, CLAUDE.md):

* Fully self-contained output — inline CSS only, zero external requests
  (no CDN, no webfonts, no external images; emoji serve as status icons).
* Autoescaping is ON: manifest/fixture data is untrusted and must be
  HTML-escaped by the template engine.
* No clock reads and no validation logic here — everything displayed comes
  from the already-aggregated :class:`~catalog_generator.model.CatalogData`.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from catalog_generator.model import CatalogData, Status

#: The templates/ directory at the repo root, resolved relative to this
#: package so rendering works regardless of the current working directory.
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

TEMPLATE_NAME = "catalog.html.j2"

#: Status icons (PRD §6.2 status table). Emoji, not images — the page must
#: not load any external resource.
STATUS_EMOJI = {
    Status.HEALTHY: "\U0001f7e2",  # green circle
    Status.DEGRADED: "\U0001f7e1",  # yellow circle
    Status.DOWN: "\U0001f534",  # red circle
    Status.NOT_DEPLOYED: "⚪",  # white circle
    Status.UNKNOWN: "❓",  # question mark
}


def _datefmt(value: datetime | None) -> str:
    """Human-readable timestamp for the page; em dash when absent."""
    if value is None:
        return "—"
    text = value.strftime("%Y-%m-%d %H:%M")
    tz = value.strftime("%Z")
    return f"{text} {tz}" if tz else text


def _environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=True,  # manifest data is untrusted — always escape
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["datefmt"] = _datefmt
    return env


def render_catalog(catalog: CatalogData) -> str:
    """Render :class:`CatalogData` to the self-contained catalog.html string."""
    template = _environment().get_template(TEMPLATE_NAME)
    return template.render(catalog=catalog, status_emoji=STATUS_EMOJI)
