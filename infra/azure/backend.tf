# Empty partial backend config — bucket/container/key supplied via
# `terraform init -backend-config=backend.hcl` (copy backend.hcl.example) so the same
# checked-in root serves prod, a dev sandbox, and CI without editing this file.
#
# State-file caveat (mirrors ../backend.hcl.example's discipline for Hetzner): every secret
# this root writes to Key Vault, plus the sensitive module outputs, land in this state.
# The backend storage account MUST have public network access disabled and use RBAC
# (Storage Blob Data Contributor scoped to the one container), never a shared access key
# handed out broadly — treat the backend as security-critical infrastructure.
terraform {
  backend "azurerm" {}
}
