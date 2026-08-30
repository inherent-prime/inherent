#!/usr/bin/env bash
#
# deploy-azure.sh -- one-click Azure production deploy (#326, epic #320).
#
# Provisions the full Azure stack (infra/azure/), waits for the API to come
# up healthy, bootstraps a workspace + API key the same way `make dev` does
# locally (scripts/dev/bootstrap.sh), and optionally load-tests the result.
#
# Usage:
#   scripts/deploy-azure.sh [--bootstrap-state] [--var-file <path>] [--yes]
#                            [--loadtest] [--destroy] [--skip-bootstrap-key]
#
# See docs/deploy/azure.md for the full walkthrough and the manual
# terraform-only path.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
INFRA_DIR="$REPO_ROOT/infra/azure"

BOLD='\033[1m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
RESET='\033[0m'

log()  { printf "${BOLD}[deploy-azure]${RESET} %s\n" "$1"; }
ok()   { printf "${GREEN}[deploy-azure]${RESET} %s\n" "$1"; }
warn() { printf "${YELLOW}[deploy-azure] WARN:${RESET} %s\n" "$1" >&2; }
err()  { printf "${RED}[deploy-azure] ERROR:${RESET} %s\n" "$1" >&2; }

usage() {
  cat <<'EOF'
Usage: scripts/deploy-azure.sh [OPTIONS]

  --bootstrap-state     Create the resource group + storage account + blob
                         container that hold Terraform remote state, and
                         write infra/azure/backend.hcl. Idempotent: safe to
                         re-run.
  --var-file <path>     Terraform var-file to apply/destroy with. Defaults to
                         infra/azure/terraform.tfvars (copy one of
                         infra/azure/envs/*.tfvars.example there first).
  --yes                 Non-interactive: skip the subscription confirmation
                         and apply/destroy with -auto-approve. Without it,
                         a plan is shown and you are prompted before apply.
  --loadtest             Run scripts/loadtest/k6-search.js against the new
                         deployment once the health gate passes (needs k6).
  --destroy              Tear the environment down (terraform destroy),
                         after the same confirmation gate as apply.
  --skip-bootstrap-key   Skip creating a workspace + API key after deploy.
  -h, --help              Show this help.

Docs: docs/deploy/azure.md
EOF
}

# ---------------------------------------------------------------------------
# Defaults. Overridable via env so a CI/scripted caller need not edit the
# script; the k8s object names mirror the naming this repo already uses for
# the service everywhere else (services/inh-public-api-svc, docker-compose*,
# scripts/dev/bootstrap.sh) so a default chart install lines up with no
# overrides needed.
# ---------------------------------------------------------------------------
RESOURCE_PREFIX="${RESOURCE_PREFIX:-inherent}"
ENVIRONMENT="${ENVIRONMENT:-prod}"
LOCATION="${LOCATION:-eastus2}"
NAMESPACE="${AZURE_DEPLOY_NAMESPACE:-inherent}"
SERVICE_NAME="${AZURE_DEPLOY_SERVICE_NAME:-inh-public-api-svc}"
POD_SELECTOR="${AZURE_DEPLOY_POD_SELECTOR:-app.kubernetes.io/name=inh-public-api-svc}"
WORKSPACE_ID="${AZURE_DEPLOY_WORKSPACE_ID:-ws_prod_001}"
BOOTSTRAP_USER_ID="${AZURE_DEPLOY_USER_ID:-prod-admin}"
HEALTH_TIMEOUT_SECONDS="${AZURE_DEPLOY_HEALTH_TIMEOUT_SECONDS:-1200}" # 20 min

BOOTSTRAP_STATE=0
VAR_FILE=""
AUTO_YES=0
LOADTEST=0
DESTROY=0
SKIP_BOOTSTRAP_KEY=0
EXIT_CODE=0

# Set by verify_key(); cleanup() kills it on any exit so a failed/interrupted
# run never leaves a background `kubectl port-forward` behind.
PF_PID=""
# shellcheck disable=SC2317  # invoked indirectly via `trap ... EXIT`, not a dead call
cleanup() {
  if [[ -n "$PF_PID" ]]; then
    kill "$PF_PID" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --bootstrap-state) BOOTSTRAP_STATE=1; shift ;;
      --var-file)
        [[ $# -ge 2 ]] || { err "--var-file requires a path"; exit 1; }
        VAR_FILE="$2"
        shift 2
        ;;
      --yes) AUTO_YES=1; shift ;;
      --loadtest) LOADTEST=1; shift ;;
      --destroy) DESTROY=1; shift ;;
      --skip-bootstrap-key) SKIP_BOOTSTRAP_KEY=1; shift ;;
      -h|--help) usage; exit 0 ;;
      *)
        err "Unknown option: $1"
        usage
        exit 1
        ;;
    esac
  done
}

# ---------------------------------------------------------------------------
# 1. Preflight (build-spec: "One-click script contract" step 1)
# ---------------------------------------------------------------------------
preflight() {
  local missing=0 bin
  for bin in az terraform kubectl helm jq curl openssl; do
    if ! command -v "$bin" >/dev/null 2>&1; then
      err "required tool not found on PATH: $bin"
      missing=1
    fi
  done
  if [[ "$missing" -ne 0 ]]; then
    err "install the missing tool(s) above and re-run."
    exit 1
  fi

  local account_json
  if ! account_json=$(az account show -o json 2>/dev/null); then
    err "az is not logged in. Run 'az login' (and 'az account set --subscription <id>' if needed)."
    exit 1
  fi
  SUBSCRIPTION_NAME=$(jq -r '.name' <<<"$account_json")
  SUBSCRIPTION_ID=$(jq -r '.id' <<<"$account_json")

  log "Target subscription: ${SUBSCRIPTION_NAME} (${SUBSCRIPTION_ID})"
  if [[ "$AUTO_YES" -ne 1 ]]; then
    local reply
    read -r -p "Deploy Inherent into this subscription? [y/N] " reply
    if [[ ! "$reply" =~ ^[Yy]$ ]]; then
      err "Aborted: subscription not confirmed."
      exit 1
    fi
  fi
}

# ---------------------------------------------------------------------------
# --var-file resolution. infra/azure only ships *.tfvars.example files (they
# are templates, not real config), so the default is the copy an operator is
# expected to make -- terraform.tfvars -- never one of the .example files.
# ---------------------------------------------------------------------------
resolve_var_file() {
  if [[ -n "$VAR_FILE" ]]; then
    if [[ ! -f "$VAR_FILE" ]]; then
      err "--var-file $VAR_FILE does not exist."
      exit 1
    fi
  elif [[ -f "$INFRA_DIR/terraform.tfvars" ]]; then
    VAR_FILE="$INFRA_DIR/terraform.tfvars"
  else
    err "No --var-file given and $INFRA_DIR/terraform.tfvars does not exist."
    err "Copy an example profile first, e.g.:"
    err "  cp $INFRA_DIR/envs/prod.tfvars.example $INFRA_DIR/terraform.tfvars"
    err "then edit it for your environment, or pass --var-file <path> explicitly."
    exit 1
  fi
  log "Using var file: $VAR_FILE"
}

# ---------------------------------------------------------------------------
# 2. --bootstrap-state: RG + storage account + blob container for tf state.
#    Idempotent via `az ... show || az ... create` on each resource.
# ---------------------------------------------------------------------------
state_storage_account_name() {
  # Storage account names must be 3-24 lowercase alphanumeric and globally
  # unique; derive a stable one from the prefix + a slice of the subscription
  # id so re-running always resolves to the same account.
  local base sub_suffix
  base=$(printf '%s' "${RESOURCE_PREFIX}tfstate" | tr -cd 'a-z0-9')
  sub_suffix=$(printf '%s' "$SUBSCRIPTION_ID" | tr -cd 'a-f0-9' | cut -c1-6)
  printf '%s%s' "${base:0:18}" "$sub_suffix"
}

bootstrap_state() {
  log "Bootstrapping Terraform remote state (resource group + storage account + container)..."
  local rg sa container account_key
  rg="${TF_STATE_RESOURCE_GROUP:-${RESOURCE_PREFIX}-tfstate-rg}"
  sa="${TF_STATE_STORAGE_ACCOUNT:-$(state_storage_account_name)}"
  container="${TF_STATE_CONTAINER:-tfstate}"

  if az group show --name "$rg" >/dev/null 2>&1; then
    log "Resource group $rg already exists."
  else
    log "Creating resource group $rg in $LOCATION..."
    az group create --name "$rg" --location "$LOCATION" >/dev/null
  fi

  if az storage account show --name "$sa" --resource-group "$rg" >/dev/null 2>&1; then
    log "Storage account $sa already exists."
  else
    log "Creating storage account $sa..."
    az storage account create \
      --name "$sa" \
      --resource-group "$rg" \
      --location "$LOCATION" \
      --sku Standard_GRS \
      --kind StorageV2 \
      --min-tls-version TLS1_2 \
      --allow-blob-public-access false >/dev/null
  fi

  account_key=$(az storage account keys list --account-name "$sa" --resource-group "$rg" --query '[0].value' -o tsv)

  if az storage container show --name "$container" --account-name "$sa" --account-key "$account_key" >/dev/null 2>&1; then
    log "Storage container $container already exists."
  else
    log "Creating storage container $container..."
    az storage container create --name "$container" --account-name "$sa" --account-key "$account_key" >/dev/null
  fi

  mkdir -p "$INFRA_DIR"
  cat > "$INFRA_DIR/backend.hcl" <<EOF
# Generated by scripts/deploy-azure.sh --bootstrap-state on $(date -u +%Y-%m-%dT%H:%M:%SZ).
# Gitignored -- do not commit. Template: infra/azure/backend.hcl.example.
resource_group_name = "${rg}"
storage_account_name = "${sa}"
container_name       = "${container}"
key                   = "inherent/${ENVIRONMENT}/terraform.tfstate"
EOF
  ok "Wrote $INFRA_DIR/backend.hcl"
}

# ---------------------------------------------------------------------------
# 3-4. terraform init / apply / destroy
# ---------------------------------------------------------------------------
terraform_init() {
  if [[ ! -f "$INFRA_DIR/backend.hcl" ]]; then
    err "$INFRA_DIR/backend.hcl not found."
    err "Run with --bootstrap-state first, or copy infra/azure/backend.hcl.example -> backend.hcl and fill it in."
    exit 1
  fi
  log "terraform init..."
  terraform -chdir="$INFRA_DIR" init -input=false -backend-config=backend.hcl
}

run_destroy() {
  log "Planning destroy against subscription ${SUBSCRIPTION_NAME}..."
  if [[ "$AUTO_YES" -eq 1 ]]; then
    terraform -chdir="$INFRA_DIR" destroy -input=false -var-file="$VAR_FILE" -auto-approve
  else
    terraform -chdir="$INFRA_DIR" destroy -input=false -var-file="$VAR_FILE"
  fi
  ok "Destroy complete."
}

# `apply -auto-approve` is gated behind --yes on every path below; this is
# the ONLY function in the script that may call `apply -auto-approve`
# (tests/test_azure_terraform_guards.py pins that).
run_apply() {
  if [[ "$AUTO_YES" -eq 1 ]]; then
    log "Applying (--yes given: -auto-approve)..."
    terraform -chdir="$INFRA_DIR" apply -input=false -var-file="$VAR_FILE" -auto-approve
  else
    local plan_file
    plan_file="$(mktemp -t deploy-azure-plan.XXXXXX)"
    log "Planning..."
    terraform -chdir="$INFRA_DIR" plan -input=false -var-file="$VAR_FILE" -out="$plan_file"
    local reply
    read -r -p "Apply this plan? [y/N] " reply
    if [[ ! "$reply" =~ ^[Yy]$ ]]; then
      rm -f "$plan_file"
      err "Aborted before apply."
      exit 1
    fi
    log "Applying..."
    terraform -chdir="$INFRA_DIR" apply -input=false "$plan_file"
    rm -f "$plan_file"
  fi
}

# ---------------------------------------------------------------------------
# 5. Fetch outputs, get AKS credentials, wait for the API's readiness gate.
# ---------------------------------------------------------------------------
fetch_outputs() {
  local outputs_json
  outputs_json=$(terraform -chdir="$INFRA_DIR" output -json)
  API_FQDN=$(jq -r '.api_fqdn.value // empty' <<<"$outputs_json")
  AKS_NAME=$(jq -r '.aks_name.value // empty' <<<"$outputs_json")
  RESOURCE_GROUP=$(jq -r '.resource_group_name.value // empty' <<<"$outputs_json")

  if [[ -z "$API_FQDN" || -z "$AKS_NAME" || -z "$RESOURCE_GROUP" ]]; then
    err "terraform outputs are missing api_fqdn / aks_name / resource_group_name."
    err "Check infra/azure/outputs.tf against the build spec's cross-module interface."
    exit 1
  fi
}

get_credentials() {
  log "Fetching AKS credentials for $AKS_NAME (resource group $RESOURCE_GROUP)..."
  az aks get-credentials --resource-group "$RESOURCE_GROUP" --name "$AKS_NAME" --overwrite-existing
}

wait_for_health() {
  local url="https://${API_FQDN}/health/ready"
  local start_ts deadline_ts attempt=0 code
  start_ts=$(date +%s)
  deadline_ts=$(( start_ts + HEALTH_TIMEOUT_SECONDS ))
  log "Waiting for $url to return 200 (timeout $(( HEALTH_TIMEOUT_SECONDS / 60 ))m)..."

  while true; do
    code=$(curl -fsS -o /dev/null -w '%{http_code}' "$url" 2>/dev/null || echo "000")
    if [[ "$code" == "200" ]]; then
      printf '\n'
      ok "API is ready: $url"
      return 0
    fi
    if [[ "$(date +%s)" -ge "$deadline_ts" ]]; then
      printf '\n'
      err "Timed out after $(( HEALTH_TIMEOUT_SECONDS / 60 )) minutes waiting for $url (last status: $code)."
      exit 1
    fi
    attempt=$(( attempt + 1 ))
    printf '\r  ... still waiting (attempt %d, elapsed %ds, last status %s)  ' \
      "$attempt" "$(( $(date +%s) - start_ts ))" "$code"
    sleep 10
  done
}

# ---------------------------------------------------------------------------
# 6. Bootstrap a workspace + API key. Same two writes as scripts/dev/
#    bootstrap.sh (PostgreSQL api_keys row + MongoDB workspaces doc), run via
#    short-lived client pods inside the cluster because PG Flexible Server and
#    Cosmos DB vCore sit behind private endpoints (enable_private_endpoints
#    default true) -- reachable from AKS, not from the operator's machine.
#    DATABASE_URL / MONGODB_URI are read straight out of the running
#    public-api pod's resolved environment, so this never re-derives or
#    guesses a connection string the chart already assembled.
# ---------------------------------------------------------------------------
bootstrap_key() {
  local pod db_url mongo_uri raw_key key_prefix

  pod=$(kubectl get pods -n "$NAMESPACE" -l "$POD_SELECTOR" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
  if [[ -z "$pod" ]]; then
    err "No pod found in namespace $NAMESPACE matching selector $POD_SELECTOR."
    err "Set AZURE_DEPLOY_NAMESPACE / AZURE_DEPLOY_POD_SELECTOR if charts/inherent uses different names."
    exit 1
  fi

  db_url=$(kubectl exec -n "$NAMESPACE" "$pod" -- printenv DATABASE_URL 2>/dev/null || true)
  mongo_uri=$(kubectl exec -n "$NAMESPACE" "$pod" -- printenv MONGODB_URI 2>/dev/null || true)
  if [[ -z "$db_url" || -z "$mongo_uri" ]]; then
    err "Could not read DATABASE_URL / MONGODB_URI from pod $pod."
    exit 1
  fi

  raw_key="ink_$(openssl rand -hex 24)"
  key_prefix="${raw_key:0:12}"

  log "Seeding workspace $WORKSPACE_ID + API key (prefix ${key_prefix}...)..."

  # PostgreSQL api_keys row -- same upsert shape as scripts/dev/bootstrap.sh,
  # hashed IN Postgres so this script never needs its own sha256 and cannot
  # disagree with the server's own hashing.
  kubectl run "bootstrap-pg-$$" --rm -i --restart=Never --quiet \
    --namespace "$NAMESPACE" --image postgres:16-alpine --command -- \
    psql "$db_url" -v ON_ERROR_STOP=1 -c \
    "INSERT INTO api_keys
       (key_id, key_hash, key_prefix, user_id, workspace_id, name, status, permissions, rate_limit)
     VALUES (
       gen_random_uuid()::text,
       encode(sha256('${raw_key}'::bytea), 'hex'),
       left('${raw_key}', 12),
       '${BOOTSTRAP_USER_ID}',
       '${WORKSPACE_ID}',
       'Azure prod deploy key',
       'active',
       '[\"read\",\"write\",\"search\"]',
       1000
     )
     ON CONFLICT (key_hash) DO UPDATE
       SET status = 'active',
           user_id = EXCLUDED.user_id,
           workspace_id = EXCLUDED.workspace_id,
           permissions = EXCLUDED.permissions;" >/dev/null

  # MongoDB workspaces doc -- ownership lookup matches on user_id, _id must
  # equal the workspace id (same contract as scripts/dev/bootstrap.sh).
  kubectl run "bootstrap-mongo-$$" --rm -i --restart=Never --quiet \
    --namespace "$NAMESPACE" --image mongo:7 --command -- \
    mongosh --quiet "$mongo_uri" --eval \
    "db.workspaces.updateOne(
       { _id: '${WORKSPACE_ID}' },
       { \$set: { user_id: '${BOOTSTRAP_USER_ID}', name: 'Azure prod workspace' } },
       { upsert: true }
     )" >/dev/null

  API_KEY="$raw_key"

  # The one and only place this key is ever printed. Never log it, write it
  # to a file, or pass it to any command whose output we capture above.
  cat <<EOF

  !!! SAVE THIS NOW -- shown once, never logged, never recoverable from here !!!

  API key       : ${API_KEY}
  Workspace ID  : ${WORKSPACE_ID}
  User ID       : ${BOOTSTRAP_USER_ID}

EOF
}

# Smoke-verify the new key via a direct port-forward to the service (avoids
# depending on public DNS / ingress propagation timing, which the health-gate
# curl above already exercises against the real hostname). Non-fatal: the
# deploy has already succeeded by this point.
verify_key() {
  local local_port=18080 code
  kubectl port-forward -n "$NAMESPACE" "svc/${SERVICE_NAME}" "${local_port}:8080" \
    >/tmp/deploy-azure-portforward.log 2>&1 &
  PF_PID=$!
  sleep 2

  code=$(curl -fsS -o /dev/null -w '%{http_code}' \
    -H "X-API-Key: ${API_KEY}" \
    -H "X-Workspace-Id: ${WORKSPACE_ID}" \
    -H "Content-Type: application/json" \
    -d '{"query":"deploy smoke test","limit":1}' \
    "http://localhost:${local_port}/v1/search" 2>/dev/null || echo "000")

  kill "$PF_PID" >/dev/null 2>&1 || true
  PF_PID=""

  if [[ "$code" == "200" ]]; then
    ok "Smoke-verified: the new API key can search (HTTP 200)."
  else
    warn "Smoke search with the new key returned HTTP $code (deploy still succeeded; check the key manually)."
  fi
}

# ---------------------------------------------------------------------------
# 7. Optional load test (#320's 20 QPS target).
# ---------------------------------------------------------------------------
run_loadtest() {
  if ! command -v k6 >/dev/null 2>&1; then
    warn "k6 is not installed; skipping load test."
    warn "Install: https://k6.io/docs/get-started/installation/ (see docs/deploy/azure.md)."
    return 1
  fi
  log "Running load test against https://${API_FQDN} ..."
  API_URL="https://${API_FQDN}" API_KEY="$API_KEY" API_WORKSPACE_ID="$WORKSPACE_ID" \
    k6 run "$SCRIPT_DIR/loadtest/k6-search.js"
}

# ---------------------------------------------------------------------------
# 8. Summary
# ---------------------------------------------------------------------------
print_summary() {
  cat <<EOF

=================================================================
 Inherent -- Azure deployment summary
=================================================================
 Endpoint:         https://${API_FQDN}
 AKS cluster:      ${AKS_NAME}
 Resource group:   ${RESOURCE_GROUP}
 Docs:             docs/deploy/azure.md
EOF
  case "$LOADTEST_STATUS" in
    ran) echo " Load test:        completed -- see k6 output above" ;;
    skipped) echo " Load test:        SKIPPED (k6 not installed)" ;;
    *) : ;;
  esac
  cat <<'EOF'

 Next steps:
   - Save the API key printed above now -- it is shown once and never logged.
   - Read docs/deploy/azure.md for tuning knobs, DR, and enterprise VNet setup.
   - Run with --destroy when you are done with this environment.
=================================================================
EOF
}

main() {
  parse_args "$@"
  preflight
  resolve_var_file

  if [[ "$BOOTSTRAP_STATE" -eq 1 ]]; then
    bootstrap_state
  fi

  terraform_init

  if [[ "$DESTROY" -eq 1 ]]; then
    run_destroy
    exit 0
  fi

  run_apply
  fetch_outputs
  get_credentials
  wait_for_health

  API_KEY=""
  if [[ "$SKIP_BOOTSTRAP_KEY" -eq 1 ]]; then
    warn "Skipping workspace/API key bootstrap (--skip-bootstrap-key)."
  else
    bootstrap_key
    verify_key
  fi

  LOADTEST_STATUS="not-requested"
  if [[ "$LOADTEST" -eq 1 ]]; then
    if [[ -z "$API_KEY" ]]; then
      warn "Cannot run load test: no API key available (--skip-bootstrap-key was set)."
      LOADTEST_STATUS="skipped"
      EXIT_CODE=1
    elif run_loadtest; then
      LOADTEST_STATUS="ran"
    else
      LOADTEST_STATUS="skipped"
      EXIT_CODE=1
    fi
  fi

  print_summary
  exit "$EXIT_CODE"
}

main "$@"
