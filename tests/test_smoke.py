"""Smoke tests: the package and all its modules import cleanly."""

import importlib

import pytest

MODULES = [
    "catalog_generator",
    "catalog_generator.aggregate",
    "catalog_generator.cli",
    "catalog_generator.config",
    "catalog_generator.model",
    "catalog_generator.render",
    "catalog_generator.validation",
    "catalog_generator.sources",
    "catalog_generator.sources.base",
]


@pytest.mark.parametrize("module", MODULES)
def test_module_imports(module: str) -> None:
    assert importlib.import_module(module) is not None


def test_version() -> None:
    import catalog_generator

    assert isinstance(catalog_generator.__version__, str)


def test_cli_help_exits_zero() -> None:
    from catalog_generator.cli import main

    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])
    assert excinfo.value.code == 0
