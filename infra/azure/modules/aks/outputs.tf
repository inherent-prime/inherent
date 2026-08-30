# Outputs per .memory/azure-build-spec.md cross-module interface:
#   aks: cluster_name/id, kube_admin config (sensitive), oidc_issuer,
#        node_resource_group, log_analytics_id
# kube_config_* outputs feed the root helm/kubernetes provider blocks
# (providers.tf, owned by the root agent) so apps/monitoring's helm_release
# and kubernetes_* resources can talk to this cluster.

output "cluster_name" {
  description = "AKS cluster name."
  value       = azurerm_kubernetes_cluster.this.name
}

output "cluster_id" {
  description = "AKS cluster resource id."
  value       = azurerm_kubernetes_cluster.this.id
}

output "kube_config_host" {
  description = "Kubernetes API server endpoint, for the root helm/kubernetes provider config."
  value       = azurerm_kubernetes_cluster.this.kube_config[0].host
  sensitive   = true
}

output "kube_config_client_certificate" {
  description = "Client certificate (PEM, base64) for the root helm/kubernetes provider config."
  value       = azurerm_kubernetes_cluster.this.kube_config[0].client_certificate
  sensitive   = true
}

output "kube_config_client_key" {
  description = "Client key (PEM, base64) for the root helm/kubernetes provider config."
  value       = azurerm_kubernetes_cluster.this.kube_config[0].client_key
  sensitive   = true
}

output "kube_config_cluster_ca_certificate" {
  description = "Cluster CA certificate (PEM, base64) for the root helm/kubernetes provider config."
  value       = azurerm_kubernetes_cluster.this.kube_config[0].cluster_ca_certificate
  sensitive   = true
}

output "kube_config_raw" {
  description = "Full raw kubeconfig, for `outputs.tf`'s documented `az aks get-credentials` alternative / kubeconfig cmd output."
  value       = azurerm_kubernetes_cluster.this.kube_config_raw
  sensitive   = true
}

output "oidc_issuer_url" {
  description = "OIDC issuer URL, for workload-identity federated_identity_credential resources (modules/security)."
  value       = azurerm_kubernetes_cluster.this.oidc_issuer_url
}

output "node_resource_group" {
  description = "The auto-created MC_* resource group holding node VMs, disks, the cluster LB, and (when enabled) the AGIC-managed Application Gateway's node-side NIC associations."
  value       = azurerm_kubernetes_cluster.this.node_resource_group
}

output "log_analytics_id" {
  description = "Pass-through of the log_analytics_workspace_id input this cluster's oms_agent was wired to (convenience for callers that only reference the aks module output, e.g. docs generation)."
  value       = var.log_analytics_workspace_id
}

output "pod_cidr" {
  description = "Overlay pod address space (var.pod_cidr passthrough) — modules/apps uses this for the public-api TRUSTED_PROXIES value (ingress controller pods live in this range)."
  value       = var.pod_cidr
}

output "kubelet_identity_object_id" {
  description = "Object id of the cluster's kubelet managed identity — needed by modules/security for ACR pull / Key Vault CSI role assignments."
  value       = azurerm_kubernetes_cluster.this.kubelet_identity[0].object_id
}
