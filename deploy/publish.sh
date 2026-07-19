#!/usr/bin/env bash
# Publish the generated catalog page into the cluster.
#
# This is what the future Jenkins job runs right after
#   python -m catalog_generator --mode live --output <out-dir>
# It (re)creates the "catalog-html" ConfigMap that the app-catalog-nginx
# Deployment mounts at /usr/share/nginx/html (see deploy/k8s/deployment.yaml).
#
# NOT runnable in the GitHub phase: there is no reachable cluster. Everything
# here is placeholders; kubectl context/kubeconfig comes from the Jenkins
# agent's environment after migration.
#
# Usage: deploy/publish.sh [out-dir]   (default: out)
#   CATALOG_NAMESPACE   override the target namespace (default: tools)

set -euo pipefail

OUT_DIR="${1:-out}"
NAMESPACE="${CATALOG_NAMESPACE:-tools}"

for f in catalog.html catalog.json; do
  if [[ ! -f "${OUT_DIR}/${f}" ]]; then
    echo "error: ${OUT_DIR}/${f} not found — run the generator first" >&2
    exit 1
  fi
done

# ConfigMap size caveat: the API caps a ConfigMap at ~1 MiB. The generated
# page is far below that; if this ever fails with "Too long", switch to the
# PVC fallback documented in deploy/README.md.
kubectl create configmap catalog-html \
  --from-file="catalog.html=${OUT_DIR}/catalog.html" \
  --from-file="catalog.json=${OUT_DIR}/catalog.json" \
  -n "${NAMESPACE}" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "ConfigMap catalog-html applied in namespace ${NAMESPACE}."

# Propagation note: a ConfigMap mounted as a volume is refreshed by the
# kubelet sync loop — the running nginx picks up the new files WITHOUT a
# restart, but only after up to ~1 minute (kubelet syncFrequency + cache
# TTL). nginx serves static files straight from disk, so no reload signal
# is needed. If an immediate cutover is ever required, force new pods:
#
#   kubectl rollout restart deployment/app-catalog-nginx -n "${NAMESPACE}"
#
# (Not done by default — the ~1 minute delay is fine for a page regenerated
# every 30 minutes.)
echo "Note: the mounted volume updates within ~1 minute (kubelet sync)."
