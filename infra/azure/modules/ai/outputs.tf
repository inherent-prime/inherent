# Outputs per .memory/azure-build-spec.md cross-module interface:
#   ai: openai_endpoint (…openai.azure.com), embedding_deployment_name,
#       key_kv_secret, dim

output "openai_endpoint" {
  description = <<-EOT
    App-facing base URL: "https://<subdomain>.openai.azure.com/openai".
    EMBEDDING_SERVICE_URL in the helm chart's env contract is set to this
    value verbatim — the app's openai_compatible provider (#311/PR #314)
    appends "/v1/embeddings" itself, so this output must NOT include that
    suffix.
  EOT
  value       = "https://${azurerm_cognitive_account.openai.custom_subdomain_name}.openai.azure.com/openai"
}

output "embedding_deployment_name" {
  description = "Azure OpenAI deployment name — maps to EMBEDDING_MODEL_ID in the helm chart's env contract."
  value       = azurerm_cognitive_deployment.embedding.name
}

output "key_kv_secret" {
  description = "Key Vault secret name holding the Cognitive Services primary key — modules/apps reads this (by name) to materialize EMBEDDING_API_KEY."
  value       = azurerm_key_vault_secret.openai_key.name
}

output "dim" {
  description = "Embedding vector dimension passthrough — maps to EMBEDDING_DIM in the helm chart's env contract and to Weaviate's class vector dimension."
  value       = var.openai_embedding_dim
}

output "cognitive_account_id" {
  description = "Cognitive Services account resource id (for role assignments / diagnostic settings owned by other modules)."
  value       = azurerm_cognitive_account.openai.id
}

# Sensitive passthrough — mirrors the pattern modules/data and
# modules/security use (KV secret NAME above for CSI/az-cli use, raw value
# here for the one module, modules/apps, that must set it directly on a
# Kubernetes Secret).
output "openai_key" {
  description = "Raw Cognitive Services primary key. Consumed only by modules/apps (materializes EMBEDDING_API_KEY)."
  value       = azurerm_cognitive_account.openai.primary_access_key
  sensitive   = true
}
