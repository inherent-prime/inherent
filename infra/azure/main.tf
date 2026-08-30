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

# --- App workloads (modules/apps — not yet built in this checkout; empty modules/apps/
# directory, so terraform validate fails on every argument below until it lands. Wired
# against the spec's documented interface + the real outputs of every module it consumes. --

module "apps" {
  source = "./modules/apps"

  resource_group_name = azurerm_resource_group.main.name
  location            = var.location
  resource_prefix     = var.resource_prefix
  environment         = var.environment
  tags                = var.tags

  inherent_version  = var.inherent_version
  ingress_profile   = var.ingress_profile
  appgw_subnet_id   = module.network.subnet_ids["appgw"]
  dns_zone_name     = var.dns_zone_name
  dns_record        = var.dns_record
  api_hostname      = var.api_hostname
  letsencrypt_email = var.letsencrypt_email

  api_replicas_min = var.api_replicas_min
  api_replicas_max = var.api_replicas_max
  worker_replicas  = var.worker_replicas
  weaviate_disk_gb = var.weaviate_disk_gb
  minio_disk_gb    = var.minio_disk_gb
  storage_profile  = var.storage_profile

  # Cluster wiring — aks module's real output names.
  aks_cluster_name    = module.aks.cluster_name
  aks_oidc_issuer_url = module.aks.oidc_issuer_url

  key_vault_uri                 = module.security.key_vault_uri
  workload_identity_client_id   = module.security.workload_identity_client_id
  weaviate_api_key_kv_secret    = module.security.weaviate_api_key_kv_secret
  ingestion_api_key_kv_secret   = module.security.ingestion_api_key_kv_secret
  minio_root_user_kv_secret     = module.security.minio_root_user_kv_secret
  minio_root_password_kv_secret = module.security.minio_root_password_kv_secret
  app_api_key_seed_kv_secret    = module.security.app_api_key_seed_kv_secret

  postgres_app_url_kv_secret         = module.data.postgres_app_url_kv_secret
  cosmos_connection_string_kv_secret = module.data.cosmos_connection_string_kv_secret
  redis_url_kv_secret                = module.data.redis_url_kv_secret
  db_names                           = module.data.db_names
  storage_account_name               = module.data.storage_account_name
  backup_container_names             = module.data.backup_container_names

  openai_endpoint           = module.ai.openai_endpoint
  embedding_deployment_name = module.ai.embedding_deployment_name
  embedding_key_kv_secret   = module.ai.key_kv_secret
  embedding_dim             = module.ai.dim

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
