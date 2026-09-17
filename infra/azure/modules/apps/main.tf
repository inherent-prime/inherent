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

# --- Workload identity federation --------------------------------------
# The chart's ServiceAccount is annotated with the workload identity's client-id
# (helm.tf, azure.workload.identity/client-id) but that annotation alone grants
# nothing — AKS's workload-identity webhook only exchanges a pod's projected
# ServiceAccount token for an Azure AD token when a matching
# federated_identity_credential exists on the identity, scoped to this exact
# "system:serviceaccount:<namespace>:<name>" subject. Without this resource the
# chart's Key Vault CSI mounts / Blob SDK calls from pods fail auth silently
# (the annotation looks correct but the token exchange 400s). "inherent" is the
# chart's fixed ServiceAccount name (charts/inherent/values.yaml
# serviceAccount.name) — kept in sync manually, not read from the chart.
resource "azurerm_federated_identity_credential" "workload" {
  name                = "${var.resource_prefix}-${var.environment}-workload-fic"
  resource_group_name = var.resource_group_name
  parent_id           = var.workload_identity_id
  audience            = ["api://AzureADTokenExchange"]
  issuer              = var.aks_oidc_issuer_url
  subject             = "system:serviceaccount:${var.namespace}:inherent"
}
