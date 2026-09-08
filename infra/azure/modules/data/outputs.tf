# Cross-module interface (azure-build-spec.md): secret NAMES + hostnames only; raw values
# only via the explicitly `sensitive = true` outputs at the bottom, for apps-module wiring
# that bypasses the CSI Secrets Store driver.

output "pg_fqdn" {
  description = "PG Flexible Server FQDN."
  value       = azurerm_postgresql_flexible_server.main.fqdn
}

output "pg_server_id" {
  description = "PG Flexible Server resource ID (for modules/dr's optional geo-replica, source_server_id)."
  value       = azurerm_postgresql_flexible_server.main.id
}

output "pg_admin_user" {
  description = "PG Flexible Server administrator login."
  value       = azurerm_postgresql_flexible_server.main.administrator_login
}

output "pg_password_kv_secret" {
  description = "Key Vault secret name holding the PG admin password (re-exposed from modules/security)."
  value       = var.postgres_admin_password_kv_secret
}

output "postgres_app_url_kv_secret" {
  description = "Key Vault secret name holding the full postgresql:// URL for the knowledge_base database (DATABASE_URL)."
  value       = azurerm_key_vault_secret.postgres_app_url.name
}

output "db_names" {
  description = "Databases created on the PG Flexible Server: app (knowledge_base) + Temporal's two."
  value       = local.db_names
}

output "cosmos_cluster_name" {
  description = "Cosmos DB for MongoDB vCore cluster name."
  value       = azurerm_mongo_cluster.main.name
}

output "cosmos_connection_string_kv_secret" {
  description = "Key Vault secret name holding the mongodb+srv:// connection URI."
  value       = azurerm_key_vault_secret.cosmos_mongo_uri.name
}

output "redis_hostname" {
  description = "Azure Cache for Redis hostname."
  value       = azurerm_redis_cache.main.hostname
}

output "redis_cache_id" {
  description = "Azure Cache for Redis resource ID (modules/monitoring attaches its memory alert to this)."
  value       = azurerm_redis_cache.main.id
}

output "redis_url_kv_secret" {
  description = "Key Vault secret name holding the rediss:// URL (with access key)."
  value       = azurerm_key_vault_secret.redis_url.name
}

output "storage_account_name" {
  description = "Storage account backing MinIO's nightly mirror and Weaviate backups."
  value       = azurerm_storage_account.main.name
}

output "backup_container_names" {
  description = "Blob containers: weaviate-backups, minio-mirror, documents-reserve."
  value       = [for c in azurerm_storage_container.this : c.name]
}

# --- Sensitive passthroughs, for apps-module wiring that needs the raw value directly -------

output "postgres_app_connection_string" {
  description = "Full postgresql:// URL for the knowledge_base database."
  value       = azurerm_key_vault_secret.postgres_app_url.value
  sensitive   = true
}

output "cosmos_connection_string" {
  description = "Full mongodb+srv:// connection URI."
  value       = azurerm_key_vault_secret.cosmos_mongo_uri.value
  sensitive   = true
}

output "redis_connection_string" {
  description = "Full rediss:// URL (with access key)."
  value       = azurerm_key_vault_secret.redis_url.value
  sensitive   = true
}

output "storage_account_primary_connection_string" {
  description = "Storage account primary connection string, for the mc-mirror CronJob / backup-azure module env (AZURE_STORAGE_CONNECTION_STRING)."
  value       = azurerm_storage_account.main.primary_connection_string
  sensitive   = true
}
