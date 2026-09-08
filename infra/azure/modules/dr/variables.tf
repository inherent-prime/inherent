# DR module — issue #325. Deliberately thin: most of the DR mechanism (GRS
# storage, the nightly `mc mirror` CronJob, the weaviate backup-azure module)
# is configured in modules/data and modules/apps, not here — this module
# only owns the one resource that has no natural home in either (the
# optional cross-region PG read replica) plus a documentation-facing summary
# output. See dr_summary below for the full picture across all components.

variable "resource_prefix" {
  description = "Naming prefix for all resources (root var: resource_prefix)."
  type        = string
}

variable "environment" {
  description = "Deployment environment tag (root var: environment)."
  type        = string
  default     = "prod"
}

variable "enable_dr" {
  description = "Root var: enable_dr. Gates every resource in this module, and documents (via dr_summary) the DR posture of components owned by other modules."
  type        = bool
  default     = true
}

variable "location_dr" {
  description = "Root var: location_dr. Paired/secondary Azure region the PG geo-replica (when enabled) and the mirrored Blob/backup data land in."
  type        = string
}

variable "resource_group_name" {
  description = "Resource group the geo-replica is created in. Can be the primary resource group (Azure resources aren't region-locked to their RG) or a dedicated DR resource group — root's choice."
  type        = string
}

variable "tags" {
  description = "Common resource tags (root var: tags)."
  type        = map(string)
  default     = {}
}

variable "pg_geo_replica" {
  description = <<-EOT
    Root var: pg_geo_replica. When true (and enable_dr = true), creates a
    cross-region PostgreSQL Flexible Server read replica in location_dr —
    promotable to primary for a regional failover (RTO-oriented). When false,
    PG's DR coverage is geo-redundant backup only (modules/data,
    geo_backup_enabled — RPO-oriented, requires a restore rather than a
    promote). Off by default: read replicas cost a full second PG instance
    continuously running; most deployments start with geo-backup only.
  EOT
  type        = bool
  default     = false
}

variable "pg_source_server_id" {
  description = "PostgreSQL Flexible Server resource id to replicate from (modules/data output). Required when pg_geo_replica = true; the source server must have geo_redundant_backup_enabled = true."
  type        = string
  default     = ""

  validation {
    condition     = !var.pg_geo_replica || var.pg_source_server_id != ""
    error_message = "pg_source_server_id is required when pg_geo_replica = true."
  }
}

variable "storage_account_grs_name" {
  description = "GRS storage account name (modules/data output) that MinIO mirrors into and weaviate's backup-azure module writes to — passthrough, used only to populate dr_summary."
  type        = string
  default     = ""
}
