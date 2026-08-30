# AKS module — issue #323 (compute layer). See .memory/azure-build-spec.md
# "Cross-module interface" for the exact output contract consumed by the
# helm/kubernetes providers (root providers.tf) and by modules/apps and
# modules/monitoring.

locals {
  cluster_name = "${var.resource_prefix}-${var.environment}-aks"
  # 3-AZ spread is the mission-critical posture (#323): losing one zone still
  # leaves capacity in the other two. Single-zone (enable_ha=false) is the
  # dev/eval cost floor from the workplan, not used in the prod profile.
  zones = var.enable_ha ? ["1", "2", "3"] : null
}

resource "azurerm_kubernetes_cluster" "this" {
  name                = local.cluster_name
  location            = var.location
  resource_group_name = var.resource_group_name
  dns_prefix          = "${var.resource_prefix}-${var.environment}"
  kubernetes_version  = var.kubernetes_version
  tags                = var.tags

  # Standard (paid) control-plane tier: this is the tier with the 99.95%
  # financially-backed uptime SLA. #323 is explicit that this deployment is
  # mission-critical (20 QPS prod target, HA+DR) — the "Free" tier's
  # best-effort SLO is not an acceptable substitute here.
  sku_tier = var.sku_tier

  # Azure CNI Overlay: pod IPs come from an overlay address space instead of
  # consuming VNet subnet IPs 1:1 per pod, so the AKS subnet doesn't need to
  # be sized for total pod count — matters once the user pool autoscales to
  # aks_user_max_count under load.
  network_profile {
    network_plugin      = "azure"
    network_plugin_mode = "overlay"
    network_policy      = "azure"
    # Required by Azure whenever network_plugin_mode = "overlay" — see
    # variables.tf. Must not overlap the AKS subnet.
    pod_cidr       = var.pod_cidr
    service_cidr   = var.service_cidr
    dns_service_ip = cidrhost(var.service_cidr, 10)
    # Standard LB SKU is required for zone redundancy and outbound rules at
    # this scale; azurerm only accepts "basic"/"standard" here (there is no
    # separate "standard_v2" enum value in this provider — Azure's Standard
    # LB is what's meant by that generation).
    load_balancer_sku = "standard"
    # userAssignedNATGateway, not the default "loadBalancer": modules/network attaches a NAT
    # gateway to the AKS subnet for stable, node-count-independent egress (see that module's
    # comment). A subnet with a NAT gateway attached cannot also use the platform LB for
    # outbound SNAT — "loadBalancer" here would conflict with the NAT gateway association at
    # apply time. Ingress/inbound traffic is unaffected: this only controls the cluster's
    # outbound path, not the Standard LB the ingress-nginx Service still provisions for inbound.
    outbound_type = "userAssignedNATGateway"
  }

  default_node_pool {
    name                         = "system"
    vm_size                      = var.aks_system_vm_size
    node_count                   = 3
    vnet_subnet_id               = var.subnet_id
    zones                        = local.zones
    only_critical_addons_enabled = true # keep app workloads off the system pool
    upgrade_settings {
      max_surge = "33%"
    }
  }

  identity {
    type = "SystemAssigned"
  }

  # Workload identity: pods authenticate to Azure (Key Vault, Blob) via a
  # federated OIDC credential instead of long-lived secrets. Required by
  # modules/security's user-assigned identity + federated_identity_credential
  # and by the weaviate/minio backup-to-Blob paths (#325 DR).
  oidc_issuer_enabled       = true
  workload_identity_enabled = true

  api_server_access_profile {
    authorized_ip_ranges = var.private_cluster_enabled ? null : var.authorized_ip_ranges
  }

  private_cluster_enabled = var.private_cluster_enabled

  # Container Insights: workspace is owned by modules/monitoring (#325) and
  # passed in here — see variables.tf note. Root main.tf must therefore
  # compose monitoring before aks.
  oms_agent {
    log_analytics_workspace_id = var.log_analytics_workspace_id
  }

  # AGIC add-on: only enabled when ingress_profile == "appgw_waf" (root
  # passes appgw_id = module.apps.appgw_id in that case; modules/apps owns
  # the azurerm_application_gateway resource itself — see modules/apps
  # ingress.tf for why this does not form a module dependency cycle: the App
  # Gateway resource has no dependency on this cluster, only the reverse).
  dynamic "ingress_application_gateway" {
    for_each = var.appgw_id != null ? [1] : []
    content {
      gateway_id = var.appgw_id
    }
  }

  lifecycle {
    ignore_changes = [
      # Node count is managed by the cluster autoscaler post-create; do not
      # fight it on every apply.
      default_node_pool[0].node_count,
    ]
  }
}

# User (workload) pool: separate from the system pool so cluster add-ons and
# app pods scale independently. Autoscaler floor/ceiling from root vars.
resource "azurerm_kubernetes_cluster_node_pool" "user" {
  name                  = "user"
  kubernetes_cluster_id = azurerm_kubernetes_cluster.this.id
  vm_size               = var.aks_user_vm_size
  vnet_subnet_id        = var.subnet_id
  zones                 = local.zones
  mode                  = "User"

  auto_scaling_enabled = true
  min_count            = var.aks_user_min_count
  max_count            = var.aks_user_max_count
  node_count           = var.aks_user_min_count

  upgrade_settings {
    max_surge = "33%"
  }

  lifecycle {
    ignore_changes = [node_count] # autoscaler-owned after initial create
  }
}
