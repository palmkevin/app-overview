"""Command-line interface: config -> sources -> aggregate -> render -> files.

Pipeline wiring (issue #9). Exit-code contract (PRD §6.2):

* Non-zero ONLY on total repo-source failure — Bitbucket entirely
  unreachable, unreadable testrun fixtures, or broken configuration.
* A fully unreachable *runtime* API is handled inside aggregation: the page
  still renders (with the "runtime data unavailable" banner) and the exit
  code is 0.

This module contains the single permitted clock read of the whole
generator: ``generated_at`` defaults to ``datetime.now(UTC)`` here and is
passed down as a parameter everywhere else (injectable for tests).
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from catalog_generator.aggregate import aggregate, to_json
from catalog_generator.config import Config, ConfigError, load_config
from catalog_generator.render import render_catalog
from catalog_generator.sources.base import RepoSource, RuntimeSource
from catalog_generator.sources.bitbucket import (
    BitbucketRepoSource,
    RepoSourceUnavailableError,
)
from catalog_generator.sources.fake import (
    DEFAULT_SCENARIO,
    FakeRepoSource,
    FakeRuntimeSource,
    FixtureError,
)
from catalog_generator.sources.kubernetes import KubernetesRuntimeSource

#: Config file picked up automatically when ``--config`` is not given.
DEFAULT_CONFIG_PATH = Path("config") / "config.yaml"

HTML_NAME = "catalog.html"
JSON_NAME = "catalog.json"


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for ``python -m catalog_generator``."""
    parser = argparse.ArgumentParser(
        prog="catalog_generator",
        description=(
            "Generate a self-contained catalog.html from app metadata "
            "(catalog-info.yaml) and Kubernetes runtime state."
        ),
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=None,
        help=(
            "Path to the generator configuration file. Default: "
            f"{DEFAULT_CONFIG_PATH} if it exists, else built-in defaults."
        ),
    )
    parser.add_argument(
        "--mode",
        choices=["testrun", "live"],
        default=None,
        help=(
            "Data source mode, overriding the config file: 'testrun' uses "
            "deterministic fixtures from testdata/ (no network); 'live' uses the "
            "real Bitbucket/Kubernetes APIs (unusable until migration)."
        ),
    )
    parser.add_argument(
        "--output",
        metavar="DIR",
        default=None,
        help="Output directory for catalog.html and catalog.json (created if missing). "
        "Overrides the config's output_dir (default: out).",
    )
    parser.add_argument(
        "--scenario",
        metavar="NAME",
        default=None,
        help=(
            "Named testrun fixture scenario, e.g. 'default' or 'runtime-unavailable'. "
            "Testrun mode only."
        ),
    )
    return parser


def main(argv: list[str] | None = None, *, generated_at: datetime | None = None) -> int:
    """CLI entry point. Returns the process exit code.

    ``generated_at`` is injectable for deterministic tests; when ``None``
    the current UTC time is used (the only clock read in the generator).
    """
    args = build_parser().parse_args(argv)
    start = time.monotonic()

    try:
        config = _resolve_config(args)
        if args.scenario is not None and config.mode != "testrun":
            raise ConfigError("--scenario is only valid in testrun mode")
        repo_source, runtime_source = _build_sources(config, args.scenario)
    except (ConfigError, FixtureError, ValueError) as exc:
        return _fail(f"configuration error: {exc}")

    if generated_at is None:
        generated_at = datetime.now(UTC)

    try:
        catalog = aggregate(repo_source, runtime_source, config, generated_at)
    except RepoSourceUnavailableError as exc:
        return _fail(f"repo source unavailable: {exc}")
    except FixtureError as exc:
        return _fail(f"unreadable testrun fixtures: {exc}")

    output_dir = Path(args.output) if args.output else Path(config.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / HTML_NAME).write_text(render_catalog(catalog), encoding="utf-8")
    (output_dir / JSON_NAME).write_text(to_json(catalog) + "\n", encoding="utf-8")

    duration = time.monotonic() - start
    gaps = catalog.gaps
    print(
        f"catalog_generator: {len(catalog.entries)} apps, "
        f"{catalog.coverage.cataloged}/{catalog.coverage.total} repos cataloged "
        f"({catalog.coverage.percent:.0f}%), gaps: "
        f"{len(gaps.deployed_uncataloged)} deployed-uncataloged, "
        f"{len(gaps.cataloged_not_deployed)} cataloged-not-deployed, "
        f"{len(gaps.invalid_metadata)} invalid-metadata, "
        f"runtime {'available' if catalog.runtime_available else 'UNAVAILABLE'}, "
        f"{duration:.2f}s -> {output_dir / HTML_NAME}",
        file=sys.stderr,
    )
    return 0


def _resolve_config(args: argparse.Namespace) -> Config:
    """Load the config (explicit path, default path, or built-in defaults)."""
    if args.config is not None:
        config = load_config(args.config)
    elif DEFAULT_CONFIG_PATH.is_file():
        config = load_config(DEFAULT_CONFIG_PATH)
    else:
        config = load_config(None)
    if args.mode is not None and args.mode != config.mode:
        # CLI override; live-mode completeness is enforced by the real
        # adapters' constructors (they raise on missing settings).
        config = replace(config, mode=args.mode)
    return config


def _build_sources(config: Config, scenario: str | None) -> tuple[RepoSource, RuntimeSource]:
    """Select the source adapters for the resolved mode."""
    if config.mode == "testrun":
        return (
            FakeRepoSource(config.testdata_dir),
            FakeRuntimeSource(config.testdata_dir, scenario or DEFAULT_SCENARIO),
        )
    return (
        BitbucketRepoSource(config),
        KubernetesRuntimeSource(config.kubernetes),
    )


def _fail(message: str) -> int:
    print(f"catalog_generator: error: {message}", file=sys.stderr)
    return 1
