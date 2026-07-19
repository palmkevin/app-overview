"""Offline sanity checks for the k3s deploy manifests (issue #16).

kubectl/kubeconform cannot run in CI (no cluster, no egress), so this is the
offline validation: every manifest in deploy/k8s/ must be parseable YAML with
the basic k8s object shape, the nginx Deployment must match the k8s
annotations declared in the root catalog-info.yaml (resolved via the default
annotation prefix from the config module — never hardcoded here), and the
generator ClusterRole must stay strictly read-only (PRD §7).
"""

from pathlib import Path

import pytest
import yaml

from catalog_generator.config import DEFAULT_ANNOTATION_PREFIX

REPO_ROOT = Path(__file__).resolve().parent.parent
K8S_DIR = REPO_ROOT / "deploy" / "k8s"
ROOT_MANIFEST = REPO_ROOT / "catalog-info.yaml"

MANIFEST_FILES = sorted(K8S_DIR.glob("*.yaml"))


def _load_docs(path: Path) -> list[dict]:
    docs = [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d is not None]
    assert docs, f"{path.name} contains no YAML documents"
    return docs


def _all_docs() -> list[tuple[str, dict]]:
    return [(path.name, doc) for path in MANIFEST_FILES for doc in _load_docs(path)]


def _find_docs(kind: str) -> list[dict]:
    return [doc for _, doc in _all_docs() if doc.get("kind") == kind]


def _catalog_annotations() -> dict:
    manifest = yaml.safe_load(ROOT_MANIFEST.read_text(encoding="utf-8"))
    return manifest["metadata"]["annotations"]


def test_expected_manifest_files_exist():
    names = {path.name for path in MANIFEST_FILES}
    assert {
        "namespace.yaml",
        "deployment.yaml",
        "service.yaml",
        "ingress.yaml",
        "generator-rbac.yaml",
    } <= names


@pytest.mark.parametrize(
    ("filename", "doc"),
    _all_docs(),
    ids=lambda value: value if isinstance(value, str) else value.get("kind", "?"),
)
def test_every_document_has_k8s_object_shape(filename: str, doc: dict):
    assert isinstance(doc, dict), f"{filename}: document is not a mapping"
    assert doc.get("apiVersion"), f"{filename}: missing apiVersion"
    assert doc.get("kind"), f"{filename}: missing kind"
    assert doc.get("metadata", {}).get("name"), f"{filename}: missing metadata.name"


def test_deployment_matches_catalog_info_annotations():
    annotations = _catalog_annotations()
    expected_name = annotations[f"{DEFAULT_ANNOTATION_PREFIX}/k8s-deployment"]
    expected_namespace = annotations[f"{DEFAULT_ANNOTATION_PREFIX}/k8s-namespace"]

    (deployment,) = _find_docs("Deployment")
    assert deployment["metadata"]["name"] == expected_name
    assert deployment["metadata"]["namespace"] == expected_namespace

    (namespace,) = _find_docs("Namespace")
    assert namespace["metadata"]["name"] == expected_namespace


def test_deployment_serves_from_configmap_with_readiness_probe():
    (deployment,) = _find_docs("Deployment")
    spec = deployment["spec"]
    assert spec["replicas"] == 1

    pod_spec = spec["template"]["spec"]
    (container,) = pod_spec["containers"]
    assert container["readinessProbe"]["httpGet"]["path"] == "/"
    assert container["resources"]["requests"] and container["resources"]["limits"]

    (mount,) = container["volumeMounts"]
    assert mount["mountPath"] == "/usr/share/nginx/html"
    (volume,) = pod_spec["volumes"]
    assert volume["configMap"]["name"] == "catalog-html"


def test_generator_clusterrole_is_strictly_readonly():
    (role,) = _find_docs("ClusterRole")
    allowed_verbs = {"get", "list"}
    allowed_resources = {"deployments", "replicasets", "pods", "namespaces"}

    rules = role["rules"]
    assert rules, "ClusterRole has no rules"
    seen_resources: set[str] = set()
    for rule in rules:
        assert set(rule["verbs"]) <= allowed_verbs, f"forbidden verbs in {rule}"
        assert set(rule["resources"]) <= allowed_resources, f"forbidden resources in {rule}"
        seen_resources |= set(rule["resources"])
    assert seen_resources == allowed_resources

    # Binding wires the role to the generator ServiceAccount, nothing else.
    (binding,) = _find_docs("ClusterRoleBinding")
    assert binding["roleRef"]["name"] == role["metadata"]["name"]
    (subject,) = binding["subjects"]
    assert subject["kind"] == "ServiceAccount"
    (account,) = _find_docs("ServiceAccount")
    assert subject["name"] == account["metadata"]["name"]
    assert subject["namespace"] == account["metadata"]["namespace"]


def test_ingress_uses_placeholder_host_and_no_tls():
    (ingress,) = _find_docs("Ingress")
    hosts = [rule["host"] for rule in ingress["spec"]["rules"]]
    assert hosts == ["apps.internal.example.com"]  # placeholder until migration
    assert "tls" not in ingress["spec"], "no TLS secrets in-repo (PRD §7)"
