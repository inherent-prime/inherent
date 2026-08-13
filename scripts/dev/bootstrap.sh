#!/usr/bin/env bash
#
# Local OSS bootstrap (#5).
#
# Creates the records a fresh local stack needs before any protected public-API
# call works, in BOTH control-plane stores the auth flow reads:
#   1. PostgreSQL  api_keys  row  (key validation)
#   2. MongoDB     workspaces doc (workspace-ownership resolution)
#
# Seeds ONE principal by default, and a SECOND one only on a local dev stack
# (see the SEED_PRINCIPAL_B gate below). Principal B exists so the tenancy
# isolation E2E (tests/integration/test_compose_tenancy.py) can prove the
# workspace boundary with two REAL callers instead of one caller plus a made-up
# workspace id: an unseeded workspace is rejected at the KEY-BINDING check,
# which is a different (and weaker) assertion than "a legitimate, fully
# provisioned tenant cannot see another tenant's content". Cross-tenant leakage
# is this product's worst failure mode (#1, ADR 0002), so the fixture for
# testing it belongs in the standard local bootstrap rather than in a test's
# own setup.
#
# Idempotent: safe to re-run. Uses ON CONFLICT / upsert.
#
# !! LOCAL / DEV ONLY !!
# The default key values are well-known development placeholders. Never run this
# against a production database with the defaults, and never reuse those keys
# outside local development. README.md and docs/deploy/production.md do document
# running this script against a real deployment with API_KEY/WORKSPACE_ID
# overridden -- which is exactly why principal B is gated rather than
# unconditional (see SEED_PRINCIPAL_B).
#
# Configurable via environment (defaults match the Makefile):
#   API_KEY, KEY_ID, WORKSPACE_ID, USER_ID, KEY_NAME, WORKSPACE_NAME
#   SEED_PRINCIPAL_B, API_KEY_B, KEY_ID_B, WORKSPACE_ID_B, USER_ID_B,
#     KEY_NAME_B, WORKSPACE_NAME_B
#   PG_CONTAINER, PG_USER, PG_DB
#   MONGO_CONTAINER, MONGO_DB
set -euo pipefail

# The well-known local dev key. Also the marker for "this is a local dev stack"
# in the principal-B gate below.
LOCAL_DEV_API_KEY="ink_dev_local_key_001"

# Principal A -- the default local dev identity every example and every
# single-tenant compose test uses.
API_KEY="${API_KEY:-$LOCAL_DEV_API_KEY}"
# Empty => let Postgres mint a uuid. NOT a literal default: key_id is UNIQUE
# while the upsert's arbiter is key_hash, so a fixed key_id makes a re-run with
# a ROTATED key value (new hash, same key_id) fail hard on the unique
# constraint instead of inserting a second row. A caller who wants a stable,
# greppable id can still set KEY_ID explicitly and accept that constraint.
KEY_ID="${KEY_ID:-}"
WORKSPACE_ID="${WORKSPACE_ID:-ws_local_001}"
USER_ID="${USER_ID:-local-dev-user}"
KEY_NAME="${KEY_NAME:-Local Dev Key}"
WORKSPACE_NAME="${WORKSPACE_NAME:-Local Dev Workspace}"

# Principal B -- a SEPARATE user owning a SEPARATE workspace. Deliberately not
# a second key for A's user: a key for the same owner would still be authorised
# for A's workspace whenever it is user-scoped, so it could never demonstrate
# the boundary.
API_KEY_B="${API_KEY_B:-ink_dev_local_key_002}"
KEY_ID_B="${KEY_ID_B:-}"
WORKSPACE_ID_B="${WORKSPACE_ID_B:-ws_local_002}"
USER_ID_B="${USER_ID_B:-user_local_002}"
KEY_NAME_B="${KEY_NAME_B:-Local Dev Key B (tenancy isolation)}"
WORKSPACE_NAME_B="${WORKSPACE_NAME_B:-Local Dev Workspace B}"

PG_CONTAINER="${PG_CONTAINER:-inherent-oss-postgres}"
PG_USER="${PG_USER:-postgres}"
PG_DB="${PG_DB:-knowledge_base}"

MONGO_CONTAINER="${MONGO_CONTAINER:-inherent-oss-mongodb}"
MONGO_DB="${MONGO_DB:-main}"

# ---------------------------------------------------------------------------
# Principal B gate. Seeding a SECOND well-known, write-capable key is only ever
# safe on a throwaway local stack, and this script is NOT only run on one:
# README's "run from published images" flow and
# docs/deploy/production.md §8 both point real deployments at it, and
# .github/workflows/hetzner-e2e.yml pipes it onto a VM with a public IP. Those
# callers override API_KEY (Hetzner mints a random per-run `ink_ci_<32 hex>`),
# so an UNCONDITIONAL second seed would plant `ink_dev_local_key_002` --
# active, read/write/search -- on an internet-reachable box. Hence:
#
#   seed B  <=>  SEED_PRINCIPAL_B=1  OR  API_KEY is the local dev default
#
# The API_KEY test is what keeps `make bootstrap`, e2e-smoke.yml and
# integration.yml (all of which bootstrap with the defaults) seeding B without
# a flag -- the tenancy tests FAIL rather than skip when B is missing, so the
# CI lanes must keep getting it. Anyone who deliberately wants a second
# principal on a non-default stack sets SEED_PRINCIPAL_B=1 and should override
# API_KEY_B too.
# ---------------------------------------------------------------------------
SEED_PRINCIPAL_B="${SEED_PRINCIPAL_B:-}"
if [[ "$SEED_PRINCIPAL_B" == "1" ]]; then
  seed_b=true
  seed_b_reason="SEED_PRINCIPAL_B=1"
elif [[ "$API_KEY" == "$LOCAL_DEV_API_KEY" ]]; then
  seed_b=true
  seed_b_reason="API_KEY is the local dev default"
else
  seed_b=false
  seed_b_reason="API_KEY is not the local dev default and SEED_PRINCIPAL_B is unset"
fi

keys_to_check=("$API_KEY")
if [[ "$seed_b" == true ]]; then
  keys_to_check+=("$API_KEY_B")
fi
for key in "${keys_to_check[@]}"; do
  if [[ "$key" != ink_* ]]; then
    echo "Error: API keys must start with 'ink_' (public API rejects other prefixes)." >&2
    exit 1
  fi
done

if [[ "$seed_b" == true ]]; then
  if [[ "$API_KEY" == "$API_KEY_B" || "$WORKSPACE_ID" == "$WORKSPACE_ID_B" || "$USER_ID" == "$USER_ID_B" ]]; then
    echo "Error: principals A and B must differ in key, workspace and user." >&2
    exit 1
  fi
fi

container_running() {
  docker ps --format '{{.Names}}' | grep -qx "$1"
}

for c in "$PG_CONTAINER" "$MONGO_CONTAINER"; do
  if ! container_running "$c"; then
    echo "Error: container '$c' is not running. Start the stack first (e.g. 'make dev')." >&2
    exit 1
  fi
done

# seed_principal <api_key> <key_id> <user_id> <workspace_id> <key_name> <workspace_name>
#
# Writes the api_keys row and the workspaces doc for one principal. Both
# statements are upserts keyed on the identity of the record (key_hash /
# workspace _id), so re-running is a no-op that also REPAIRS a row someone
# revoked or re-pointed by hand.
seed_principal() {
  local api_key="$1" key_id="$2" user_id="$3" workspace_id="$4" key_name="$5" workspace_name="$6"
  # Log the prefix, never the key: this script's output ends up in CI logs, and
  # on the Hetzner lane the key is a masked per-run secret.
  local key_prefix="${api_key:0:12}"

  # 1. PostgreSQL api_keys row. key_hash is sha256(hex) of the full key;
  #    key_prefix is the first 12 chars (matches services validation). Both are
  #    computed IN Postgres so the script needs no sha256 binary (macOS ships
  #    `shasum`, Linux `sha256sum`) and cannot disagree with the server's own
  #    hashing.
  #
  #    key_id: an EMPTY argument means "mint a uuid". key_id is UNIQUE but the
  #    upsert arbitrates on key_hash, so hard-coding it would break the rotation
  #    path -- re-running with a changed key value produces a new hash (no
  #    conflict, so a real INSERT) carrying an already-taken key_id, and
  #    Postgres rejects the whole statement. `nullif`+`coalesce` keeps the
  #    default behaviour (a fresh uuid per row) while still letting a caller
  #    pin a readable id.
  echo "  - PostgreSQL api_keys ($PG_DB): ${key_prefix}... -> ${workspace_id} ..."
  docker exec -i "$PG_CONTAINER" psql -v ON_ERROR_STOP=1 -U "$PG_USER" -d "$PG_DB" -c \
    "INSERT INTO api_keys
       (key_id, key_hash, key_prefix, user_id, workspace_id, name, status, permissions, rate_limit)
     VALUES (
       coalesce(nullif('${key_id}', ''), gen_random_uuid()::text),
       encode(sha256('${api_key}'::bytea), 'hex'),
       left('${api_key}', 12),
       '${user_id}',
       '${workspace_id}',
       '${key_name}',
       'active',
       '[\"read\",\"write\",\"search\"]',
       1000
     )
     ON CONFLICT (key_hash) DO UPDATE
       SET status = 'active',
           user_id = EXCLUDED.user_id,
           workspace_id = EXCLUDED.workspace_id,
           permissions = EXCLUDED.permissions;" >/dev/null

  # 2. MongoDB workspaces doc. Ownership lookup matches on user_id and returns
  #    str(_id) as the workspace id, so _id must equal the workspace id.
  echo "  - MongoDB workspaces ($MONGO_DB): ${workspace_id} owned by ${user_id} ..."
  docker exec -i "$MONGO_CONTAINER" mongosh --quiet "$MONGO_DB" --eval \
    "db.workspaces.updateOne(
       { _id: '${workspace_id}' },
       { \$set: { user_id: '${user_id}', name: '${workspace_name}' } },
       { upsert: true }
     )" >/dev/null
}

echo "Bootstrapping workspace + API key..."

seed_principal "$API_KEY" "$KEY_ID" "$USER_ID" "$WORKSPACE_ID" "$KEY_NAME" "$WORKSPACE_NAME"

if [[ "$seed_b" == true ]]; then
  echo "Seeding second principal for tenancy tests (${seed_b_reason})..."
  seed_principal "$API_KEY_B" "$KEY_ID_B" "$USER_ID_B" "$WORKSPACE_ID_B" "$KEY_NAME_B" "$WORKSPACE_NAME_B"
else
  echo "Skipping the second (tenancy-test) principal: ${seed_b_reason}."
  echo "  No '${API_KEY_B:0:12}...' key was created. Set SEED_PRINCIPAL_B=1 (and"
  echo "  override API_KEY_B / WORKSPACE_ID_B / USER_ID_B) if you really want one."
fi

cat <<EOF

Bootstrap complete. Use these for API calls:

  API key       : ${API_KEY}
  Workspace ID  : ${WORKSPACE_ID}
  User ID       : ${USER_ID}
EOF

# Only advertise B when B actually exists -- printing credentials the script
# just decided NOT to create is how a reader ends up believing a key is live.
if [[ "$seed_b" == true ]]; then
  cat <<EOF

Second principal (tenancy isolation tests; separate user AND workspace):

  API key       : ${API_KEY_B}
  Workspace ID  : ${WORKSPACE_ID_B}
  User ID       : ${USER_ID_B}
EOF
fi

cat <<EOF

Example:
  curl -s http://localhost:18000/v1/search \\
    -H "X-API-Key: ${API_KEY}" \\
    -H "X-Workspace-Id: ${WORKSPACE_ID}" \\
    -H "Content-Type: application/json" \\
    -d '{"query":"hello","limit":3}'
EOF
