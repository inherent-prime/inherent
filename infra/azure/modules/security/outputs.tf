output "key_vault_id" {
  description = "Key Vault resource ID."
  value       = azurerm_key_vault.this.id
}

output "key_vault_uri" {
  description = "Key Vault DNS URI (https://<name>.vault.azure.net/)."
  value       = azurerm_key_vault.this.vault_uri
}

output "workload_identity_id" {
  description = "User-assigned identity resource ID (for AKS kubelet/workload identity federation)."
  value       = azurerm_user_assigned_identity.workload.id
}

output "workload_identity_client_id" {
  description = "Client ID pods use in their federated-workload-identity service account annotation."
  value       = azurerm_user_assigned_identity.workload.client_id
}

output "workload_identity_principal_id" {
  description = "Principal (object) ID of the workload identity, for additional role assignments (e.g. Storage, Cosmos data-plane RBAC in modules/data)."
  value       = azurerm_user_assigned_identity.workload.principal_id
}

# Secret NAMES only — never raw values — for modules that just need to point a CSI
# SecretProviderClass or `az keyvault secret show` at the right entry.
output "postgres_admin_password_kv_secret" {
  description = "Key Vault secret name holding the PG Flexible Server admin password."
  value       = azurerm_key_vault_secret.generated["postgres-admin-password"].name
}

output "weaviate_api_key_kv_secret" {
  description = "Key Vault secret name holding the Weaviate API key."
  value       = azurerm_key_vault_secret.generated["weaviate-api-key"].name
}

output "ingestion_api_key_kv_secret" {
  description = "Key Vault secret name holding the ingestion API's shared static secret."
  value       = azurerm_key_vault_secret.generated["ingestion-api-key"].name
}

output "minio_root_user_kv_secret" {
  description = "Key Vault secret name holding the MinIO root username."
  value       = azurerm_key_vault_secret.generated["minio-root-user"].name
}

output "minio_root_password_kv_secret" {
  description = "Key Vault secret name holding the MinIO root password."
  value       = azurerm_key_vault_secret.generated["minio-root-password"].name
}

# Sensitive passthroughs — only for the modules (#322 data) that must set the raw value on
# an Azure resource at create time (e.g. administrator_password) rather than read it from KV.
output "postgres_admin_password" {
  description = "Raw PG Flexible Server admin password. Consumed only by modules/data."
  value       = random_password.postgres_admin.result
  sensitive   = true
}

output "weaviate_api_key" {
  description = "Raw Weaviate API key. Consumed by modules/apps (Helm values -> Kubernetes Secret)."
  value       = random_password.weaviate_api_key.result
  sensitive   = true
}

output "ingestion_api_key" {
  description = "Raw ingestion API shared secret. Consumed by modules/apps."
  value       = random_password.ingestion_api_key.result
  sensitive   = true
}

output "minio_root_user" {
  description = "Raw MinIO root username. Consumed by modules/apps."
  value       = random_string.minio_root_user.result
  sensitive   = true
}

output "minio_root_password" {
  description = "Raw MinIO root password. Consumed by modules/apps."
  value       = random_password.minio_root_password.result
  sensitive   = true
}
