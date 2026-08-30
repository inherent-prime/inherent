# Azure OpenAI module — issue #324. Ground truth (azure-build-spec.md):
# embeddings run through the app's `openai_compatible` provider (#311/PR #314),
# which POSTs to "<openai_endpoint>/v1/embeddings" with a Bearer key. The
# `openai_endpoint` output here is the base URL *without* the /v1/embeddings
# suffix — the app appends it, don't double it up.

variable "resource_prefix" {
  description = "Naming prefix for all resources (root var: resource_prefix)."
  type        = string
}

variable "environment" {
  description = "Deployment environment tag (root var: environment)."
  type        = string
  default     = "prod"
}

variable "location" {
  description = "Azure region for the Cognitive Services account (root var: location). Azure OpenAI is region-limited — pick a region with OpenAI + the target model available (see docs/deploy/azure.md prerequisites)."
  type        = string
}

variable "resource_group_name" {
  description = "Resource group the Cognitive Services account is created in."
  type        = string
}

variable "tags" {
  description = "Common resource tags (root var: tags)."
  type        = map(string)
  default     = {}
}

variable "key_vault_id" {
  description = "Key Vault id (modules/security output: key_vault_id) that the OpenAI primary key is written into as a secret."
  type        = string
}

variable "openai_embedding_model" {
  description = <<-EOT
    Root var: openai_embedding_model. Azure OpenAI base model name to deploy
    for embeddings. Default text-embedding-3-small matches
    openai_embedding_dim=1536 (EMBEDDING_DIM in the helm chart's env contract)
    — changing one without the other breaks Weaviate's fixed vector dimension.
  EOT
  type        = string
  default     = "text-embedding-3-small"
}

variable "openai_embedding_dim" {
  description = "Root var: openai_embedding_dim. Vector dimension of openai_embedding_model — passed through as this module's `dim` output for the helm chart's EMBEDDING_DIM and for Weaviate's class schema. Must match the model (see openai_embedding_model comment)."
  type        = number
  default     = 1536
}

variable "openai_sku" {
  description = "Root var: openai_sku. Cognitive Services account SKU. S0 is the only pay-as-you-go SKU for Azure OpenAI."
  type        = string
  default     = "S0"
}

variable "openai_capacity" {
  description = "Root var: openai_capacity. Deployment throughput in units of 1K TPM (tokens/minute) — e.g. 50 = 50K TPM. Raise this if embedding throughput becomes the bottleneck under load (see docs/deploy/azure.md scale ceilings table); subject to the subscription's regional Azure OpenAI quota."
  type        = number
  default     = 50
}

variable "public_network_access_enabled" {
  description = <<-EOT
    Controls public network access to the Cognitive Services account. true
    (default) is required unless private endpoints are wired up for this
    resource — AKS pods reach it over the public endpoint by default in this
    build. Set false only once a private endpoint + private DNS zone for
    'privatelink.openai.azure.com' exist (not provisioned by this module —
    see modules/network's private_dns_zone_ids and coordinate with the
    network module owner if enterprise-VNet-private OpenAI access is needed).
  EOT
  type        = bool
  default     = true
}
