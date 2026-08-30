# Local Hetzner VM test

Provision a throwaway Hetzner VM from your laptop with Terraform, start the
release Compose stack via cloud-init, smoke-test the Public API, then destroy.

**Costs real Hetzner money.** Destroy when finished.

State lives in **Hetzner Object Storage** (S3-compatible) — same backend style as
prod/CI, with a dedicated laptop state key.

For long-lived production deploys (stable state key, firewall lockdown), see
[deploy-hetzner.md](deploy-hetzner.md) and [infra/README.md](https://github.com/inherent-prime/inherent/blob/main/infra/README.md).

## Prerequisites

| Requirement | Notes |
| --- | --- |
| Hetzner Cloud API token | `HCLOUD_TOKEN` env var |
| Hetzner Object Storage S3 keys | `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` |
| Object Storage bucket + endpoint | In `backend.hcl` (see step 2) |
| SSH key pair | Default path: `~/.ssh/id_ed25519.pub` |
| Terraform ≥ 1.5 | Lockfile targets ~1.9; CI uses 1.9.8 |
| Published GHCR image tag | Prefer a versioned tag; avoid bare `latest` for search e2e |
| Repo checkout | Commands assume repository root unless noted |

You do **not** need GitHub Actions secrets if the files above exist locally.

## What gets created

Terraform creates:

1. `hcloud_ssh_key` — your public key registered with Hetzner
2. `hcloud_firewall` — inbound TCP 22, TCP 18000, ICMP
3. `hcloud_server` — Ubuntu 24.04 VM with cloud-init user data

Cloud-init on first boot:

1. Installs Docker Engine + Compose plugin
2. Creates `/opt/inherent`
3. Downloads `docker-compose.release.yml` for `compose_git_ref`
4. Writes `/opt/inherent/.env` (defaults or `env_file_content`)
5. Runs `docker compose ... up -d`

## Steps

### 1. Load credentials

Export these env vars by whatever means you use (shell profile, secret manager,
direnv, etc.). Values must not be committed to the repo:

```bash
export HCLOUD_TOKEN="<hetzner-cloud-api-token>"
export AWS_ACCESS_KEY_ID="<hetzner-object-storage-access-key>"
export AWS_SECRET_ACCESS_KEY="<hetzner-object-storage-secret-key>"
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-eu-central}"
```

Do not put tokens or S3 keys in Terraform files. The `hcloud` provider reads
`HCLOUD_TOKEN`; the S3 backend reads `AWS_*`.

### 2. Configure remote state (`backend.hcl`)

```bash
cd infra
cp backend.hcl.example backend.hcl   # only if you do not already have one
```

Edit `backend.hcl` (gitignored). Laptop tests should use a **dedicated state key**
— never `inherent/prod/...` and never CI’s `inherent/ci/<run_id>/...`:

```hcl
bucket = "inherent-tfstate-test"
key    = "inherent/local/laptop/terraform.tfstate"

endpoints = {
  s3 = "https://fsn1.your-objectstorage.com"
}

skip_credentials_validation = true
skip_metadata_api_check     = true
skip_region_validation      = true
skip_requesting_account_id  = true
use_path_style              = true
```

Init (from `infra/`):

```bash
terraform init -input=false -reconfigure -backend-config=backend.hcl
```

### 3. Configure variables

```bash
cp terraform.tfvars.example terraform.tfvars
```

Edit `terraform.tfvars`. Recommended for a local stack test:

```hcl
server_name         = "inherent-local-test"
server_type         = "cpx32"   # headroom for TEI + full stack; cpx22 is tighter
server_image        = "ubuntu-24.04"
server_location     = "fsn1"
server_backups      = false
ssh_public_key_path = "~/.ssh/id_ed25519.pub"
ssh_key_name        = "inherent-local-test-key"
inherent_version    = "0.4.1"   # pin a published tag; avoid surprise :latest
compose_git_ref     = "main"    # ref that has docker-compose.release.yml
environment         = "local-test"
```

Optional: inject **strong** app secrets at apply time (omit → TF default `.env`
with `changeme` — fine only for short-lived smoke of cloud-init):

```hcl
env_file_content = <<-EOF
INHERENT_VERSION=0.4.1
POSTGRES_USER=postgres
POSTGRES_PASSWORD=<strong-random>
POSTGRES_DB=knowledge_base
MONGODB_URI=mongodb://mongodb:27017
MONGODB_DB_NAME=main
WEAVIATE_URL=http://weaviate:8080
WEAVIATE_API_KEY=<strong-random>
REDIS_URL=redis://valkey:6379
MQ_REDIS_URL=redis://valkey:6379
AWS_ACCESS_KEY_ID=S3RVER
AWS_SECRET_ACCESS_KEY=S3RVER
AWS_REGION=us-east-1
AWS_S3_REGION=us-east-1
AWS_S3_BUCKET=inherent-documents
AWS_S3_ENDPOINT=http://s3rver:9000
EMBEDDING_MODEL_ID=BAAI/bge-small-en-v1.5
EMBEDDING_SERVICE_URL=http://text-embeddings-inference:80
EMBEDDING_DIM=384
EMBEDDING_ENABLED=true
TEMPORAL_ENABLED=true
TEMPORAL_HOST=temporal:7233
TEMPORAL_NAMESPACE=default
TEMPORAL_TASK_QUEUE=document-ingestion
INGESTION_API_KEY=<strong-random>
LOG_LEVEL=INFO
ENVIRONMENT=local-test
EOF
```

Generate secrets locally, for example:

```bash
openssl rand -base64 32 | tr -d '/+=' | head -c 40
```

Notes:

- `env_file_content` is `sensitive = true` (CLI redaction only). Values still
  land in **remote state** (protect the Object Storage bucket) and cloud-init
  **user_data** / instance metadata.
- Temporal has **no separate secret**. Server uses `POSTGRES_PASSWORD` via
  Compose (`POSTGRES_PWD`). Release compose **hardcodes** worker
  `TEMPORAL_HOST` / `TEMPORAL_NAMESPACE` / `TEMPORAL_TASK_QUEUE` — see Temporal
  section below.
- `terraform.tfvars` and `backend.hcl` are gitignored under `infra/`.

### 4. Plan and apply

```bash
terraform plan
terraform apply
```

Confirm with `yes`. Capture the IP:

```bash
export SERVER_IPV4="$(terraform output -raw server_ipv4)"
echo "$SERVER_IPV4"
```

### 5. Wait for cloud-init and health

```bash
ssh -o StrictHostKeyChecking=accept-new "root@${SERVER_IPV4}" \
  'cloud-init status --wait'

ssh "root@${SERVER_IPV4}" \
  'docker compose -f /opt/inherent/docker-compose.release.yml ps'

curl -fsS "http://${SERVER_IPV4}:18000/health"
```

Typical first-boot time: a few minutes (Docker install + image pulls).

If cloud-init fails:

```bash
ssh "root@${SERVER_IPV4}" \
  'cloud-init status; journalctl -u cloud-final --no-pager -n 80'
```

### 6. Bootstrap API key (optional)

Protected Public API routes need a workspace + API key. From **repository root**:

```bash
export SERVER_IPV4="$(cd infra && terraform output -raw server_ipv4)"
export API_KEY="ink_local_$(openssl rand -hex 16)"

ssh "root@${SERVER_IPV4}" \
  "API_KEY=${API_KEY} bash -s" < scripts/dev/bootstrap.sh

curl -sS -X POST "http://${SERVER_IPV4}:18000/v1/search" \
  -H "X-API-Key: ${API_KEY}" \
  -H "X-Workspace-Id: ws_local_001" \
  -H "Content-Type: application/json" \
  -d '{"query":"ping","limit":1}'
```

Release compose container names match bootstrap defaults
(`inherent-oss-postgres`, `inherent-oss-mongodb`).

### 7. Optional: public-api compose tests against the VM

```bash
export PUBLIC_API_URL="http://${SERVER_IPV4}:18000"
export INTEGRATION_API_KEY="${API_KEY}"
export INTEGRATION_WORKSPACE_ID=ws_local_001
export INTEGRATION_TIMEOUT=600

cd services/inh-public-api-svc
uv sync --frozen --extra dev --group dev
uv run pytest -m compose
```

### 8. Destroy

Always destroy when the test is done (same env vars + `backend.hcl` as apply):

```bash
cd infra
# reload AWS_* / HCLOUD_TOKEN if this is a new shell (step 1)
terraform destroy
# confirm yes
```

If apply succeeded but state is missing or destroy fails, delete the server and
SSH key named in `terraform.tfvars` in the Hetzner Cloud Console (or API).

## Temporal on this path

| Item | Source |
| --- | --- |
| Temporal container | `docker-compose.release.yml` (`temporalio/auto-setup`) |
| DB password for Temporal | `POSTGRES_PASSWORD` in `/opt/inherent/.env` → Temporal `POSTGRES_PWD` |
| Temporal DBs created | Postgres DBs `temporal` + `temporal_visibility` (verified) |
| Client host / namespace / queue in `.env` | Written by TF defaults or `env_file_content` |
| Worker values that actually apply | Release compose **hardcodes** on `inh-ingestion-svc`: `TEMPORAL_HOST=temporal:7233`, `TEMPORAL_NAMESPACE=default`, `TEMPORAL_TASK_QUEUE=document-ingestion` |
| GitHub / Hetzner “Temporal secret” | **None** |

**Implication:** a custom `TEMPORAL_NAMESPACE` in `env_file_content` updates
`/opt/inherent/.env` only. The release ingestion worker still uses `default`
until compose interpolates those vars (it does not today).

Demo auto-setup is not production Temporal. Hardening notes:
[deploy/production.md](../deploy/production.md).

## Verified (live Hetzner laptop path)

| Check | Result |
| --- | --- |
| `HCLOUD_TOKEN` set | Works with Hetzner API |
| S3 `AWS_*` + `backend.hcl` | `terraform init -reconfigure -backend-config=backend.hcl` succeeds |
| Apply / cloud-init / stack | Creates SSH key, firewall, server; compose healthy |
| Strong secrets in `/opt/inherent/.env` | Hash-matched to apply-time values (not `changeme`) |
| Weaviate API key required | No key → HTTP 401; with key → 200 on `/v1/meta` |
| Temporal healthy + Postgres DBs | Health SERVING; `temporal` / `temporal_visibility` present |
| `/health` Public API | HTTP 200 |
| Bootstrap + search with `:latest` | API key works; search can fail with **Weaviate 401 from public-api** if published image lacks Bearer client — pin a fixed release tag / republish; see [audit/act-hetzner-e2e-weaviate-401.md](../audit/act-hetzner-e2e-weaviate-401.md) |
| Destroy | Removes all four resources |

Prefer a **versioned** `inherent_version` (e.g. `0.4.x` after a known-good publish).

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `terraform init` wants S3 / AWS creds | Export `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`; confirm `backend.hcl` |
| State key conflict | Use `inherent/local/...` — do not share prod or CI keys |
| Empty or wrong SSH | `ssh_public_key_path` exists; key registered as `ssh_key_name` |
| Compose file download fails | `compose_git_ref` exists on GitHub with `docker-compose.release.yml` |
| Image pull fails | `inherent_version` is a published GHCR tag for both services |
| `/health` never ready | `docker compose ... ps` and `docker compose ... logs` on the VM |
| Weaviate 401 via public-api | Stale `public-api-svc` image without Bearer client; see [audit/act-hetzner-e2e-weaviate-401.md](../audit/act-hetzner-e2e-weaviate-401.md) |
| Accidental bill | `terraform destroy` with same backend; console cleanup if state lost |

## Related

| Doc | When |
| --- | --- |
| [deploy-hetzner.md](deploy-hetzner.md) | Long-lived VM + stable Object Storage state key |
| [infra/README.md](https://github.com/inherent-prime/inherent/blob/main/infra/README.md) | Full infra layout + CI e2e secrets |
| [testing.md](../testing.md) | CI Hetzner e2e vs local compose tests |
| [deploy/production.md](../deploy/production.md) | Secrets / Temporal hardening checklist |
| [.env.example](https://github.com/inherent-prime/inherent/blob/main/.env.example) | Full env var reference |
