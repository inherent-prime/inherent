#!/usr/bin/env bash
#
# Local OSS bootstrap (#5).
#
# Creates the records a fresh local stack needs before any protected public-API
# call works, in BOTH control-plane stores the auth flow reads:
#   1. PostgreSQL  api_keys  row  (key validation)
#   2. MongoDB     workspaces doc (workspace-ownership resolution)
#
# Seeds TWO independent principals, not one. The second exists so the tenancy
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
# The key values are well-known development placeholders. Never run this against
# a production database and never reuse these keys outside local development.
#
# Configurable via environment (defaults match the Makefile):
#   API_KEY, KEY_ID, WORKSPACE_ID, USER_ID, KEY_NAME, WORKSPACE_NAME
#   API_KEY_B, KEY_ID_B, WORKSPACE_ID_B, USER_ID_B, KEY_NAME_B, WORKSPACE_NAME_B
#   PG_CONTAINER, PG_USER, PG_DB
#   MONGO_CONTAINER, MONGO_DB
set -euo pipefail

# Principal A -- the default local dev identity every example and every
# single-tenant compose test uses.
API_KEY="${API_KEY:-ink_dev_local_key_001}"
KEY_ID="${KEY_ID:-key_local_001}"
WORKSPACE_ID="${WORKSPACE_ID:-ws_local_001}"
USER_ID="${USER_ID:-local-dev-user}"
KEY_NAME="${KEY_NAME:-Local Dev Key}"
WORKSPACE_NAME="${WORKSPACE_NAME:-Local Dev Workspace}"

# Principal B -- a SEPARATE user owning a SEPARATE workspace. Deliberately not
# a second key for A's user: a key for the same owner would still be authorised
# for A's workspace whenever it is user-scoped, so it could never demonstrate
# the boundary.
API_KEY_B="${API_KEY_B:-ink_dev_local_key_002}"
KEY_ID_B="${KEY_ID_B:-key_local_002}"
WORKSPACE_ID_B="${WORKSPACE_ID_B:-ws_local_002}"
USER_ID_B="${USER_ID_B:-user_local_002}"
KEY_NAME_B="${KEY_NAME_B:-Local Dev Key B (tenancy isolation)}"
WORKSPACE_NAME_B="${WORKSPACE_NAME_B:-Local Dev Workspace B}"

PG_CONTAINER="${PG_CONTAINER:-inherent-oss-postgres}"
PG_USER="${PG_USER:-postgres}"
PG_DB="${PG_DB:-knowledge_base}"

MONGO_CONTAINER="${MONGO_CONTAINER:-inherent-oss-mongodb}"
MONGO_DB="${MONGO_DB:-main}"

for key in "$API_KEY" "$API_KEY_B"; do
  if [[ "$key" != ink_* ]]; then
    echo "Error: API keys must start with 'ink_' (public API rejects other prefixes)." >&2
    exit 1
  fi
done

if [[ "$API_KEY" == "$API_KEY_B" || "$WORKSPACE_ID" == "$WORKSPACE_ID_B" || "$USER_ID" == "$USER_ID_B" ]]; then
  echo "Error: principals A and B must differ in key, workspace and user." >&2
  exit 1
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

  # 1. PostgreSQL api_keys row. key_hash is sha256(hex) of the full key;
  #    key_prefix is the first 12 chars (matches services validation). Both are
  #    computed IN Postgres so the script needs no sha256 binary (macOS ships
  #    `shasum`, Linux `sha256sum`) and cannot disagree with the server's own
  #    hashing.
  echo "  - PostgreSQL api_keys ($PG_DB): ${key_id} -> ${workspace_id} ..."
  docker exec -i "$PG_CONTAINER" psql -v ON_ERROR_STOP=1 -U "$PG_USER" -d "$PG_DB" -c \
    "INSERT INTO api_keys
       (key_id, key_hash, key_prefix, user_id, workspace_id, name, status, permissions, rate_limit)
     VALUES (
       '${key_id}',
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

echo "Bootstrapping local dev workspaces + API keys (LOCAL/DEV ONLY)..."

seed_principal "$API_KEY" "$KEY_ID" "$USER_ID" "$WORKSPACE_ID" "$KEY_NAME" "$WORKSPACE_NAME"
seed_principal "$API_KEY_B" "$KEY_ID_B" "$USER_ID_B" "$WORKSPACE_ID_B" "$KEY_NAME_B" "$WORKSPACE_NAME_B"

cat <<EOF

Bootstrap complete (local/dev only). Use these for local API calls:

  API key       : ${API_KEY}
  Workspace ID  : ${WORKSPACE_ID}
  User ID       : ${USER_ID}

Second principal (tenancy isolation tests; separate user AND workspace):

  API key       : ${API_KEY_B}
  Workspace ID  : ${WORKSPACE_ID_B}
  User ID       : ${USER_ID_B}

Example:
  curl -s http://localhost:18000/v1/search \\
    -H "X-API-Key: ${API_KEY}" \\
    -H "X-Workspace-Id: ${WORKSPACE_ID}" \\
    -H "Content-Type: application/json" \\
    -d '{"query":"hello","limit":3}'
EOF
