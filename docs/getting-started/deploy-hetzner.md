# Deploy to Production (Hetzner)

Use Terraform to provision a Hetzner VM with Docker, then deploy the full
Inherent stack from published images. Terraform manages infrastructure only;
Docker Compose runs the application.

## What You Will Deploy

```
                     Hetzner VM
  ┌───────────────────────────────────────────┐
  │  Docker Compose (docker-compose.release.yml) │
  │  ┌───────────┐ ┌──────────┐ ┌──────────┐ │
  │  │ PostgreSQL │ │ MongoDB  │ │ Weaviate │ │
  │  ├───────────┤ ├──────────┤ ├──────────┤ │
  │  │  Valkey   │ │ S3rver   │ │ Temporal │ │
  │  ├───────────┤ ├──────────┤ ├──────────┤ │
  │  │  TEI      │ │Ingestion │ │Public API│ │
  │  └───────────┘ └──────────┘ └──────────┘ │
  └───────────────────────────────────────────┘
```

## Prerequisites

- [Hetzner account](https://www.hetzner.com) with API token
- SSH key pair (default: `~/.ssh/id_ed25519.pub`)
- Terraform >= 1.5.0
- For remote state: Hetzner Object Storage bucket + S3 access keys (create in console)

## 1. Authenticate with Hetzner

```bash
export HCLOUD_TOKEN="<your-hetzner-api-token>"
```

Do not store the token in any Terraform file. The `hcloud` provider reads it
from this environment variable.

## 2. Configure the Deployment

```bash
cd infra

cp terraform.tfvars.example terraform.tfvars
```

Edit `terraform.tfvars` to match your needs. At minimum, set the server type and
location:

```hcl
server_name         = "inherent-prod"
server_type         = "cpx22"      # 2 vCPU, 4 GB RAM
server_image        = "ubuntu-24.04"
server_location     = "fsn1"       # Falkenstein
server_backups      = false
ssh_public_key_path = "~/.ssh/id_ed25519.pub"
ssh_key_name        = "inherent-prod-key"
inherent_version    = "latest"
```

### Setting application secrets

Terraform does not require secrets to provision infrastructure. The default
`.env` written by cloud-init uses development-style placeholders. To inject
production secrets at apply time, add `env_file_content` to your
`terraform.tfvars`:

```hcl
env_file_content = <<-EOF
POSTGRES_PASSWORD=<strong-password>
INGESTION_API_KEY=<strong-api-key>
WEAVIATE_API_KEY=<weaviate-api-key>
...
EOF
```

`sensitive = true` only redacts the value from Terraform CLI plan/apply
terminal output. Secrets still land in Terraform state (restrict Object
Storage / state access) and in cloud-init `user_data` / instance metadata
(`169.254.169.254`) — processes and containers on the VM can read metadata.
Treat that as a known limitation. If omitted, the server starts with safe
defaults that you can update later via SSH.

The full list of environment variables is documented in
[.env.example](https://github.com/inherent-prime/inherent/blob/main/.env.example) at the repository root.

## 3. Initialize Terraform

Pick one init path:

| Path | When | State key / backend | Init |
|------|------|---------------------|------|
| **Prod / long-lived** | Stable prod VM | `backend.hcl` stable key (e.g. `inherent/prod/...`) | copy `backend.hcl.example` → `backend.hcl`, set `AWS_*` env, `terraform init -backend-config=backend.hcl` |
| **Laptop test** | Local experiments | `backend.hcl` key e.g. `inherent/local/laptop/...` | same Object Storage; see [local-vm-test.md](local-vm-test.md) |
| **CI e2e** | GHA Hetzner e2e | Object Storage `inherent/ci/<github.run_id>/terraform.tfstate` via workflow-generated `backend-ci.hcl` | see [infra/README.md § CI e2e](https://github.com/inherent-prime/inherent/blob/main/infra/README.md#ci-e2e) |

- `.terraform.lock.hcl` is the **provider lock** — committed to git.
- `*.tfstate` is **state** — never commit; remote state uses Hetzner Object Storage (S3-compatible).
- **Hard rule:** never point CI or laptop tests at the production state key.
- Operator creates the Object Storage bucket and S3 keys in the Hetzner console (out of band).

### Remote state (production)

```bash
cp backend.hcl.example backend.hcl
# Edit backend.hcl: bucket name, state key (e.g. inherent/prod/...), Object Storage endpoint

export AWS_ACCESS_KEY_ID="<hetzner-s3-access-key>"
export AWS_SECRET_ACCESS_KEY="<hetzner-s3-secret-key>"
export AWS_DEFAULT_REGION="eu-central"

terraform init -backend-config=backend.hcl
```

### Laptop test (Object Storage)

Step-by-step smoke path (remote state, bootstrap, destroy):
[local-vm-test.md](local-vm-test.md). Uses the same S3 backend with a dedicated
key such as `inherent/local/laptop/terraform.tfstate`.

### CI e2e (remote state)

GHA Hetzner e2e uses Object Storage under `inherent/ci/<run_id>/`, not local
state. Configure secrets/vars, recover workflow, and orphan cleanup in
[infra/README.md § CI e2e](https://github.com/inherent-prime/inherent/blob/main/infra/README.md#ci-e2e).

## 4. Review the Plan

```bash
terraform plan
```

Inspect the resources Terraform will create:

- `hcloud_ssh_key.default` — your SSH public key registered with Hetzner
- `hcloud_firewall.default` — firewall allowing SSH (22), Public API (18000),
  and ICMP
- `hcloud_firewall_attachment.default` — attaches the firewall to the server
- `hcloud_server.default` — the Hetzner VM with cloud-init user data

## 5. Provision

```bash
terraform apply
```

Confirm with `yes`. Terraform provisions the server, and cloud-init runs the
following automatically:

1. Installs Docker Engine and Docker Compose plugin
2. Creates `/opt/inherent`
3. Downloads `docker-compose.release.yml` from GitHub
4. Writes `.env` with your configuration
5. Starts all containers with `docker compose up -d`

## 6. Verify the Deployment

Wait for cloud-init to finish (typically 2–5 minutes). Then check the server:

```bash
ssh root@$(terraform output -raw server_ipv4)

docker compose -f /opt/inherent/docker-compose.release.yml ps
```

All services should show `Up` or `healthy`. Check the Public API:

```bash
curl -s http://localhost:18000/health
```

To reach the API from your local machine:

```bash
curl -s http://$(terraform output -raw server_ipv4):18000/health
```

## Updating the Application

SSH into the server and use Docker Compose directly:

```bash
ssh root@<server-ip>

cd /opt/inherent

# Pull the latest images
docker compose -f docker-compose.release.yml pull

# Restart with updated images
docker compose -f docker-compose.release.yml up -d
```

To change the deployed version:

```bash
export INHERENT_VERSION=0.4.0
docker compose -f docker-compose.release.yml pull
docker compose -f docker-compose.release.yml up -d
```

## Updating Infrastructure

Edit the Terraform files and re-apply:

```bash
cd infra
terraform apply
```

Terraform applies only the changed resources — the server name, type, firewall
rules, or location.

## Common Commands

| Command | Purpose |
| --- | --- |
| `terraform plan` | Preview infrastructure changes |
| `terraform apply` | Provision or update infrastructure |
| `terraform destroy` | Remove all infrastructure |
| `terraform output -raw server_ipv4` | Get the server's public IP |
| `terraform output -raw server_ipv6` | Get the server's public IPv6 |

## Clean Up

Remove all provisioned resources:

```bash
cd infra
terraform destroy
```

Confirm with `yes`. This deletes the server, firewall, SSH key, and all Docker
volumes on the VM. Data is permanently lost — back up anything important first.

## Architecture

Terraform manages four resources:

```
Terraform
  ├── hcloud_ssh_key          SSH public key registered with Hetzner
  ├── hcloud_server           Hetzner VM (with cloud-init user data)
  ├── hcloud_firewall         Firewall rules (SSH, API, ICMP)
  └── hcloud_firewall_attachment  Attaches firewall to server
```

The firewall allows inbound traffic on:

- Port **22** (SSH) — `ssh_allowed_ips` (default any source)
- Port **18000** (Inherent Public API) — `api_allowed_ips` (default any source)
- **ICMP** (ping) from any source

All other inbound traffic is blocked. This Hetzner firewall is the only
network barrier for Docker-published ports; restrict the CIDR vars in
production. Service-to-service traffic stays on the Compose network and does
not traverse the firewall.

## Not in Scope (Future Iterations)

- DNS / TLS / HTTPS — terminate TLS at a reverse proxy on the same VM or
  through a load balancer
- Persistent volumes — currently uses Docker named volumes on the VM's root
  disk. Add `hcloud_volume` and mount it into the Compose services for
  persistent data that survives server replacement
- Floating IP — attach a floating IP to decouple the address from the server
  lifecycle
- Multiple environments — add `dev/` and `staging/` workspace directories with
  their own `terraform.tfvars`
- Dedicated VM for Text Embeddings Inference — reduce noise on the shared VM
- Multi-node deployments — separate ingestion, retrieval, and database nodes

## Next Steps

- Browse [the docs index](../index.md) for the full documentation index.
- Open the Public API docs at `http://<server-ip>:18000/docs`.
- Temporal UI is **not** opened on the Hetzner firewall (only 22, 18000, ICMP).
  Reach it via SSH tunnel:

  ```bash
  ssh -L 18233:127.0.0.1:18233 root@<server-ip>
  # then browse http://127.0.0.1:18233
  ```

- Run the end-to-end local smoke test from the
  [root README](https://github.com/inherent-prime/inherent/blob/main/README.md#local-smoke-test) against your production server
  to verify the full upload-ingest-search path.
