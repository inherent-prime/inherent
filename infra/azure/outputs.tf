output "resource_group_name" {
  description = "Primary resource group."
  value       = azurerm_resource_group.main.name
}

output "aks_name" {
  description = "AKS cluster name."
  value       = module.aks.cluster_name
}

output "kubeconfig_command" {
  description = "Fetch cluster credentials with the Azure CLI."
  value       = "az aks get-credentials --resource-group ${azurerm_resource_group.main.name} --name ${module.aks.cluster_name}"
}

output "kv_uri" {
  description = "Key Vault DNS URI."
  value       = module.security.key_vault_uri
}

output "pg_fqdn" {
  description = "PG Flexible Server FQDN."
  value       = module.data.pg_fqdn
}

output "cosmos_cluster_name" {
  description = "Cosmos DB for MongoDB vCore cluster name."
  value       = module.data.cosmos_cluster_name
}

output "redis_hostname" {
  description = "Azure Cache for Redis hostname."
  value       = module.data.redis_hostname
}

output "storage_account_name" {
  description = "Storage account backing MinIO's nightly mirror and Weaviate backups."
  value       = module.data.storage_account_name
}

output "api_fqdn" {
  description = "Public API ingress FQDN/IP (from modules/apps, ingress annotation per the spec)."
  value       = module.apps.api_fqdn
}

output "openai_endpoint" {
  description = "Azure OpenAI resource endpoint (embedding_profile = azure_openai)."
  value       = try(module.ai.openai_endpoint, null)
}

output "dr_enabled" {
  description = "Whether modules/dr was instantiated this apply."
  value       = var.enable_dr
}
