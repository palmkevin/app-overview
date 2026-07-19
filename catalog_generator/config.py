"""Generator configuration: loading, defaults, and mode-dependent checks.

Configuration is externalized (PRD §8). Secrets are NEVER read from the
config file — the file only names the *environment variables* that hold
the tokens; the values come from the process environment at runtime.

The annotation prefix (``annotation_prefix``) is configurable; the value
``ourcompany.io`` below is only the *default* and must never be assumed
anywhere else in the codebase.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

#: Default annotation prefix — a placeholder default, overridable via the
#: ``annotation_prefix`` config key. The single place this string exists.
DEFAULT_ANNOTATION_PREFIX = "ourcompany.io"

#: Default names of the environment variables holding the secrets.
DEFAULT_BITBUCKET_TOKEN_ENV = "CATALOG_BITBUCKET_TOKEN"
DEFAULT_K8S_TOKEN_ENV = "CATALOG_K8S_TOKEN"

MODES = ("testrun", "live")

#: Config keys that would smell like an inline secret. Rejected outright so
#: nobody ever puts a token value in a config file.
_SECRET_LIKE_KEYS = ("token", "password", "secret", "api_key", "apikey")


class ConfigError(Exception):
    """Raised for unreadable, malformed, or invalid configuration."""


@dataclass(frozen=True)
class BitbucketConfig:
    """Bitbucket repo-source settings (used in ``live`` mode)."""

    base_url: str | None = None
    project_keys: tuple[str, ...] = ()
    #: Name of the env var holding the read-only Bitbucket token.
    token_env: str = DEFAULT_BITBUCKET_TOKEN_ENV


@dataclass(frozen=True)
class KubernetesConfig:
    """Kubernetes runtime-source settings (used in ``live`` mode)."""

    api_endpoint: str | None = None
    ca_path: str | None = None
    #: Namespaces scanned for deployments (incl. uncataloged ones).
    namespaces: tuple[str, ...] = ()
    #: Name of the env var holding the read-only ServiceAccount token.
    token_env: str = DEFAULT_K8S_TOKEN_ENV


@dataclass(frozen=True)
class Config:
    """Fully resolved generator configuration."""

    mode: str = "testrun"
    annotation_prefix: str = DEFAULT_ANNOTATION_PREFIX
    #: Directory with the deterministic fixtures (testrun mode).
    testdata_dir: str = "testdata"
    #: Output directory for catalog.html / catalog.json.
    output_dir: str = "out"
    #: Deployments to exclude from the uncataloged-gap check. Each item is
    #: a regular expression matched against the Deployment name (an explicit
    #: name is simply a regex without metacharacters).
    ignore_deployments: tuple[str, ...] = ()
    bitbucket: BitbucketConfig = field(default_factory=BitbucketConfig)
    kubernetes: KubernetesConfig = field(default_factory=KubernetesConfig)

    def annotation_key(self, suffix: str) -> str:
        """Build a fully-qualified annotation key, e.g. ``<prefix>/k8s-namespace``."""
        return f"{self.annotation_prefix}/{suffix}"

    def bitbucket_token(self) -> str | None:
        """Read the Bitbucket token from the configured env var (never from config)."""
        return os.environ.get(self.bitbucket.token_env)

    def k8s_token(self) -> str | None:
        """Read the k8s token from the configured env var (never from config)."""
        return os.environ.get(self.kubernetes.token_env)


def load_config(path: str | Path | None = None) -> Config:
    """Load and validate the YAML config file.

    ``path=None`` returns the pure defaults (sufficient for testrun mode).
    Raises :class:`ConfigError` with a readable message on any problem;
    live mode reports *all* missing required keys in one error.
    """
    if path is None:
        data: dict = {}
    else:
        path = Path(path)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"cannot read config file {path}: {exc}") from exc
        try:
            loaded = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ConfigError(f"config file {path} is not valid YAML: {exc}") from exc
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, dict):
            raise ConfigError(
                f"config file {path} must contain a YAML mapping, got {type(loaded).__name__}"
            )
        data = loaded

    _reject_secret_keys(data, where="top level")
    _reject_unknown_keys(
        data,
        allowed=(
            "mode",
            "annotation_prefix",
            "testdata_dir",
            "output_dir",
            "ignore_deployments",
            "bitbucket",
            "kubernetes",
        ),
        where="top level",
    )

    mode = _get_str(data, "mode", default="testrun")
    if mode not in MODES:
        raise ConfigError(f"mode must be one of {list(MODES)}, got {mode!r}")

    annotation_prefix = _get_str(data, "annotation_prefix", default=DEFAULT_ANNOTATION_PREFIX)
    if not annotation_prefix or annotation_prefix.endswith("/"):
        raise ConfigError(
            f"annotation_prefix must be a non-empty string without a trailing '/', "
            f"got {annotation_prefix!r}"
        )

    config = Config(
        mode=mode,
        annotation_prefix=annotation_prefix,
        testdata_dir=_get_str(data, "testdata_dir", default="testdata"),
        output_dir=_get_str(data, "output_dir", default="out"),
        ignore_deployments=_get_str_tuple(data, "ignore_deployments"),
        bitbucket=_parse_bitbucket(data.get("bitbucket")),
        kubernetes=_parse_kubernetes(data.get("kubernetes")),
    )

    if config.mode == "live":
        _require_live_keys(config)
    return config


def _parse_bitbucket(raw: object) -> BitbucketConfig:
    data = _as_section(raw, "bitbucket")
    _reject_secret_keys(data, where="bitbucket")
    _reject_unknown_keys(
        data, allowed=("base_url", "project_keys", "token_env"), where="bitbucket"
    )
    return BitbucketConfig(
        base_url=_get_str_or_none(data, "base_url", where="bitbucket"),
        project_keys=_get_str_tuple(data, "project_keys", where="bitbucket"),
        token_env=_get_str(
            data, "token_env", default=DEFAULT_BITBUCKET_TOKEN_ENV, where="bitbucket"
        ),
    )


def _parse_kubernetes(raw: object) -> KubernetesConfig:
    data = _as_section(raw, "kubernetes")
    _reject_secret_keys(data, where="kubernetes")
    _reject_unknown_keys(
        data,
        allowed=("api_endpoint", "ca_path", "namespaces", "token_env"),
        where="kubernetes",
    )
    return KubernetesConfig(
        api_endpoint=_get_str_or_none(data, "api_endpoint", where="kubernetes"),
        ca_path=_get_str_or_none(data, "ca_path", where="kubernetes"),
        namespaces=_get_str_tuple(data, "namespaces", where="kubernetes"),
        token_env=_get_str(
            data, "token_env", default=DEFAULT_K8S_TOKEN_ENV, where="kubernetes"
        ),
    )


def _require_live_keys(config: Config) -> None:
    missing: list[str] = []
    if not config.bitbucket.base_url:
        missing.append("bitbucket.base_url")
    if not config.bitbucket.project_keys:
        missing.append("bitbucket.project_keys")
    if not config.kubernetes.api_endpoint:
        missing.append("kubernetes.api_endpoint")
    if not config.kubernetes.namespaces:
        missing.append("kubernetes.namespaces")
    if missing:
        raise ConfigError(
            "mode 'live' requires the following config keys, which are missing "
            "or empty: " + ", ".join(missing)
        )


def _as_section(raw: object, name: str) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"'{name}' must be a mapping, got {type(raw).__name__}")
    return raw


def _reject_unknown_keys(data: dict, allowed: tuple[str, ...], where: str) -> None:
    unknown = sorted(str(k) for k in data if k not in allowed)
    if unknown:
        raise ConfigError(
            f"unknown config key(s) in {where}: {', '.join(unknown)} "
            f"(allowed: {', '.join(allowed)})"
        )


def _reject_secret_keys(data: dict, where: str) -> None:
    # Defense in depth: even before the unknown-key check message, tell the
    # user explicitly that secret VALUES never belong in the config file.
    bad = sorted(str(k) for k in data if str(k).lower() in _SECRET_LIKE_KEYS)
    if bad:
        raise ConfigError(
            f"config key(s) {', '.join(bad)} in {where} look like inline secrets. "
            "Secrets must come from environment variables only; the config file "
            "may only name the variable via 'token_env'."
        )


def _get_str(data: dict, key: str, default: str, where: str = "top level") -> str:
    value = data.get(key, default)
    if not isinstance(value, str):
        raise ConfigError(f"'{key}' in {where} must be a string, got {type(value).__name__}")
    return value


def _get_str_or_none(data: dict, key: str, where: str = "top level") -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"'{key}' in {where} must be a string, got {type(value).__name__}")
    return value


def _get_str_tuple(data: dict, key: str, where: str = "top level") -> tuple[str, ...]:
    value = data.get(key)
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"'{key}' in {where} must be a list of strings, got {value!r}")
    return tuple(value)
