# Apps module — issue #323 / #324.

resource "kubernetes_namespace" "inherent" {
  metadata {
    name = var.namespace
    labels = {
      "app.kubernetes.io/part-of" = "inherent"
      # Required for the AKS workload-identity webhook to inject the
      # federated token into pods in this namespace.
      "azure.workload.identity/use" = "true"
    }
  }
}

# --- Read the ONE value modules/security intentionally does not pass
# through directly (see variables.tf's pg_password_kv_secret comment): PG
# admin password, needed by temporal as a discrete POSTGRES_PWD env var
# rather than embedded in a DSN. Every other secret this module needs
# arrives as a sensitive input variable straight from modules/data,
# modules/security, or modules/ai — see those variables' descriptions for
# which module's output each maps to. -------------------------------------

data "azurerm_key_vault_secret" "pg_password" {
  name         = var.pg_password_kv_secret
  key_vault_id = var.key_vault_id
}
