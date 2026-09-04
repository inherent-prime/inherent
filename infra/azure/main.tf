# Module composition only — no resources here (besides the two resource groups every
# module needs a home in, and the guardrail preconditions below). Wiring follows the
# cross-module interface in .memory/azure-build-spec.md, cross-checked against each
# sibling module's actual variables.tf/outputs.tf.
#
# Composition order matters for two real dependencies (not just readability):
#   monitoring before aks   — aks.log_analytics_workspace_id needs monitoring.workspace_id
#   data       before *     — pg/redis/cosmos/storage outputs feed monitoring, dr, apps

resource "azurerm_resource_group" "main" {
  name     = "${var.resource_prefix}-${var.environment}-rg"
  location = var.location
  tags     = var.tags
}

# DR resource group: only when modules/dr will actually create something in it. enable_dr
# alone does NOT need this RG — enable_dr's real effects (GRS storage replication,
# geo-redundant PG backups) live in modules/data, in the PRIMARY resource group, not here.
# This RG exists only for the optional cross-region PG read replica (pg_geo_replica), the
# one resource modules/dr itself creates.
resource "azurerm_resource_group" "dr" {
  count = var.enable_dr && var.pg_geo_replica ? 1 : 0

  name     = "${var.resource_prefix}-${var.environment}-dr-rg"
  location = var.location_dr
  tags     = var.tags
}

# --- Guardrails: fail loudly at plan/apply time instead of deploying a broken stack --------
# terraform_data (built into Terraform core, no provider needed) has no attributes to
# compute, so it is always (re)planned and its lifecycle preconditions always evaluated —
# an anchor for cross-variable checks a `variable` block's own `validation` cannot express
# (validation blocks only see their own variable, not var.dns_zone_name from within
# var.api_hostname's block, etc).
resource "terraform_data" "guardrails" {
  lifecycle {
    precondition {
      condition     = var.api_hostname != "" || (var.dns_zone_name != "" && var.dns_record != "")
      error_message = "Set either api_hostname, or both dns_zone_name and dns_record — the ingress needs a hostname to serve and cert-manager needs one to request a certificate for."
    }

    precondition {
      condition     = var.ingress_profile != "nginx" || var.letsencrypt_email != ""
      error_message = "letsencrypt_email is required when ingress_profile = \"nginx\" — cert-manager's Let's Encrypt ACME registration needs a contact address."
    }

    precondition {
      condition     = var.alert_email_address != "" || var.letsencrypt_email != ""
      error_message = "Set alert_email_address (or letsencrypt_email as a fallback) — an empty receiver means monitoring alerts fire into nothing."
    }
  }
}

# --- Issue #321: network + security foundation ---------------------------------------------

module "network" {
  source = "./modules/network"

  resource_group_name = azurerm_resource_group.main.name
  location            = var.location
  resource_prefix     = var.resource_prefix
  environment         = var.environment
  tags                = var.tags

  vnet_cidr                = var.vnet_cidr
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
  deployer_ip_ranges       = var.deployer_ip_ranges
  # Key Vault's PE lives in the "pe" subnet, NOT "data" — "data" is delegated to PG Flexible
  # and Azure forbids private endpoints in a delegated subnet (see modules/network).
  subnet_id           = module.network.subnet_ids["pe"]
  private_dns_zone_id = try(module.network.private_dns_zone_ids["vault"], "")
}

# --- Issue #322: data layer ------------------------------------------------------------------

module "data" {
  source = "./modules/data"

  resource_group_name = azurerm_resource_group.main.name
  location            = var.location
  resource_prefix     = var.resource_prefix
  environment         = var.environment
  tags                = var.tags

  subnet_id            = module.network.subnet_ids["data"] # PG Flexible's delegated subnet
  pe_subnet_id         = module.network.subnet_ids["pe"]   # Cosmos/Redis/Blob PEs
  private_dns_zone_ids = module.network.private_dns_zone_ids

  key_vault_id                      = module.security.key_vault_id
  postgres_admin_password           = module.security.postgres_admin_password
  postgres_admin_password_kv_secret = module.security.postgres_admin_password_kv_secret

  enable_ha                = var.enable_ha
  enable_dr                = var.enable_dr
  enable_private_endpoints = var.enable_private_endpoints
  deployer_ip_ranges       = var.deployer_ip_ranges

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
  log_retention_days  = var.log_retention_days

  pg_server_id   = module.data.pg_server_id
  redis_cache_id = module.data.redis_cache_id
  # Always null: ingress_profile validation (variables.tf) rejects "appgw_waf" outright, so
  # the appgw_id-driven alert path (azurerm_monitor_metric_alert.appgw_unhealthy_hosts) is
  # unreachable in this checkout. The App Gateway/AGIC wiring that would populate this is
  # tracked as a follow-up under epic #320 — see variables.tf's ingress_profile validation.
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
  sku_tier                = var.aks_sku_tier
  pod_cidr                = var.pod_cidr

  aks_system_vm_size = var.aks_system_vm_size
  aks_user_vm_size   = var.aks_user_vm_size
  aks_user_min_count = var.aks_user_min_count
  aks_user_max_count = var.aks_user_max_count

  log_analytics_workspace_id = module.monitoring.workspace_id
  # Same appgw_id note as modules/monitoring above — always null, epic #320 follow-up.
  appgw_id = null

  # modules/network attaches a NAT gateway to this same subnet; aks's network_profile now
  # sets outbound_type = "userAssignedNATGateway" (see that module's comment), which requires
  # the NAT gateway association to exist before the cluster is created. subnet_id above only
  # creates an implicit dependency on the subnet itself, not on the separate
  # azurerm_subnet_nat_gateway_association resource in modules/network — this depends_on
  # closes that gap explicitly rather than relying on apply ordering to get lucky.
  depends_on = [module.network]
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
  # The identity's resource id (not the client id above) — parent_id for this module's
  # azurerm_federated_identity_credential.workload.
  workload_identity_id = module.security.workload_identity_id

  # AI layer (modules/ai, issue #324) — Azure OpenAI embeddings (#311 / PR #314 contract).
  openai_endpoint                  = module.ai.openai_endpoint
  openai_embedding_deployment_name = module.ai.embedding_deployment_name
  openai_key                       = module.ai.openai_key
  openai_embedding_dim             = module.ai.dim

  aks_pod_cidr        = module.aks.pod_cidr
  aks_service_cidr    = module.aks.service_cidr
  aks_oidc_issuer_url = module.aks.oidc_issuer_url

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

# DNS A record, only when dns_zone_name is set (the api_hostname-only path leaves DNS to the
# operator, out of band — see local.api_hostname above). Points at ingress-nginx's LB IP.
#
# count only tests var.dns_zone_name (a plain input, always known at plan time) —
# module.apps.ingress_nginx_lb_ip is NOT safe to use in count: it comes from a data source
# that depends_on helm_release.ingress_nginx, so on the apply that first creates that release
# its value is unknown until apply, and an unknown value in a count/for_each expression is a
# hard Terraform plan error, not just a runtime one. The lifecycle precondition below is where
# that "might not be ready yet" case belongs instead — preconditions ARE allowed to depend on
# apply-time values.
resource "azurerm_dns_a_record" "api" {
  count = var.dns_zone_name != "" ? 1 : 0

  name                = var.dns_record
  zone_name           = var.dns_zone_name
  resource_group_name = var.dns_zone_resource_group
  ttl                 = 300
  records             = [module.apps.ingress_nginx_lb_ip]
  tags                = var.tags

  lifecycle {
    precondition {
      # Best-effort, per modules/apps/outputs.tf's own comment on ingress_nginx_lb_ip: the
      # Azure LB assigns this IP asynchronously after helm_release.ingress_nginx completes,
      # so it can still read as null immediately post-apply even though it converges within
      # a minute or two. A clear, targeted failure here (re-run `terraform apply`) beats
      # either silently skipping the record or letting a `records = [null]` plan surface a
      # confusing Azure API/type error instead — and it doesn't block any other resource,
      # including the rest of this same apply.
      condition     = module.apps.ingress_nginx_lb_ip != null
      error_message = "ingress-nginx's LoadBalancer IP is not assigned yet (converges within a minute or two of ingress-nginx installing). Re-run `terraform apply` to create the api DNS A record once it has."
    }
  }
}

# --- DR (modules/dr, issue #325) — conditional on enable_dr -------------------------------------
# The module itself is instantiated whenever enable_dr = true (it also produces dr_summary,
# a documentation-facing output the runbook generation needs regardless of pg_geo_replica) —
# but azurerm_resource_group.dr above only exists when pg_geo_replica is ALSO true, since
# that's the only resource this module ever creates. Fall back to the primary RG when
# pg_geo_replica = false: the reference is unused in that case (modules/dr's own geo_replica
# resource is itself count-gated the same way), this just keeps the input a valid string.

module "dr" {
  count  = var.enable_dr ? 1 : 0
  source = "./modules/dr"

  resource_group_name = var.pg_geo_replica ? azurerm_resource_group.dr[0].name : azurerm_resource_group.main.name
  location_dr         = var.location_dr
  resource_prefix     = var.resource_prefix
  environment         = var.environment
  tags                = var.tags

  enable_dr      = var.enable_dr
  pg_geo_replica = var.pg_geo_replica

  pg_source_server_id      = module.data.pg_server_id
  storage_account_grs_name = module.data.storage_account_name
}
