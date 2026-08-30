# Monitoring module — issue #325. Owns the Log Analytics workspace consumed
# by modules/aks's oms_agent (Container Insights) — see azure-build-spec.md
# cross-module interface: "aks takes it as input per spec". Root main.tf
# must therefore compose this module BEFORE aks.

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
  description = "Azure region (root var: location)."
  type        = string
}

variable "resource_group_name" {
  description = "Resource group for the workspace, action group, and alert rules."
  type        = string
}

variable "tags" {
  description = "Common resource tags (root var: tags)."
  type        = map(string)
  default     = {}
}

variable "log_retention_days" {
  description = "Log Analytics workspace retention, in days. 30 is the free-tier-friendly default; raise for longer audit/compliance windows (cost scales with retention x ingestion volume)."
  type        = number
  default     = 30
}

variable "alert_email_address" {
  description = <<-EOT
    Email receiver for the action group. NOTE for the root-module integrator:
    this is not yet in the "Root variables (exact names)" list in
    .memory/azure-build-spec.md — add e.g. `alert_email_address` to root
    variables.tf and wire it through, or pass a fixed value at the call site.
    Required (no default) so alerts are never silently unroutable.
  EOT
  type        = string
}

variable "pg_server_id" {
  description = <<-EOT
    PostgreSQL Flexible Server resource id (modules/data output — data module
    must expose this in addition to pg_fqdn for these alerts to attach).
    Empty string skips the PG CPU/storage alerts (e.g. in a profile without
    managed PG).
  EOT
  type        = string
  default     = ""
}

variable "redis_cache_id" {
  description = <<-EOT
    Azure Cache for Redis resource id (modules/data output — data module must
    expose this in addition to redis_hostname for the memory alert to attach).
    Empty string skips the Redis memory alert.
  EOT
  type        = string
  default     = ""
}

variable "appgw_id" {
  description = <<-EOT
    Application Gateway resource id (modules/apps output, same value passed
    to modules/aks's appgw_id — see that module's variables.tf). When set
    (ingress_profile = "appgw_waf"), an UnhealthyHostCount metric alert is
    created against it. When null (nginx profile), API availability is
    instead approximated by the container-restart log query alert below —
    per azure-build-spec.md's documented fallback (AKS's own LB doesn't
    expose a clean per-backend health metric to alert on).
  EOT
  type        = string
  default     = null
}

variable "app_namespace" {
  description = "Kubernetes namespace the chart deploys into — scopes the container-restart log query. Must match modules/apps' kubernetes_namespace (\"inherent\" per issue #323)."
  type        = string
  default     = "inherent"
}

variable "ingress_namespace" {
  description = "Namespace the ingress controller runs in — scopes the 5xx-rate log query. Matches the ingress-nginx helm_release's target namespace in modules/apps."
  type        = string
  default     = "ingress-nginx"
}
