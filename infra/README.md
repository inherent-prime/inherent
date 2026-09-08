# Inherent — Infrastructure

Terraform deployment targets, one subfolder per cloud provider (#355). Each
subfolder is a self-contained Terraform root with its own providers, remote
state, and README; nothing is shared across providers, so changes in one
cannot break another.

| Provider | Path | Shape | Guide |
|----------|------|-------|-------|
| **Hetzner** | [`hetzner/`](hetzner/) | Single VM + Docker Compose release stack. Cheap eval / small-team tier — no HA, no DR. | [docs/getting-started/production.md](../docs/getting-started/production.md) |
| **Azure** | [`azure/`](azure/) | AKS (3 zones) + managed data services, HA + DR, one-click deploy. Production tier. | [docs/deploy/azure.md](../docs/deploy/azure.md) |

Future providers (GCP, AWS, …) get their own subfolder here following the
same pattern: `versions.tf`/`providers.tf`/partial backend config at the
root, modules below it, examples as `*.example`, provider lock committed,
state never committed.

Cross-provider rules (each subfolder's README has the provider-specific
detail):

- `.terraform.lock.hcl` is the **provider lock** — committed to git,
  per root.
- `*.tfstate` is **state** — never committed; every root uses remote state
  with its own bucket/container and key. **Hard rule:** never point CI or a
  laptop test at a production state key.
- Secrets are never tfvars. They are generated in-stack or injected at
  apply time, and they DO land in Terraform state — the state backend must
  be private and access-controlled.
