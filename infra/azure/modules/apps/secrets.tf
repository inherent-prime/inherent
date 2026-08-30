# kubernetes_secret resources materializing values sourced from
# modules/data, modules/security, and modules/ai (sensitive TF outputs, see
# variables.tf) into the cluster. charts/inherent/values.yaml's `secrets.*`
# block references these by name/key — kept in sync manually, see the
# comment above each block for which chart value it feeds.

resource "kubernetes_secret" "postgres" {
  metadata {
    name      = "${var.resource_prefix}-postgres"
    namespace = kubernetes_namespace.inherent.metadata[0].name
  }
  data = {
    DATABASE_URL = var.postgres_app_connection_string
  }
  type = "Opaque"
}

resource "kubernetes_secret" "mongodb" {
  metadata {
    name      = "${var.resource_prefix}-mongodb"
    namespace = kubernetes_namespace.inherent.metadata[0].name
  }
  data = {
    MONGODB_URI = var.cosmos_connection_string
  }
  type = "Opaque"
}

resource "kubernetes_secret" "weaviate" {
  metadata {
    name      = "${var.resource_prefix}-weaviate"
    namespace = kubernetes_namespace.inherent.metadata[0].name
  }
  data = {
    WEAVIATE_API_KEY = var.weaviate_api_key
  }
  type = "Opaque"
}

resource "kubernetes_secret" "redis" {
  metadata {
    name      = "${var.resource_prefix}-redis"
    namespace = kubernetes_namespace.inherent.metadata[0].name
  }
  data = {
    REDIS_URL = var.redis_connection_string
  }
  type = "Opaque"
}

resource "kubernetes_secret" "s3" {
  metadata {
    name      = "${var.resource_prefix}-minio-root"
    namespace = kubernetes_namespace.inherent.metadata[0].name
  }
  data = {
    MINIO_ROOT_USER     = var.minio_root_user
    MINIO_ROOT_PASSWORD = var.minio_root_password
  }
  type = "Opaque"
}

resource "kubernetes_secret" "ingestion" {
  metadata {
    name      = "${var.resource_prefix}-ingestion"
    namespace = kubernetes_namespace.inherent.metadata[0].name
  }
  data = {
    INGESTION_API_KEY = var.ingestion_api_key
  }
  type = "Opaque"
}

resource "kubernetes_secret" "embedding_api_key" {
  count = var.embedding_profile == "azure_openai" ? 1 : 0
  metadata {
    name      = "${var.resource_prefix}-embedding"
    namespace = kubernetes_namespace.inherent.metadata[0].name
  }
  data = {
    EMBEDDING_API_KEY = var.openai_key
  }
  type = "Opaque"
}

# Same PG admin password as secrets.postgres, but shaped for temporal's
# discrete POSTGRES_USER/POSTGRES_PWD env vars (temporalio/server, unlike
# the app services, doesn't take a single DSN — see charts/inherent
# values.yaml's temporal.postgres block). This is the one secret this
# module reads from Key Vault directly rather than taking as a sensitive
# input variable — see main.tf.
resource "kubernetes_secret" "temporal_postgres" {
  metadata {
    name      = "${var.resource_prefix}-temporal-postgres"
    namespace = kubernetes_namespace.inherent.metadata[0].name
  }
  data = {
    POSTGRES_PASSWORD = data.azurerm_key_vault_secret.pg_password.value
  }
  type = "Opaque"
}

# DR only: AZURE_STORAGE_CONNECTION_STRING for weaviate's backup-azure
# module and the MinIO mirror CronJob.
resource "kubernetes_secret" "backup_blob" {
  count = var.enable_dr ? 1 : 0
  metadata {
    name      = "${var.resource_prefix}-backup-blob"
    namespace = kubernetes_namespace.inherent.metadata[0].name
  }
  data = {
    AZURE_STORAGE_CONNECTION_STRING = var.storage_account_connection_string
  }
  type = "Opaque"
}
