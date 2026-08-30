# Apps module — issue #323 (compute/ingress) + issue #324 (AI wiring). Owns
# the namespace, the kubernetes_secret resources that materialize Key
# Vault-sourced values into the cluster, the chart's helm_release, and
# ingress (nginx+cert-manager, or AppGW WAF_v2 + AGIC).
#
# kubernetes/helm providers are wired in root providers.tf from modules/aks's
# kube_config_* outputs — this module's kubernetes_*/helm_release resources
# depend on aks implicitly through that provider configuration, so no
# explicit aks kube_config variable is needed here.

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
  description = "Azure region (root var: location) — used only by the appgw_waf path's Application Gateway + public IP."
  type        = string
}

variable "resource_group_name" {
  description = "Resource group for the appgw_waf path's Application Gateway + public IP."
  type        = string
}

variable "tags" {
  description = "Common resource tags (root var: tags)."
  type        = map(string)
  default     = {}
}

variable "namespace" {
  description = "Kubernetes namespace for the whole chart. Fixed at \"inherent\" per issue #323; exposed as a var only so modules/monitoring's app_namespace / modules/security's federated credential subject can be kept in sync if ever changed."
  type        = string
  default     = "inherent"
}

variable "key_vault_id" {
  description = "Key Vault id (modules/security output) — used to read the pg/mongo/redis/openai secrets other modules wrote, and to write the secrets this module generates itself (weaviate/ingestion API keys, MinIO root credentials)."
  type        = string
}

# --- data module cross-module inputs (pg_fqdn, pg_admin_user, etc. per
# azure-build-spec.md "data:" output list). Wherever modules/data or
# modules/security already exposes a SENSITIVE passthrough of a raw value
# (e.g. `postgres_app_connection_string`, `weaviate_api_key`) this module
# takes that value directly instead of re-deriving it or re-reading it from
# Key Vault — avoids duplicate random_password generation and duplicate
# connection-string assembly logic living in two modules at once. The one
# exception is the PG admin password: modules/security's raw passthrough for
# it is documented "Consumed only by modules/data" (temporal needs it
# separately as discrete POSTGRES_USER/POSTGRES_PWD, not embedded in a DSN),
# so this module reads that one value via a Key Vault Secret data source
# instead — see main.tf.
variable "pg_fqdn" {
  description = "PostgreSQL Flexible Server FQDN (modules/data output: pg_fqdn)."
  type        = string
}

variable "pg_admin_user" {
  description = "PostgreSQL Flexible Server admin username (modules/data output: pg_admin_user)."
  type        = string
}

variable "pg_password_kv_secret" {
  description = "Key Vault secret NAME holding the PG admin password (modules/data output: pg_password_kv_secret, re-exposed from modules/security). Read via a Key Vault Secret data source (main.tf) for temporal's discrete POSTGRES_PWD — see note above on why this is the one exception to the sensitive-passthrough pattern."
  type        = string
}

variable "postgres_app_connection_string" {
  description = "Full postgresql:// URL for the app's knowledge_base database (modules/data output: postgres_app_connection_string, sensitive). Used directly as DATABASE_URL — modules/data already assembles and URL-encodes this, so this module does not reconstruct it."
  type        = string
  sensitive   = true
}

variable "cosmos_connection_string" {
  description = "Full mongodb+srv:// connection URI (modules/data output: cosmos_connection_string, sensitive). Used directly as MONGODB_URI."
  type        = string
  sensitive   = true
}

variable "redis_connection_string" {
  description = "Full rediss://:<key>@<host>:6380/0 URL (modules/data output: redis_connection_string, sensitive). Used directly as REDIS_URL / MQ_REDIS_URL."
  type        = string
  sensitive   = true
}

variable "storage_account_connection_string" {
  description = "Storage account connection string (modules/data output: storage_account_primary_connection_string, sensitive) — materialized as AZURE_STORAGE_CONNECTION_STRING for weaviate's backup-azure module and the MinIO mirror CronJob. Required when enable_dr = true."
  type        = string
  sensitive   = true
  default     = ""
}

variable "weaviate_backup_container" {
  description = "Blob container name for weaviate's backup-azure module (modules/data output: backup_container_names includes \"weaviate-backups\")."
  type        = string
  default     = "weaviate-backups"
}

variable "minio_mirror_container" {
  description = "Blob container name the nightly MinIO mirror writes to (modules/data output: backup_container_names includes \"minio-mirror\")."
  type        = string
  default     = "minio-mirror"
}

# --- security module cross-module inputs (generated app secrets) --------
variable "weaviate_api_key" {
  description = "Raw Weaviate API key (modules/security output: weaviate_api_key, sensitive — security's own comment marks this \"Consumed by modules/apps\")."
  type        = string
  sensitive   = true
}

variable "ingestion_api_key" {
  description = "Raw ingestion API shared secret (modules/security output: ingestion_api_key, sensitive)."
  type        = string
  sensitive   = true
}

variable "minio_root_user" {
  description = "Raw MinIO root username (modules/security output: minio_root_user, sensitive)."
  type        = string
  sensitive   = true
}

variable "minio_root_password" {
  description = "Raw MinIO root password (modules/security output: minio_root_password, sensitive)."
  type        = string
  sensitive   = true
}

# --- ai module cross-module inputs --------------------------------------
variable "openai_endpoint" {
  description = "modules/ai output: openai_endpoint. Used as EMBEDDING_SERVICE_URL when embedding_profile = azure_openai."
  type        = string
  default     = ""
}

variable "openai_embedding_deployment_name" {
  description = "modules/ai output: embedding_deployment_name. Used as EMBEDDING_MODEL_ID when embedding_profile = azure_openai."
  type        = string
  default     = ""
}

variable "openai_key" {
  description = "modules/ai output: openai_key (sensitive). Raw Azure OpenAI primary key, materialized directly as EMBEDDING_API_KEY."
  type        = string
  sensitive   = true
  default     = ""
}

variable "openai_embedding_dim" {
  description = "modules/ai output: dim. Used as EMBEDDING_DIM regardless of embedding_profile."
  type        = number
  default     = 1536
}

# --- aks module cross-module inputs --------------------------------------
variable "aks_pod_cidr" {
  description = "modules/aks output: pod_cidr. Used as the public-api TRUSTED_PROXIES value (the ingress controller's pods live in this range under Azure CNI Overlay)."
  type        = string
}

variable "workload_identity_client_id" {
  description = "modules/security output: workload_identity_client_id. Annotated onto the chart's ServiceAccount (azure.workload.identity/client-id) for pods to auth to Key Vault/Blob without secrets. NOTE for the integrator: modules/security's federated_identity_credential subject must be \"system:serviceaccount:<namespace>:inherent\" (namespace = var.namespace, default \"inherent\") -- keep this in sync if the SA name (fixed \"inherent\" in charts/inherent/values.yaml) or namespace ever changes."
  type        = string
}

# --- deployment profile knobs (root vars, passed straight through) -------

variable "embedding_profile" {
  description = "Root var: embedding_profile (azure_openai|tei)."
  type        = string
  default     = "azure_openai"
  validation {
    condition     = contains(["azure_openai", "tei"], var.embedding_profile)
    error_message = "embedding_profile must be azure_openai or tei."
  }
}

variable "ingress_profile" {
  description = "Root var: ingress_profile (nginx|appgw_waf)."
  type        = string
  default     = "nginx"
  validation {
    condition     = contains(["nginx", "appgw_waf"], var.ingress_profile)
    error_message = "ingress_profile must be nginx or appgw_waf."
  }
}

variable "enable_ha" {
  description = "Root var: enable_ha — drives temporal.server.replicas (2 vs 1) and weaviate's Premium_ZRS StorageClass."
  type        = bool
  default     = true
}

variable "enable_dr" {
  description = "Root var: enable_dr — gates weaviate.backup.enabled and minio.mirror.enabled in the chart."
  type        = bool
  default     = true
}

variable "inherent_version" {
  description = "Root var: inherent_version — image tag for public-api-svc/ingestion-svc. Must NOT be \"latest\" in prod (guard test)."
  type        = string
}

variable "image_registry" {
  description = "GHCR registry hosting the two custom images. Matches docker-compose.release.yml's INHERENT_REGISTRY default."
  type        = string
  default     = "ghcr.io/inherent-prime"
}

variable "api_replicas_min" {
  description = "Root var: api_replicas_min."
  type        = number
  default     = 2
}

variable "api_replicas_max" {
  description = "Root var: api_replicas_max."
  type        = number
  default     = 6
}

variable "worker_replicas" {
  description = "Root var: worker_replicas."
  type        = number
  default     = 2
}

variable "weaviate_disk_gb" {
  description = "Root var: weaviate_disk_gb."
  type        = number
  default     = 64
}

variable "minio_disk_gb" {
  description = "Root var: minio_disk_gb."
  type        = number
  default     = 128
}

# --- ingress ---------------------------------------------------------------
variable "api_hostname" {
  description = "Root var: api_hostname (or derived from dns_zone_name + dns_record — root's choice; this module just needs the final FQDN). Ingress host for inh-public-api."
  type        = string
}

variable "letsencrypt_email" {
  description = "Root var: letsencrypt_email. cert-manager ClusterIssuer contact address. Required when ingress_profile = \"nginx\" (cert-manager path); ignored for appgw_waf (WAF_v2 path documents its own TLS story in docs/deploy/azure.md — App Gateway certs are not automated by this module)."
  type        = string
  default     = ""
}

variable "ingress_nginx_chart_version" {
  description = "Pinned ingress-nginx helm chart version."
  type        = string
  default     = "4.11.3"
}

variable "cert_manager_chart_version" {
  description = "Pinned cert-manager helm chart version."
  type        = string
  default     = "v1.16.2"
}

# --- appgw_waf path only ---------------------------------------------------
variable "appgw_subnet_id" {
  description = "Dedicated Application Gateway subnet id (network module output: subnet_ids.appgw). Required when ingress_profile = \"appgw_waf\"."
  type        = string
  default     = ""
}
