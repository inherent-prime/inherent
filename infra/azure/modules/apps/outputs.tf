# Outputs per .memory/azure-build-spec.md cross-module interface:
#   apps: api_service annotation → ingress ip/fqdn

output "namespace" {
  description = "Kubernetes namespace the chart is installed into."
  value       = kubernetes_namespace.inherent.metadata[0].name
}

output "api_hostname" {
  description = "Ingress host configured for inh-public-api (passthrough of var.api_hostname)."
  value       = var.api_hostname
}

output "api_fqdn" {
  description = "Public API FQDN — root outputs.tf's api_fqdn and scripts/deploy-azure.sh's readiness wait read this directly (no try(), so a rename here breaks validate loudly instead of silently going null)."
  value       = var.api_hostname
}

output "appgw_id" {
  description = <<-EOT
    Application Gateway resource id, or null when ingress_profile = "nginx".
    Wire this into modules/aks's appgw_id input to enable the AGIC add-on —
    see ingress.tf's module-level comment for why this does not create a
    dependency cycle.
  EOT
  value       = var.ingress_profile == "appgw_waf" ? azurerm_application_gateway.this[0].id : null
}

output "appgw_public_ip" {
  description = "Application Gateway's public IP address, or null when ingress_profile = \"nginx\" — point api_hostname's DNS A record at this (documented in docs/deploy/azure.md)."
  value       = var.ingress_profile == "appgw_waf" ? azurerm_public_ip.appgw[0].ip_address : null
}

# Best-effort: the ingress-nginx Service's LoadBalancer IP is assigned
# asynchronously by the Azure LB after helm_release.ingress_nginx completes,
# so this can read as empty immediately post-apply even though it converges
# within a minute or two — scripts/deploy-azure.sh's "wait for ingress IP"
# step (not this module) is the reliable way to obtain it during a fresh
# deploy; this output is a convenience for `terraform output` afterwards.
data "kubernetes_service" "ingress_nginx_controller" {
  count = var.ingress_profile == "nginx" ? 1 : 0

  metadata {
    name      = "ingress-nginx-controller"
    namespace = "ingress-nginx"
  }

  depends_on = [helm_release.ingress_nginx]
}

output "ingress_nginx_lb_ip" {
  description = "ingress-nginx controller Service's LoadBalancer IP, or null when ingress_profile = \"appgw_waf\". May be empty immediately post-apply — see comment above."
  value = (
    var.ingress_profile == "nginx" && length(data.kubernetes_service.ingress_nginx_controller) > 0
    ? try(data.kubernetes_service.ingress_nginx_controller[0].status[0].load_balancer[0].ingress[0].ip, null)
    : null
  )
}

output "kubernetes_secret_names" {
  description = "Map of chart secret role -> in-cluster kubernetes_secret name (weaviate_api_key/ingestion_api_key/minio_root/etc. Key Vault secret NAMEs are owned by modules/security and modules/ai, not re-exposed here — this module only consumed their raw sensitive passthroughs to build these kubernetes_secret objects)."
  value = {
    postgres          = kubernetes_secret.postgres.metadata[0].name
    mongodb           = kubernetes_secret.mongodb.metadata[0].name
    weaviate          = kubernetes_secret.weaviate.metadata[0].name
    redis             = kubernetes_secret.redis.metadata[0].name
    s3                = kubernetes_secret.s3.metadata[0].name
    ingestion         = kubernetes_secret.ingestion.metadata[0].name
    temporal_postgres = kubernetes_secret.temporal_postgres.metadata[0].name
  }
}
