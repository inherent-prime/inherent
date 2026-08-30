# Issue #322 — data layer: PG Flexible (app + Temporal databases), Cosmos DB for MongoDB
# vCore, Azure Cache for Redis, and the Blob storage account backing MinIO's nightly mirror
# and Weaviate's backup-azure module. See root variables.tf for the state-file caveat —
# every secret this module writes to Key Vault, and the sensitive outputs below, also land
# in Terraform state.

resource "random_string" "suffix" {
  length  = 4
  special = false
  upper   = false
}

locals {
  name_prefix = "${var.resource_prefix}-${var.environment}"
  db_names    = ["knowledge_base", "temporal", "temporal_visibility"]

  # Storage account names: 3-24 chars, lowercase letters+digits only, globally unique.
  storage_account_name = substr(
    lower(replace("${var.resource_prefix}${var.environment}st${random_string.suffix.result}", "/[^a-z0-9]/", "")),
    0, 24
  )
}

# --- PostgreSQL Flexible Server ------------------------------------------------------------
# version 15 to match the app's pinned `postgres:15` (docker-compose.release.yml). VNet
# integration via delegated_subnet_id is always on (not gated by enable_private_endpoints) —
# it is PG Flexible's normal deployment mode here, not an optional add-on private endpoint.

resource "azurerm_postgresql_flexible_server" "main" {
  name                = "${local.name_prefix}-pg-${random_string.suffix.result}"
  resource_group_name = var.resource_group_name
  location            = var.location

  version                = "15"
  administrator_login    = "pgadmin"
  administrator_password = var.postgres_admin_password

  sku_name   = var.pg_sku
  storage_mb = var.pg_storage_mb
  zone       = "1"

  # HA standby always lands in a different zone; geo-redundant backup is separate from HA
  # and only protects against a region-wide loss (paired with modules/dr's restore path).
  dynamic "high_availability" {
    for_each = var.enable_ha ? [1] : []
    content {
      mode                      = "ZoneRedundant"
      standby_availability_zone = "2"
    }
  }

  geo_redundant_backup_enabled = var.enable_dr
  # 35 days (max) when DR is on so a geo-restore has a wide RPO window; 7 otherwise (cost).
  backup_retention_days = var.enable_dr ? 35 : 7

  delegated_subnet_id = var.subnet_id
  private_dns_zone_id = var.private_dns_zone_ids["postgres"]

  tags = var.tags

  lifecycle {
    # Azure disallows changing zone/HA in place in some SKU/region combinations; force a
    # replace rather than a confusing mid-apply error if an operator edits this later.
    create_before_destroy = false
  }
}

resource "azurerm_postgresql_flexible_server_database" "this" {
  for_each = toset(local.db_names)

  name      = each.value
  server_id = azurerm_postgresql_flexible_server.main.id
  charset   = "UTF8"
  collation = "en_US.utf8"
}

# --- Cosmos DB for MongoDB vCore -----------------------------------------------------------
# azurerm_mongo_cluster (not the older azurerm_cosmosdb_mongo_database RU-based resource) —
# vCore is the MongoDB-API-compatible offering the app's `motor`/`pymongo`-style
# mongodb+srv:// client needs.

resource "random_password" "cosmos_admin" {
  # Not in modules/security's generated-secret list (spec enumerates postgres/weaviate/
  # ingestion/minio/app-seed only) — generated here since only this module knows the
  # cluster exists. Written to Key Vault below like every other data-layer secret.
  length           = 32
  special          = true
  override_special = "-_."
}

resource "azurerm_mongo_cluster" "main" {
  name                = "${local.name_prefix}-mongo-${random_string.suffix.result}"
  resource_group_name = var.resource_group_name
  location            = var.location

  administrator_username = "mongoadmin"
  administrator_password = random_password.cosmos_admin.result

  # "M30" etc — same shape as var.cosmos_mongo_sku.
  compute_tier       = var.cosmos_mongo_sku
  storage_size_in_gb = 128
  shard_count        = 1
  version            = "7.0"

  # Values per azurerm_mongo_cluster docs: Disabled | SameZone | ZoneRedundant.
  high_availability_mode = var.enable_ha ? "ZoneRedundant" : "Disabled"
  public_network_access  = var.enable_private_endpoints ? "Disabled" : "Enabled"

  tags = var.tags
}

resource "azurerm_private_endpoint" "cosmos" {
  count = var.enable_private_endpoints ? 1 : 0

  name                = "${local.name_prefix}-mongo-pe"
  resource_group_name = var.resource_group_name
  location            = var.location
  subnet_id           = var.subnet_id
  tags                = var.tags

  private_service_connection {
    name                           = "${local.name_prefix}-mongo-psc"
    private_connection_resource_id = azurerm_mongo_cluster.main.id
    subresource_names              = ["MongoCluster"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "cosmos"
    private_dns_zone_ids = [var.private_dns_zone_ids["cosmos"]]
  }
}

# --- Azure Cache for Redis ------------------------------------------------------------------
# maxmemory_policy MUST be noeviction: Redis Streams (the ingestion MQ backend, MQ_BACKEND=redis)
# are keys, not a bounded cache — allkeys-lru silently drops undelivered upload events. See
# docs/deploy/production.md. Confirmed against the azurerm_redis_cache schema that
# redis_configuration.maxmemory_policy is a plain optional string with no tier restriction
# in the provider itself; Standard C1 (the spec default) is kept rather than forced to Premium.

resource "azurerm_redis_cache" "main" {
  name                = "${local.name_prefix}-redis-${random_string.suffix.result}"
  location            = var.location
  resource_group_name = var.resource_group_name

  capacity = var.redis_capacity
  family   = var.redis_family
  sku_name = var.redis_sku

  minimum_tls_version           = "1.2"
  non_ssl_port_enabled          = false
  public_network_access_enabled = !var.enable_private_endpoints

  redis_configuration {
    maxmemory_policy = "noeviction"
  }

  tags = var.tags
}

resource "azurerm_private_endpoint" "redis" {
  count = var.enable_private_endpoints ? 1 : 0

  name                = "${local.name_prefix}-redis-pe"
  resource_group_name = var.resource_group_name
  location            = var.location
  subnet_id           = var.subnet_id
  tags                = var.tags

  private_service_connection {
    name                           = "${local.name_prefix}-redis-psc"
    private_connection_resource_id = azurerm_redis_cache.main.id
    subresource_names              = ["redisCache"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "redis"
    private_dns_zone_ids = [var.private_dns_zone_ids["redis"]]
  }
}

# --- Storage account (MinIO mirror target + Weaviate backups) -------------------------------

resource "azurerm_storage_account" "main" {
  name                = local.storage_account_name
  resource_group_name = var.resource_group_name
  location            = var.location

  account_tier = "Standard"
  # GRS when DR is on (cross-region replica for the nightly mc-mirror + weaviate backups);
  # ZRS otherwise (zone-redundant, in-region only — cheaper, no DR).
  account_replication_type = var.enable_dr ? "GRS" : "ZRS"

  min_tls_version                 = "TLS1_2"
  https_traffic_only_enabled      = true
  public_network_access_enabled   = !var.enable_private_endpoints
  allow_nested_items_to_be_public = false

  blob_properties {
    versioning_enabled = true
  }

  tags = var.tags
}

resource "azurerm_storage_container" "this" {
  for_each = toset(["weaviate-backups", "minio-mirror", "documents-reserve"])

  name                  = each.value
  storage_account_id    = azurerm_storage_account.main.id
  container_access_type = "private"
}

resource "azurerm_private_endpoint" "blob" {
  count = var.enable_private_endpoints ? 1 : 0

  name                = "${local.name_prefix}-blob-pe"
  resource_group_name = var.resource_group_name
  location            = var.location
  subnet_id           = var.subnet_id
  tags                = var.tags

  private_service_connection {
    name                           = "${local.name_prefix}-blob-psc"
    private_connection_resource_id = azurerm_storage_account.main.id
    subresource_names              = ["blob"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "blob"
    private_dns_zone_ids = [var.private_dns_zone_ids["blob"]]
  }
}

# --- Key Vault secrets: connection strings/URLs ---------------------------------------------
# Only URLs/connection strings live here (raw component secrets — cosmos admin password —
# are written too, for the setup Jobs that need discrete fields rather than a URI).

resource "azurerm_key_vault_secret" "postgres_app_url" {
  name  = "postgres-app-url"
  value = "postgresql://${azurerm_postgresql_flexible_server.main.administrator_login}:${urlencode(var.postgres_admin_password)}@${azurerm_postgresql_flexible_server.main.fqdn}:5432/knowledge_base?sslmode=require"

  key_vault_id = var.key_vault_id
}

resource "azurerm_key_vault_secret" "cosmos_admin_password" {
  name  = "cosmos-mongo-admin-password"
  value = random_password.cosmos_admin.result

  key_vault_id = var.key_vault_id
}

resource "azurerm_key_vault_secret" "cosmos_mongo_uri" {
  name = "cosmos-mongo-uri"
  # azurerm_mongo_cluster has no standalone hostname attribute — connection_strings[0].value
  # is Azure's own ready-to-use mongodb+srv:// URI, credentials already substituted since
  # administrator_username/password were set on the cluster above.
  value = azurerm_mongo_cluster.main.connection_strings[0].value

  key_vault_id = var.key_vault_id
}

resource "azurerm_key_vault_secret" "redis_url" {
  name = "redis-url"
  # rediss:// (TLS) on 6380 — Azure Cache Standard tier's only listener once non_ssl_port_enabled=false.
  value = "rediss://:${urlencode(azurerm_redis_cache.main.primary_access_key)}@${azurerm_redis_cache.main.hostname}:6380/0"

  key_vault_id = var.key_vault_id
}
