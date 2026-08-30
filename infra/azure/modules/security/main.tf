# Issue #321 — Key Vault, workload identity, and the generated credentials every other
# module (#322 data, apps) hands off secrets through instead of a tfvar. See root
# variables.tf / README for the state-file caveat: these values still land in Terraform
# state, so the state backend (storage account) MUST be private + RBAC'd, never public.

data "azurerm_client_config" "current" {}

# Global-uniqueness suffix for the vault name (Key Vault names are a global DNS namespace).
resource "random_string" "kv_suffix" {
  length  = 4
  special = false
  upper   = false
}

resource "azurerm_user_assigned_identity" "workload" {
  name                = "${var.resource_prefix}-${var.environment}-workload-id"
  resource_group_name = var.resource_group_name
  location            = var.location
  tags                = var.tags
}

resource "azurerm_key_vault" "this" {
  name                = "${var.resource_prefix}-${var.environment}-kv-${random_string.kv_suffix.result}"
  resource_group_name = var.resource_group_name
  location            = var.location
  tenant_id           = data.azurerm_client_config.current.tenant_id
  sku_name            = "standard"

  # RBAC (not vault access policies) so Key Vault Secrets User/Officer role assignments
  # below are the single source of truth for who can read/write secrets.
  rbac_authorization_enabled = true
  purge_protection_enabled   = true
  soft_delete_retention_days = 90

  # NOTE: public_network_access_enabled = false disables the public endpoint outright — the
  # network_acls ip_rules below have NO effect in that state (Azure only honors ip_rules on
  # a public endpoint that exists, i.e. "Selected networks" mode). So when the deployer needs
  # a firewalled public path in (deployer_ip_ranges non-empty), the endpoint must stay
  # enabled and network_acls.default_action = "Deny" does the actual restricting instead;
  # private-endpoint traffic reaches the vault either way (PEs bypass network_acls entirely).
  public_network_access_enabled = !var.enable_private_endpoints || length(var.deployer_ip_ranges) > 0

  network_acls {
    default_action = var.enable_private_endpoints ? "Deny" : "Allow"
    bypass         = "AzureServices"
    # Deployer egress IPs — required when enable_private_endpoints = true and Terraform
    # runs outside the VNet, otherwise this module's own
    # azurerm_key_vault_secret.generated writes below 403. See variable description.
    ip_rules = var.deployer_ip_ranges
  }

  tags = var.tags
}

resource "azurerm_private_endpoint" "kv" {
  count = var.enable_private_endpoints ? 1 : 0

  name                = "${var.resource_prefix}-${var.environment}-kv-pe"
  resource_group_name = var.resource_group_name
  location            = var.location
  subnet_id           = var.subnet_id
  tags                = var.tags

  private_service_connection {
    name                           = "${var.resource_prefix}-${var.environment}-kv-psc"
    private_connection_resource_id = azurerm_key_vault.this.id
    subresource_names              = ["vault"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "vault"
    private_dns_zone_ids = [var.private_dns_zone_id]
  }
}

# Workload pods (public-api, ingestion, migrate) read secrets through the CSI Secrets
# Store driver using this identity's federated workload identity (wired in the aks/apps
# modules) — grant it read-only access to secret values.
resource "azurerm_role_assignment" "workload_secrets_user" {
  scope                = azurerm_key_vault.this.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.workload.principal_id
}

# RBAC-mode vaults grant the deployer nothing by default (unlike access-policy mode) —
# Terraform itself needs write access to create the secrets below. NOTE: Azure RBAC role
# assignments can take a minute to propagate; a fresh `apply` that fails the first secret
# write with 403 just needs a re-run, no `time` provider added to avoid widening the
# provider surface for a transient consistency wait.
resource "azurerm_role_assignment" "terraform_secrets_officer" {
  scope                = azurerm_key_vault.this.id
  role_definition_name = "Key Vault Secrets Officer"
  principal_id         = data.azurerm_client_config.current.object_id
}

# --- Generated credentials (issue #322 depends on these) ---------------------------------
# All Azure-issued/random — no secret is ever a tfvar (see root variables.tf). Character
# sets are restricted to avoid breaking connection-string URL encoding (postgres, mongodb+srv,
# rediss://) downstream in the data module.

resource "random_password" "postgres_admin" {
  length           = 32
  special          = true
  override_special = "-_."
}

resource "random_password" "weaviate_api_key" {
  length  = 40
  special = false
}

resource "random_password" "ingestion_api_key" {
  length  = 40
  special = false
}

resource "random_string" "minio_root_user" {
  length  = 16
  special = false
  upper   = false
}

resource "random_password" "minio_root_password" {
  length  = 32
  special = false
}

locals {
  secrets = {
    postgres-admin-password = random_password.postgres_admin.result
    weaviate-api-key        = random_password.weaviate_api_key.result
    ingestion-api-key       = random_password.ingestion_api_key.result
    minio-root-user         = random_string.minio_root_user.result
    minio-root-password     = random_password.minio_root_password.result
  }
}

resource "azurerm_key_vault_secret" "generated" {
  for_each = local.secrets

  name         = each.key
  value        = each.value
  key_vault_id = azurerm_key_vault.this.id

  depends_on = [azurerm_role_assignment.terraform_secrets_officer]
}
