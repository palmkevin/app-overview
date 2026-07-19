"""Tests for configuration loading (issue #4).

No network, no clock reads — pure file/YAML handling.
"""

from pathlib import Path

import pytest

from catalog_generator.config import (
    DEFAULT_ANNOTATION_PREFIX,
    DEFAULT_BITBUCKET_TOKEN_ENV,
    DEFAULT_K8S_TOKEN_ENV,
    Config,
    ConfigError,
    load_config,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CONFIG = REPO_ROOT / "config" / "config.example.yaml"


def write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


# --- defaults -----------------------------------------------------------------


def test_defaults_without_config_file() -> None:
    config = load_config(None)
    assert config.mode == "testrun"
    assert config.annotation_prefix == DEFAULT_ANNOTATION_PREFIX
    assert config.testdata_dir == "testdata"
    assert config.output_dir == "out"
    assert config.ignore_deployments == ()
    assert config.bitbucket.token_env == DEFAULT_BITBUCKET_TOKEN_ENV
    assert config.kubernetes.token_env == DEFAULT_K8S_TOKEN_ENV


def test_empty_config_file_gives_defaults(tmp_path: Path) -> None:
    path = write_config(tmp_path, "")
    config = load_config(path)
    assert config == load_config(None)


def test_testrun_mode_needs_no_bitbucket_or_k8s_keys(tmp_path: Path) -> None:
    path = write_config(tmp_path, "mode: testrun\n")
    config = load_config(path)
    assert config.bitbucket.base_url is None
    assert config.kubernetes.api_endpoint is None


# --- example file --------------------------------------------------------------


def test_loading_the_example_config_succeeds() -> None:
    config = load_config(EXAMPLE_CONFIG)
    assert config.mode == "testrun"
    assert config.bitbucket.base_url == "https://bitbucket.example.com"
    assert config.bitbucket.project_keys == ("APPS",)
    assert config.kubernetes.api_endpoint == "https://k8s.example.com:6443"
    assert config.kubernetes.ca_path == "/etc/catalog/k8s-ca.crt"
    assert config.kubernetes.namespaces == ("tools", "apps")
    assert "app-catalog-nginx" in config.ignore_deployments


def test_example_config_contains_no_real_hostnames_or_secrets() -> None:
    text = EXAMPLE_CONFIG.read_text(encoding="utf-8")
    assert "example.com" in text
    # Only env var NAMES, never values.
    assert "token_env" in text


# --- annotation prefix ----------------------------------------------------------


def test_non_default_annotation_prefix_is_honored(tmp_path: Path) -> None:
    path = write_config(tmp_path, "annotation_prefix: acme-corp.dev\n")
    config = load_config(path)
    assert config.annotation_prefix == "acme-corp.dev"
    assert config.annotation_key("k8s-deployment") == "acme-corp.dev/k8s-deployment"
    assert config.annotation_key("k8s-namespace") == "acme-corp.dev/k8s-namespace"


@pytest.mark.parametrize("bad", ["''", "trailing.slash/"])
def test_invalid_annotation_prefix_rejected(tmp_path: Path, bad: str) -> None:
    path = write_config(tmp_path, f"annotation_prefix: {bad}\n")
    with pytest.raises(ConfigError, match="annotation_prefix"):
        load_config(path)


# --- mode validation -------------------------------------------------------------


def test_invalid_mode_rejected(tmp_path: Path) -> None:
    path = write_config(tmp_path, "mode: production\n")
    with pytest.raises(ConfigError, match="mode"):
        load_config(path)


def test_live_mode_missing_keys_are_all_listed(tmp_path: Path) -> None:
    path = write_config(tmp_path, "mode: live\n")
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    message = str(excinfo.value)
    assert "live" in message
    for key in (
        "bitbucket.base_url",
        "bitbucket.project_keys",
        "kubernetes.api_endpoint",
        "kubernetes.namespaces",
    ):
        assert key in message


def test_live_mode_partial_config_lists_only_missing(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        "mode: live\n"
        "bitbucket:\n"
        "  base_url: https://bitbucket.example.com\n"
        "  project_keys: [APPS]\n",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    message = str(excinfo.value)
    assert "bitbucket.base_url" not in message
    assert "kubernetes.api_endpoint" in message
    assert "kubernetes.namespaces" in message


def test_live_mode_with_all_required_keys_loads(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        "mode: live\n"
        "bitbucket:\n"
        "  base_url: https://bitbucket.example.com\n"
        "  project_keys: [APPS]\n"
        "kubernetes:\n"
        "  api_endpoint: https://k8s.example.com:6443\n"
        "  namespaces: [tools]\n",
    )
    config = load_config(path)
    assert config.mode == "live"
    assert config.kubernetes.namespaces == ("tools",)


# --- secrets ----------------------------------------------------------------------


def test_tokens_come_from_environment_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_config(
        tmp_path,
        "bitbucket:\n"
        "  token_env: MY_CUSTOM_BB_TOKEN\n"
        "kubernetes:\n"
        "  token_env: MY_CUSTOM_K8S_TOKEN\n",
    )
    config = load_config(path)
    monkeypatch.setenv("MY_CUSTOM_BB_TOKEN", "bb-token-value")
    monkeypatch.setenv("MY_CUSTOM_K8S_TOKEN", "k8s-token-value")
    assert config.bitbucket_token() == "bb-token-value"
    assert config.k8s_token() == "k8s-token-value"


def test_missing_env_vars_yield_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DEFAULT_BITBUCKET_TOKEN_ENV, raising=False)
    monkeypatch.delenv(DEFAULT_K8S_TOKEN_ENV, raising=False)
    config = load_config(None)
    assert config.bitbucket_token() is None
    assert config.k8s_token() is None


def test_inline_secret_in_config_file_is_rejected(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        "bitbucket:\n  token: super-secret-value\n",
    )
    with pytest.raises(ConfigError, match="environment variables"):
        load_config(path)


# --- malformed input ----------------------------------------------------------------


def test_missing_file_readable_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read config file"):
        load_config(tmp_path / "does-not-exist.yaml")


def test_invalid_yaml_readable_error(tmp_path: Path) -> None:
    path = write_config(tmp_path, "mode: [unclosed\n")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(path)


def test_non_mapping_yaml_rejected(tmp_path: Path) -> None:
    path = write_config(tmp_path, "- just\n- a\n- list\n")
    with pytest.raises(ConfigError, match="mapping"):
        load_config(path)


def test_unknown_top_level_key_rejected(tmp_path: Path) -> None:
    path = write_config(tmp_path, "annotation_prefx: typo.example\n")
    with pytest.raises(ConfigError, match="annotation_prefx"):
        load_config(path)


def test_wrong_type_readable_error(tmp_path: Path) -> None:
    path = write_config(tmp_path, "bitbucket:\n  project_keys: APPS\n")
    with pytest.raises(ConfigError, match="project_keys"):
        load_config(path)


def test_config_is_immutable() -> None:
    config = load_config(None)
    with pytest.raises(AttributeError):
        config.mode = "live"  # type: ignore[misc]
    assert isinstance(config, Config)
