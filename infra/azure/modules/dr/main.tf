# DR module — issue #325. Everything here is gated by enable_dr; see
# variables.tf for why this module is intentionally small.

# Cross-region PG read replica. Optional (pg_geo_replica) — most deployments
# rely on geo-redundant backup (modules/data) alone; a live replica adds a
# second continuously-running PG instance's cost for a lower RTO on failover.
resource "azurerm_postgresql_flexible_server" "geo_replica" {
  count = var.enable_dr && var.pg_geo_replica ? 1 : 0

  name                = "${var.resource_prefix}-${var.environment}-pg-dr"
  resource_group_name = var.resource_group_name
  location            = var.location_dr
  tags                = var.tags

  create_mode      = "Replica"
  source_server_id = var.pg_source_server_id
}
