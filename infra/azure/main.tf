# Module composition only — no resources here (besides the two resource groups every
# module needs a home in). Wiring follows the cross-module interface in
# .memory/azure-build-spec.md, cross-checked against each sibling module's actual
# variables.tf/outputs.tf where those modules already exist in this checkout.
#
# Composition order matters for two real dependencies (not just readability):
#   monitoring before aks   — aks.log_analytics_workspace_id needs monitoring.workspace_id
#   data       before *     — pg/redis/cosmos/storage outputs feed monitoring, dr, apps
# modules/apps is still unbuilt at the time this root was written (empty directory) — its
# block below is wired against the spec's documented interface ahead of that landing;
# `terraform validate` fails there until it exists, same as any module this root doesn't
# own (see README.md "Validation").

resource "azurerm_resource_group" "main" {
  name     = "${var.resource_prefix}-${var.environment}-rg"
  location = var.location
  tags     = var.tags
}

# DR resource group (modules/dr's own choice of RG per its variables.tf comment — Azure
# resources aren't region-locked to their RG, but a separate RG keeps the DR footprint
# visible and lets teardown order differ from the primary RG's).
resource "azurerm_resource_group" "dr" {
  count = var.enable_dr ? 1 : 0

  name     = "${var.resource_prefix}-${var.environment}-dr-rg"
  location = var.location_dr
  tags     = var.tags
}

# --- Issue #321: network + security foundation ---------------------------------------------

module "network" {
  source = "./modules/network"

  resource_group_name = azurerm_resource_group.main.name
  location            = var.location
  resource_prefix     = var.resource_prefix
  environment         = var.environment
  tags                = var.tags

  existing_vnet_id         = var.existing_vnet_id
  existing_subnet_ids      = var.existing_subnet_ids
  enable_private_endpoints = var.enable_private_endpoints
}

module "security" {
  source = "./modules/security"

  resource_group_name = azurerm_resource_group.main.name
  location            = var.location
  resource_prefix     = var.resource_prefix
  environment         = var.environment
  tags                = var.tags

  enable_private_endpoints = var.enable_private_endpoints
  subnet_id                = module.network.subnet_ids["data"]
  private_dns_zone_id      = try(module.network.private_dns_zone_ids["vault"], "")
}

# --- Issue #322: data layer ------------------------------------------------------------------

module "data" {
  source = "./modules/data"

  resource_group_name = azurerm_resource_group.main.name
  location            = var.location
  resource_prefix     = var.resource_prefix
  environment         = var.environment
  tags                = var.tags

  subnet_id            = module.network.subnet_ids["data"]
  private_dns_zone_ids = module.network.private_dns_zone_ids

  key_vault_id                      = module.security.key_vault_id
  postgres_admin_password           = module.security.postgres_admin_password
  postgres_admin_password_kv_secret = module.security.postgres_admin_password_kv_secret

  enable_ha                = var.enable_ha
  enable_dr                = var.enable_dr
  enable_private_endpoints = var.enable_private_endpoints

  pg_sku           = var.pg_sku
  pg_storage_mb    = var.pg_storage_mb
  cosmos_mongo_sku = var.cosmos_mongo_sku
  redis_sku        = var.redis_sku
  redis_family     = var.redis_family
  redis_capacity   = var.redis_capacity
}

# --- Observability (modules/monitoring, issue #325) — composed before aks: aks's
# log_analytics_workspace_id input needs monitoring's workspace_id output. ------------------

module "monitoring" {
  source = "./modules/monitoring"

  resource_group_name = azurerm_resource_group.main.name
  location            = var.location
  resource_prefix     = var.resource_prefix
  environment         = var.environment
  tags                = var.tags

  alert_email_address = var.alert_email_address != "" ? var.alert_email_address : var.letsencrypt_email

  pg_server_id   = module.data.pg_server_id
  redis_cache_id = module.data.redis_cache_id
  # App Gateway resource id: null (nginx ingress_profile default, or appgw_waf until
  # modules/apps defines where the azurerm_application_gateway resource itself is created —
  # see the appgw_id sequencing note in modules/aks/variables.tf and the TODO on
  # module "apps" below).
  appgw_id = null
}

# --- AKS (modules/aks, issue #323) ------------------------------------------------------------

module "aks" {
  source = "./modules/aks"

  resource_group_name = azurerm_resource_group.main.name
  location            = var.location
  resource_prefix     = var.resource_prefix
  environment         = var.environment
  tags                = var.tags

  subnet_id = module.network.subnet_ids["aks"]

  private_cluster_enabled = var.private_cluster_enabled
  authorized_ip_ranges    = var.authorized_ip_ranges
  enable_ha               = var.enable_ha

  aks_system_vm_size = var.aks_system_vm_size
  aks_user_vm_size   = var.aks_user_vm_size
  aks_user_min_count = var.aks_user_min_count
  aks_user_max_count = var.aks_user_max_count

  log_analytics_workspace_id = module.monitoring.workspace_id
  # Same appgw_id staging note as modules/monitoring above.
  appgw_id = null
}

# --- AI layer (modules/ai, issue #324) ---------------------------------------------------------

module "ai" {
  source = "./modules/ai"

  resource_group_name = azurerm_resource_group.main.name
  location            = var.location
  resource_prefix     = var.resource_prefix
  environment         = var.environment
  tags                = var.tags

  key_vault_id = module.security.key_vault_id

  openai_embedding_model = var.openai_embedding_model
  openai_embedding_dim   = var.openai_embedding_dim
  openai_sku             = var.openai_sku
  openai_capacity        = var.openai_capacity
}

# --- App workloads (modules/apps, issue #323) ------------------------------------------------
# Wired against modules/apps/variables.tf as built: the apps module consumes raw sensitive
# passthroughs from data/security/ai (single source of secret truth — see the "Secret
# generation ownership" note in modules/apps/secrets.tf) rather than Key Vault secret names.

locals {
  # One public hostname, from either input style: explicit api_hostname wins; else the
  # Azure DNS pair. envs/*.tfvars.example show both. Validation lives here (not in a
  # variable block, which cannot see other variables).
  api_hostname = var.api_hostname != "" ? var.api_hostname : "${var.dns_record}.${var.dns_zone_name}"
}

module "apps" {
  source = "./modules/apps"

  resource_group_name = azurerm_resource_group.main.name
  location            = var.location
  resource_prefix     = var.resource_prefix
  environment         = var.environment
  tags                = var.tags

  key_vault_id = module.security.key_vault_id

  # Data layer (modules/data) — hostnames + raw connection strings (sensitive).
  pg_fqdn                           = module.data.pg_fqdn
  pg_admin_user                     = module.data.pg_admin_user
  pg_password_kv_secret             = module.data.pg_password_kv_secret
  postgres_app_connection_string    = module.data.postgres_app_connection_string
  cosmos_connection_string          = module.data.cosmos_connection_string
  redis_connection_string           = module.data.redis_connection_string
  storage_account_connection_string = module.data.storage_account_primary_connection_string

  # Security layer (modules/security) — generated credentials (sensitive).
  weaviate_api_key            = module.security.weaviate_api_key
  ingestion_api_key           = module.security.ingestion_api_key
  minio_root_user             = module.security.minio_root_user
  minio_root_password         = module.security.minio_root_password
  workload_identity_client_id = module.security.workload_identity_client_id

  # AI layer (modules/ai, issue #324) — Azure OpenAI embeddings (#311 / PR #314 contract).
  openai_endpoint                  = module.ai.openai_endpoint
  openai_embedding_deployment_name = module.ai.embedding_deployment_name
  openai_key                       = module.ai.openai_key
  openai_embedding_dim             = module.ai.dim

  aks_pod_cidr = module.aks.pod_cidr

  embedding_profile = var.embedding_profile
  ingress_profile   = var.ingress_profile
  enable_ha         = var.enable_ha
  enable_dr         = var.enable_dr

  inherent_version = var.inherent_version
  api_replicas_min = var.api_replicas_min
  api_replicas_max = var.api_replicas_max
  worker_replicas  = var.worker_replicas
  weaviate_disk_gb = var.weaviate_disk_gb
  minio_disk_gb    = var.minio_disk_gb

  api_hostname      = local.api_hostname
  letsencrypt_email = var.letsencrypt_email
  appgw_subnet_id   = module.network.subnet_ids["appgw"]

  # Helm/kubernetes providers (providers.tf) already read module.aks via try() — this
  # depends_on makes the ordering explicit in the graph, not just the provider config.
  depends_on = [module.aks]
}

# --- DR (modules/dr, issue #325) — conditional on enable_dr -------------------------------------

module "dr" {
  count  = var.enable_dr ? 1 : 0
  source = "./modules/dr"

  resource_group_name = azurerm_resource_group.dr[0].name
  location_dr         = var.location_dr
  resource_prefix     = var.resource_prefix
  environment         = var.environment
  tags                = var.tags

  enable_dr      = var.enable_dr
  pg_geo_replica = var.pg_geo_replica

  pg_source_server_id      = module.data.pg_server_id
  storage_account_grs_name = module.data.storage_account_name
}
