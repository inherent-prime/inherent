# dr_summary: consumed by docs/deploy/azure-dr-runbook.md generation (per
# .memory/azure-build-spec.md repo layout: "runbook outputs"). Documents the
# DR mechanism for every stateful component even though most of them are
# configured in modules/data / modules/apps, not this module — see
# variables.tf for why.

locals {
  dr_summary = {
    postgresql = {
      mechanism = var.pg_geo_replica ? "cross-region read replica (promotable), plus geo-redundant backup" : "geo-redundant backup only (geo-restore)"
      rpo       = var.pg_geo_replica ? "~5 min (async replication lag)" : "<= 1 h (geo-backup interval)"
      rto       = var.pg_geo_replica ? "~minutes (promote replica)" : "<= 4 h (geo-restore + re-point DATABASE_URL)"
      owned_by  = "modules/data (+ modules/dr when pg_geo_replica=true)"
    }
    mongodb_cosmos = {
      mechanism = "Cosmos DB for MongoDB vCore geo-backup"
      rpo       = "<= 1 h"
      rto       = "<= 4 h (restore to new account, re-point MONGODB_URI)"
      owned_by  = "modules/data"
    }
    redis_cache = {
      # NOT cache-only: Redis Streams (MQ_BACKEND=redis) hold in-flight upload/ingestion
      # events, audit records, and consumer-group offsets — see modules/data's
      # maxmemory_policy=noeviction comment for why Streams durability matters here at all.
      # Losing the cache loses whatever hasn't been consumed/committed yet, same as any
      # message queue without persistence enabled.
      mechanism = "none — Redis has no backup/replication configured; a loss drops in-flight MQ Streams events (upload/ingestion jobs not yet consumed, audit records, consumer-group offsets). Recovery is re-publishing/re-ingesting: source documents already durable in MinIO/PG are re-submitted through the ingestion API to regenerate the events Redis lost, not a Redis-side restore."
      rpo       = "up to the in-flight queue depth at time of loss (not time-based — bounded by how much is unconsumed, not by an interval)"
      rto       = "minutes (re-provision Azure Cache for Redis, reconnect) + re-ingestion time for whatever was in flight"
      owned_by  = "modules/data"
    }
    weaviate_vectors = {
      mechanism = "backup-azure module -> GRS Blob container (weaviate-backups)"
      rpo       = "per backup schedule — see docs/deploy/azure-dr-runbook.md"
      rto       = "restore Blob backup into a fresh weaviate StatefulSet"
      owned_by  = "charts/inherent (installed by modules/apps)"
    }
    object_storage_minio = {
      mechanism = "hourly rclone sync CronJob, MinIO -> GRS Blob"
      rpo       = "<= 1 h (hourly mirror cadence)"
      rto       = "restore MinIO PVC from the mirrored Blob container"
      owned_by  = "charts/inherent (installed by modules/apps)"
    }
    temporal = {
      mechanism = "no independent backup — re-provisioned against the restored PG (temporal + temporal_visibility DBs)"
      rpo       = "tied to postgresql's RPO above"
      rto       = "re-run the schema-setup + namespace-register helm hook Jobs against the restored PG"
      owned_by  = "charts/inherent (installed by modules/apps)"
    }
    storage_account = {
      mechanism = var.storage_account_grs_name != "" ? "GRS storage account (${var.storage_account_grs_name}), Azure-managed cross-region replication" : "GRS storage account, Azure-managed cross-region replication"
      rpo       = "near-continuous (Azure-managed async geo-replication, typically < 15 min)"
      rto       = "customer-initiated storage account failover (Azure Storage GRS)"
      owned_by  = "modules/data"
    }
  }
}

output "dr_summary" {
  description = "Map: component -> {mechanism, rpo, rto, owned_by}. Empty map when enable_dr = false. Consumed by docs/deploy/azure-dr-runbook.md."
  value       = var.enable_dr ? local.dr_summary : {}
}

output "pg_geo_replica_id" {
  description = "Resource id of the cross-region PG read replica, or null when pg_geo_replica = false."
  value       = var.enable_dr && var.pg_geo_replica ? azurerm_postgresql_flexible_server.geo_replica[0].id : null
}

output "pg_geo_replica_fqdn" {
  description = "FQDN of the cross-region PG read replica, or null when pg_geo_replica = false. Promote this server and re-point DATABASE_URL at it during a regional failover (see docs/deploy/azure-dr-runbook.md)."
  value       = var.enable_dr && var.pg_geo_replica ? azurerm_postgresql_flexible_server.geo_replica[0].fqdn : null
}
