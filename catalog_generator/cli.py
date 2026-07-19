"""Command-line interface for the catalog generator.

Stub (issue #1): argument parsing only. The actual generation pipeline
(config -> sources -> aggregate -> render) is wired up in later issues.
"""

import argparse


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
        help="Path to the generator configuration file.",
    )
    parser.add_argument(
        "--mode",
        choices=["testrun", "live"],
        default="testrun",
        help=(
            "Data source mode: 'testrun' uses deterministic fixtures from testdata/ "
            "(no network); 'live' uses the real Bitbucket/Kubernetes APIs "
            "(unusable until migration). Default: testrun."
        ),
    )
    parser.add_argument(
        "--output",
        metavar="DIR",
        default="out/",
        help="Output directory for the generated page. Default: out/.",
    )
    parser.add_argument(
        "--scenario",
        metavar="NAME",
        default=None,
        help="Named testrun scenario (e.g. 'runtime-unavailable'). Testrun mode only.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns the process exit code."""
    parser = build_parser()
    parser.parse_args(argv)
    # Stub: the generation pipeline lands in later issues (M2-generator).
    parser.exit(
        status=2,
        message="catalog_generator: generation not implemented yet (project scaffolding only)\n",
    )
    return 2  # unreachable; parser.exit() raises SystemExit
