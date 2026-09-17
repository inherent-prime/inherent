# AKS module — issue #323. Inputs are named to match the root variables.tf
# documented in .memory/azure-build-spec.md; the root module (owned by the
# terraform-root agent) is expected to pass these straight through 1:1.

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
  description = "Azure region for the cluster (root var: location)."
  type        = string
}

variable "resource_group_name" {
  description = "Resource group the cluster is created in (from the root/network layer)."
  type        = string
}

variable "tags" {
  description = "Common resource tags (root var: tags)."
  type        = map(string)
  default     = {}
}

variable "subnet_id" {
  description = "Subnet id for the AKS node pools (network module output: subnet_ids.aks)."
  type        = string
}

variable "pod_cidr" {
  description = <<-EOT
    Overlay pod address space (network_profile.pod_cidr) — required by Azure
    whenever network_plugin_mode = "overlay" (this module always uses
    overlay, see main.tf). Must not overlap the AKS subnet or any other
    routable range in the VNet. modules/apps' TRUSTED_PROXIES value and any
    NetworkPolicy ipBlock tightening should reference this same CIDR.
  EOT
  type        = string
  default     = "10.244.0.0/16"
}

variable "service_cidr" {
  description = <<-EOT
    Kubernetes Service ClusterIP address space (network_profile.service_cidr). Azure's own
    documented default for a fresh cluster is 10.0.0.0/16; this module sets it explicitly
    (rather than leaving it to that implicit default) so modules/apps' networkPolicy.serviceCidr
    input is always a value this module actually configured, not an assumption about what
    Azure would have picked. Must not overlap pod_cidr, the AKS subnet, or any other routable
    VNet range.
  EOT
  type        = string
  default     = "10.0.0.0/16"
}

variable "enable_ha" {
  description = <<-EOT
    Root var: enable_ha. When true, node pools spread across all 3 availability
    zones in the region and the user pool's min_count/max_count give headroom
    for a zone loss without dropping below api_replicas_min scheduling capacity.
    When false, the cluster is single-zone (dev profile cost floor).
  EOT
  type        = bool
  default     = true
}

variable "private_cluster_enabled" {
  description = "Root var: private_cluster_enabled. true = API server has no public endpoint (enterprise VNet mode); reach it via VPN/ExpressRoute/jumpbox."
  type        = bool
  default     = false
}

variable "authorized_ip_ranges" {
  description = <<-EOT
    Root var: authorized_ip_ranges. CIDRs allowed to reach the public API server
    endpoint (api_server_access_profile.authorized_ip_ranges). Ignored when
    private_cluster_enabled = true (no public endpoint to restrict). Empty list
    = no IP allowlist (any source can reach the API server, auth still required).
  EOT
  type        = list(string)
  default     = []
}

variable "aks_system_vm_size" {
  description = "Root var: aks_system_vm_size. VM size for the system node pool (CriticalAddonsOnly, runs cluster add-ons only)."
  type        = string
  default     = "Standard_D2s_v5"
}

variable "aks_user_vm_size" {
  description = "Root var: aks_user_vm_size. VM size for the user (workload) node pool."
  type        = string
  default     = "Standard_D4s_v5"
}

variable "aks_user_min_count" {
  description = "Root var: aks_user_min_count. Cluster-autoscaler floor for the user pool."
  type        = number
  default     = 3
}

variable "aks_user_max_count" {
  description = "Root var: aks_user_max_count. Cluster-autoscaler ceiling for the user pool — raise this to scale past ~20 QPS (see docs/deploy/azure.md scale ceilings table)."
  type        = number
  default     = 6
}

variable "kubernetes_version" {
  description = "AKS control-plane + default node pool Kubernetes version. null = AKS-selected default at apply time; pin explicitly for reproducible prod rollouts."
  type        = string
  default     = null
}

variable "sku_tier" {
  description = <<-EOT
    AKS control-plane pricing tier. "Standard" is the paid tier with the
    financially-backed 99.95% uptime SLA — a mission-critical requirement for
    this deployment (issue #323), so it is the default and not left to the
    free "Free" tier's best-effort SLO. "Premium" adds Long Term Support only;
    not needed here.
  EOT
  type        = string
  default     = "Standard"
  validation {
    condition     = contains(["Free", "Standard", "Premium"], var.sku_tier)
    error_message = "sku_tier must be Free, Standard, or Premium."
  }
}

variable "log_analytics_workspace_id" {
  description = <<-EOT
    Log Analytics workspace id wired into the oms_agent (Container Insights)
    block. Cross-module interface (azure-build-spec.md): the monitoring module
    OUTPUTS workspace_id; the root composes monitoring before aks and passes
    monitoring.workspace_id in here. See module-level note in main.tf.
  EOT
  type        = string
}

variable "appgw_id" {
  description = <<-EOT
    Application Gateway resource id, wired into the AGIC (ingress_application_gateway)
    cluster add-on. Set only when ingress_profile = "appgw_waf" (root composes
    apps' AppGW before aks and passes its id here — see modules/apps comments
    for the sequencing note). null = AGIC add-on disabled (nginx ingress profile).
  EOT
  type        = string
  default     = null
}
