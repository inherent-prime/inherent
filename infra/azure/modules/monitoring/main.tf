# Monitoring module — issue #325.

resource "azurerm_log_analytics_workspace" "this" {
  name                = "${var.resource_prefix}-${var.environment}-law"
  location            = var.location
  resource_group_name = var.resource_group_name
  sku                 = "PerGB2018"
  retention_in_days   = var.log_retention_days
  tags                = var.tags
}

resource "azurerm_monitor_action_group" "this" {
  name                = "${var.resource_prefix}-${var.environment}-ag"
  resource_group_name = var.resource_group_name
  # short_name is capped at 12 chars by the API.
  short_name = substr("${var.resource_prefix}alrt", 0, 12)
  tags       = var.tags

  email_receiver {
    name                    = "primary"
    email_address           = var.alert_email_address
    use_common_alert_schema = true
  }
}

# --- Metric alerts -----------------------------------------------------

resource "azurerm_monitor_metric_alert" "pg_cpu" {
  count = var.pg_server_id != "" ? 1 : 0

  name                = "${var.resource_prefix}-${var.environment}-pg-cpu-high"
  resource_group_name = var.resource_group_name
  scopes              = [var.pg_server_id]
  description         = "PostgreSQL Flexible Server CPU > 80% for 15m — investigate slow queries / connection storms before it degrades p95 search latency."
  severity            = 2
  frequency           = "PT5M"
  window_size         = "PT15M"
  tags                = var.tags

  criteria {
    metric_namespace = "Microsoft.DBforPostgreSQL/flexibleServers"
    metric_name      = "cpu_percent"
    aggregation      = "Average"
    operator         = "GreaterThan"
    threshold        = 80
  }

  action {
    action_group_id = azurerm_monitor_action_group.this.id
  }
}

resource "azurerm_monitor_metric_alert" "pg_storage" {
  count = var.pg_server_id != "" ? 1 : 0

  name                = "${var.resource_prefix}-${var.environment}-pg-storage-high"
  resource_group_name = var.resource_group_name
  scopes              = [var.pg_server_id]
  description         = "PostgreSQL Flexible Server storage > 80% — raise pg_storage_mb before autogrow (if disabled) hits a hard stop."
  severity            = 1
  frequency           = "PT15M"
  window_size         = "PT30M"
  tags                = var.tags

  criteria {
    metric_namespace = "Microsoft.DBforPostgreSQL/flexibleServers"
    metric_name      = "storage_percent"
    aggregation      = "Average"
    operator         = "GreaterThan"
    threshold        = 80
  }

  action {
    action_group_id = azurerm_monitor_action_group.this.id
  }
}

resource "azurerm_monitor_metric_alert" "redis_memory" {
  count = var.redis_cache_id != "" ? 1 : 0

  name                = "${var.resource_prefix}-${var.environment}-redis-mem-high"
  resource_group_name = var.resource_group_name
  scopes              = [var.redis_cache_id]
  description         = "Azure Cache for Redis used memory > 85% — with maxmemory-policy=noeviction (required for Streams durability, see modules/data) this risks OOM write rejection rather than silent eviction."
  severity            = 1
  frequency           = "PT5M"
  window_size         = "PT15M"
  tags                = var.tags

  criteria {
    metric_namespace = "Microsoft.Cache/Redis"
    metric_name      = "usedmemorypercentage"
    aggregation      = "Average"
    operator         = "GreaterThan"
    threshold        = 85
  }

  action {
    action_group_id = azurerm_monitor_action_group.this.id
  }
}

# API availability, App Gateway path: unhealthy backend host count on the
# WAF_v2 gateway. Only created when ingress_profile = "appgw_waf" (appgw_id
# set) — see variables.tf for the nginx-profile fallback.
resource "azurerm_monitor_metric_alert" "appgw_unhealthy_hosts" {
  count = var.appgw_id != null ? 1 : 0

  name                = "${var.resource_prefix}-${var.environment}-appgw-unhealthy-hosts"
  resource_group_name = var.resource_group_name
  scopes              = [var.appgw_id]
  description         = "Application Gateway has >0 unhealthy backend hosts — public-api pods are failing their AGIC health probe."
  severity            = 1
  frequency           = "PT1M"
  window_size         = "PT5M"
  tags                = var.tags

  criteria {
    metric_namespace = "Microsoft.Network/applicationGateways"
    metric_name      = "UnhealthyHostCount"
    aggregation      = "Average"
    operator         = "GreaterThan"
    threshold        = 0
  }

  action {
    action_group_id = azurerm_monitor_action_group.this.id
  }
}

# --- Log query alerts (Container Insights over the workspace above) ----

resource "azurerm_monitor_scheduled_query_rules_alert_v2" "aks_node_not_ready" {
  name                 = "${var.resource_prefix}-${var.environment}-aks-node-not-ready"
  resource_group_name  = var.resource_group_name
  location             = var.location
  scopes               = [azurerm_log_analytics_workspace.this.id]
  severity             = 1
  evaluation_frequency = "PT5M"
  window_duration      = "PT10M"
  description          = "One or more AKS nodes reporting NotReady — cluster autoscaler / node health issue, capacity may be degraded."
  tags                 = var.tags

  criteria {
    query                   = <<-QUERY
      KubeNodeInventory
      | where TimeGenerated > ago(10m)
      | summarize LatestStatus = arg_max(TimeGenerated, Status) by Computer
      | where LatestStatus != "Ready"
      | summarize NotReadyCount = dcount(Computer)
    QUERY
    time_aggregation_method = "Maximum"
    threshold               = 0
    operator                = "GreaterThan"

    failing_periods {
      minimum_failing_periods_to_trigger_alert = 1
      number_of_evaluation_periods             = 1
    }
  }

  action {
    action_groups = [azurerm_monitor_action_group.this.id]
  }
}

# API availability, nginx fallback (spec: "if not clean, alert on container
# restarts via log query alert" — AKS's platform LB has no clean per-backend
# health metric to alert on directly, unlike the AppGW path above).
resource "azurerm_monitor_scheduled_query_rules_alert_v2" "public_api_restarts" {
  name                 = "${var.resource_prefix}-${var.environment}-public-api-restarts"
  resource_group_name  = var.resource_group_name
  location             = var.location
  scopes               = [azurerm_log_analytics_workspace.this.id]
  severity             = 2
  evaluation_frequency = "PT5M"
  window_duration      = "PT15M"
  description          = "inh-public-api containers restarting repeatedly — crashloop or failing readiness/liveness probes."
  tags                 = var.tags

  criteria {
    query                   = <<-QUERY
      KubePodInventory
      | where TimeGenerated > ago(15m)
      | where Namespace == "${var.app_namespace}"
      | where Name has "inh-public-api"
      | summarize RestartCount = max(ContainerRestartCount) by Name
      | summarize TotalRestarts = sum(RestartCount)
    QUERY
    time_aggregation_method = "Maximum"
    threshold               = 3
    operator                = "GreaterThan"

    failing_periods {
      minimum_failing_periods_to_trigger_alert = 1
      number_of_evaluation_periods             = 1
    }
  }

  action {
    action_groups = [azurerm_monitor_action_group.this.id]
  }
}

resource "azurerm_monitor_scheduled_query_rules_alert_v2" "ingress_5xx_rate" {
  name                 = "${var.resource_prefix}-${var.environment}-ingress-5xx-rate"
  resource_group_name  = var.resource_group_name
  location             = var.location
  scopes               = [azurerm_log_analytics_workspace.this.id]
  severity             = 2
  evaluation_frequency = "PT5M"
  window_duration      = "PT5M"
  description          = <<-EOT
    5xx rate on the ingress controller's access log > 10 in 5m. Best-effort
    text match against the default nginx access log line — retune this KQL
    if ingress-nginx JSON logging is enabled (recommended: swap `matches
    regex` for a `parse-json` + status field comparison, more reliable).
  EOT
  tags                 = var.tags

  criteria {
    query                   = <<-QUERY
      ContainerLogV2
      | where TimeGenerated > ago(5m)
      | where PodNamespace == "${var.ingress_namespace}"
      | where LogMessage matches regex @'" 5[0-9]{2} '
      | summarize FiveXXCount = count()
    QUERY
    time_aggregation_method = "Maximum"
    threshold               = 10
    operator                = "GreaterThan"

    failing_periods {
      minimum_failing_periods_to_trigger_alert = 1
      number_of_evaluation_periods             = 1
    }
  }

  action {
    action_groups = [azurerm_monitor_action_group.this.id]
  }
}
