# Azure OpenAI module — issue #324. Depends on the epic's #311/PR #314
# `openai_compatible` embedding provider landing app-side (see
# .memory/azure-build-spec.md "Ground truth"); this module is infra-only and
# has no runtime dependency on that PR, but the deployment as a whole is not
# usable until it merges — flagged in docs/deploy/azure.md limitations.

# custom_subdomain_name must be a globally-unique DNS label across all Azure
# OpenAI customers (it becomes <subdomain>.openai.azure.com) and is required
# for API access (Azure rejects data-plane calls to accounts without one) —
# append a short random suffix so re-running in a fresh subscription/region
# doesn't collide with another deployment using the same resource_prefix.
resource "random_string" "openai_subdomain_suffix" {
  length  = 6
  special = false
  upper   = false
}

locals {
  openai_subdomain = "${var.resource_prefix}-${var.environment}-oai-${random_string.openai_subdomain_suffix.result}"
}

resource "azurerm_cognitive_account" "openai" {
  name                = "${var.resource_prefix}-${var.environment}-openai"
  location            = var.location
  resource_group_name = var.resource_group_name
  kind                = "OpenAI"
  sku_name            = var.openai_sku
  tags                = var.tags

  # Required for API access — Azure OpenAI's data-plane rejects requests
  # against accounts without a custom subdomain.
  custom_subdomain_name = local.openai_subdomain

  public_network_access_enabled = var.public_network_access_enabled
}

# Single embedding deployment. Model/version/dim are pinned together (see
# variables.tf) since Weaviate's vector index has a fixed dimension per class.
resource "azurerm_cognitive_deployment" "embedding" {
  name                 = var.openai_embedding_model
  cognitive_account_id = azurerm_cognitive_account.openai.id

  model {
    format = "OpenAI"
    name   = var.openai_embedding_model
  }

  sku {
    name     = "Standard"
    capacity = var.openai_capacity
  }
}

# Primary key into Key Vault — never passed as a plain tfvar (see root
# variables.tf "Secrets: NONE passed as tfvars" convention); modules/apps
# reads this secret to materialize the EMBEDDING_API_KEY kubernetes Secret.
resource "azurerm_key_vault_secret" "openai_key" {
  name         = "${var.resource_prefix}-${var.environment}-openai-key"
  value        = azurerm_cognitive_account.openai.primary_access_key
  key_vault_id = var.key_vault_id
}
