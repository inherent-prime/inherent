# Outputs per .memory/azure-build-spec.md cross-module interface:
#   monitoring: workspace_id, action_group_id

output "workspace_id" {
  description = "Log Analytics workspace id — feed into modules/aks's log_analytics_workspace_id input (Container Insights) and into modules/apps for any diagnostic_setting wiring."
  value       = azurerm_log_analytics_workspace.this.id
}

output "workspace_name" {
  description = "Log Analytics workspace name (for az monitor / portal links in docs)."
  value       = azurerm_log_analytics_workspace.this.name
}

output "action_group_id" {
  description = "Action group id — reusable by other modules that want to wire additional alerts (e.g. modules/dr for a geo-replica lag alert)."
  value       = azurerm_monitor_action_group.this.id
}
