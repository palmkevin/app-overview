# Deploying the App Catalog to k3s (prepared for migration)

Manifests and publish mechanism for hosting the generated catalog page on the
company k3s cluster behind nginx. **Nothing here can be applied or tested in
the GitHub phase** — the cluster is unreachable, so these files are reviewed
and validated offline only (`tests/test_deploy_manifests.py` YAML-sanity-checks
them in CI). All hostnames/registries are placeholders.

## What's in here

| File | Purpose |
|---|---|
| `k8s/namespace.yaml` | Namespace `tools` (must match the `k8s-namespace` annotation in the root `catalog-info.yaml`) |
| `k8s/deployment.yaml` | nginx Deployment `app-catalog-nginx` (must match the `k8s-deployment` annotation), serving the page from a ConfigMap volume |
| `k8s/service.yaml` | ClusterIP Service |
| `k8s/ingress.yaml` | Ingress, placeholder host `apps.internal.example.com`, internal-only (no TLS secrets in-repo) |
| `k8s/generator-rbac.yaml` | ServiceAccount + ClusterRole + ClusterRoleBinding for the **generator** (read-only, PRD §7) |
| `publish.sh` | What the Jenkins job runs after generation to push `catalog.html`/`catalog.json` into the cluster |

The `catalog-html` ConfigMap itself is deliberately **not** a manifest — it is
generated content, created and updated only by `publish.sh`.

## Prerequisites

- A kubeconfig/context for the target cluster with permission to apply the
  manifests (`kubectl apply` into namespace `tools` + the cluster-scoped RBAC
  objects). This is the *deployer's* access, needed once.
- For the recurring **generator** runs: apply `k8s/generator-rbac.yaml` first,
  then store the `app-catalog-generator` ServiceAccount token in the Jenkins
  credentials store. The generator receives it only via the env var named by
  the `kubernetes.token_env` config key (default `CATALOG_K8S_TOKEN`) — never
  from a config file. The ClusterRole grants exactly `get`+`list` on
  `deployments`, `replicasets`, `pods`, `namespaces`: no secrets, no writes.

## Apply (one-time / on manifest change)

```bash
kubectl apply -f deploy/k8s/
```

Order does not matter within the directory (kubectl sorts namespaces first).
The nginx pod stays **unready** until the first publish creates the
`catalog-html` ConfigMap — that is expected.

## Publish (every generator run)

```bash
python -m catalog_generator --mode live --output out/
deploy/publish.sh out/
```

`publish.sh` renders the ConfigMap client-side and applies it:

```bash
kubectl create configmap catalog-html \
  --from-file=catalog.html --from-file=catalog.json \
  -n tools --dry-run=client -o yaml | kubectl apply -f -
```

The mounted volume picks up the new content **without a pod restart**, but
only after the kubelet sync loop runs — worst case ~1 minute (kubelet
`syncFrequency` + ConfigMap cache TTL). nginx serves static files from disk,
so no reload is needed. For an immediate cutover:
`kubectl rollout restart deployment/app-catalog-nginx -n tools`.

## Roll back

- **Bad page content:** re-run `publish.sh` against a previous known-good
  output directory (or re-apply a saved copy of the previous ConfigMap YAML —
  `kubectl get configmap catalog-html -n tools -o yaml` before publishing if
  you want a snapshot).
- **Bad Deployment change:** `kubectl rollout undo deployment/app-catalog-nginx -n tools`
  (ConfigMap content is not versioned by rollouts — content rollback is always
  the re-publish path above).

## Decision: ConfigMap vs. PVC for the page content

**Chosen: ConfigMap.** No storage provisioning, atomic updates via
`kubectl apply`, trivially rollback-able by re-publishing, and the kubelet
propagates updates to the mounted volume automatically.

**Caveat:** the API server caps a ConfigMap at **~1 MiB** (etcd limit).
`catalog.html` + `catalog.json` are a few hundred KiB for the expected ~15
apps, so there is ample headroom — but the publish fails hard ("Too long: must
have at most 1048576 bytes") if the catalog ever outgrows it.

**PVC fallback (if the cap is ever hit):** replace the `configMap` volume in
`k8s/deployment.yaml` with a small (e.g. 100 Mi) ReadWriteOnce
PersistentVolumeClaim on k3s' default `local-path` StorageClass, and change
`publish.sh` to copy the files into the running pod's volume
(`kubectl cp`, or a short-lived Job that mounts the PVC and writes the files).
Costs: node-pinned storage, a copy step that is not atomic, and manual
rollback snapshots.

## Security notes (PRD §7)

- Page is internal-only: the Ingress carries commented internal-only
  annotation placeholders and a placeholder host; no TLS secrets in the repo.
- The generator RBAC is strictly read-only (`get`/`list` on the four listed
  resources); `tests/test_deploy_manifests.py` fails CI if anyone widens it.
- No real company hostnames, registries, or tokens anywhere in `deploy/` —
  placeholders until migration.
