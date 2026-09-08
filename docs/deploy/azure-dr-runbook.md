# Azure DR Runbook

Restore and failover procedures for the [Azure production deployment](azure.md).
Read this before you need it — walk the [quarterly drill](#quarterly-dr-drill-checklist)
at least once so the commands below are proven against your own environment.

## Failure Modes

| Failure | Handling | Operator action |
| --- | --- | --- |
| Availability-zone loss | Automatic | None. Postgres Flexible fails over to its zone-redundant standby; AKS reschedules pods onto healthy-zone nodes; Weaviate/MinIO PVCs (`Premium_ZRS`) survive the zone. Zero-touch. |
| Region loss | Manual | Run the [region-loss restore procedure](#region-loss-restore-procedure) below. |
| Accidental data deletion (a table, a workspace, a bucket) | Manual | Point-in-time restore (PITR) into a scratch instance, then selectively recover — do not restore over the live primary. See [PITR procedure](#point-in-time-restore-accidental-deletion). |

## RPO / RTO by Component

| Component | Failure | RPO | RTO | Mechanism |
| --- | --- | --- | --- | --- |
| Postgres Flexible | Zone loss | 0 | Seconds–~2 min | Synchronous zone-redundant standby, automatic failover |
| Postgres Flexible | Region loss / PITR | ≤ 1 hour | ≤ 1 hour (restore time scales with DB size) | Geo-restore from geo-redundant backup, or `pg_geo_replica` standby if enabled |
| Cosmos DB for MongoDB | Zone loss | 0 | Seconds | HA replica set (when `enable_ha`) |
| Cosmos DB for MongoDB | Region loss / PITR | ≤ 1 hour | ≤ 2 hours | Continuous backup, point-in-time restore into a new account |
| Azure Cache for Redis | Any loss | Best-effort (async replication) | Minutes (Standard tier auto-failover) | Redis holds MQ Streams + rate-limit state, not a durable system of record — see [production hardening](production.md#5-fix-the-event-queue-eviction-policy) |
| Weaviate (vectors) | Zone loss | 0 | ~1–5 min | `Premium_ZRS` PVC is zone-redundant; AKS reschedules the pod |
| Weaviate (vectors) | Region loss | ≤ 24 hours (daily `weaviate-backup` CronJob, 03:00 UTC) — vectors are re-derivable from the source documents already durably stored in Postgres/Mongo/object storage, so a same-day RPO is the accepted posture for this specific, regenerable data class, not the system's ≤1h target | ≤ 3 hours (restore time scales with index size); if the last snapshot is unacceptably stale, re-ingesting from source documents is always a fallback, at the cost of ingestion-pipeline time instead of restore time | Restore the most recent `backup-azure` snapshot from the DR-region storage account's `weaviate-backups` container, or re-ingest |
| MinIO / document blobs | Zone loss | 0 | ~1–5 min | `Premium_ZRS` PVC is zone-redundant |
| MinIO / document blobs | Region loss | ≤ 1 hour | ≤ 2 hours (re-mirror time scales with data volume) | `minio-mirror` CronJob runs `rclone sync` hourly, MinIO → GRS Blob (`minio-mirror` container); re-mirror from that GRS replica in the DR region |
| Temporal (workflow state) | Zone/region loss | Follows Postgres (state lives in the `temporal`/`temporal_visibility` databases) | Schema re-setup Job + Postgres restore time | `temporal-sql-tool` schema setup re-run against restored Postgres |

**Object storage and Postgres are both inside the system-wide ≤1h RPO
target; Weaviate is the one documented exception**, at ≤24h with
re-ingestion as a fallback for anything even that misses — see the table
above. If you need a smaller Weaviate window, tighten `weaviate.backup.schedule`
(chart value, default `"0 3 * * *"`) before you need this runbook, not
during a region-loss event.

## Region-Loss Restore Procedure

Run these in order. `$RG`, `$LOCATION_DR` (default `centralus`),
`$RG_STATE`, and `$PROD_TFVARS` are your environment's actual values.
Postgres/Weaviate/MinIO all sit behind private endpoints when
`enable_private_endpoints` is on (the default) — every step below that talks
to one of those runs from *inside* the cluster (`kubectl exec`/`kubectl run`),
the same reachability constraint `scripts/deploy-azure.sh` documents for its
own bootstrap step, not from an arbitrary operator laptop.

### 1. Apply Terraform in the paired region

```bash
cd infra/azure

terraform init -backend-config=backend.hcl

# Two-phase apply (see docs/deploy/azure.md §4): module.aks first, its
# outputs configure the helm/kubernetes providers the full apply needs.
terraform apply \
  -var-file="$PROD_TFVARS" \
  -var="location=$LOCATION_DR" \
  -var="location_dr=eastus2" \
  -var="pg_geo_replica=false" \
  -target=module.aks

terraform apply \
  -var-file="$PROD_TFVARS" \
  -var="location=$LOCATION_DR" \
  -var="location_dr=eastus2" \
  -var="pg_geo_replica=false"
```

This stands up a fresh network/security/AKS/data/AI stack — and, via
`charts/inherent`, a fresh (empty) Weaviate/MinIO/Temporal — in the DR
region. It does **not** contain application data yet; that comes from the
restores below. If you keep `pg_geo_replica=true` in steady state, skip the
Postgres restore step and go straight to [step 6](#6-cut-over-dns) once the
standing replica promotes.

### 2. Restore Postgres (geo-restore)

```bash
az postgres flexible-server geo-restore \
  --resource-group "$RG" \
  --name inherent-pg-dr \
  --source-server "/subscriptions/<sub-id>/resourceGroups/<primary-rg>/providers/Microsoft.DBforPostgreSQL/flexibleServers/<primary-server-name>" \
  --location "$LOCATION_DR"
```

Point the `data` module's Postgres output at the restored server (or re-run
`terraform apply` with the restored server's connection details wired in, per
your state-import approach). Verify from inside the cluster (private
endpoint, per the note above) using the running `public-api` pod's own
already-resolved `DATABASE_URL`:

```bash
POD=$(kubectl get pods -n inherent -l app.kubernetes.io/component=public-api -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n inherent "$POD" -- sh -c 'psql "$DATABASE_URL" -c "select count(*) from processed_documents;"'
kubectl exec -n inherent "$POD" -- sh -c 'psql "$DATABASE_URL" -c "select count(*) from api_keys;"'
```

(`psql` itself needs to be on the image for this to work — if `public-api`'s
image doesn't carry it, run the same query via a short-lived
`postgres:16-alpine` pod instead, following the pattern
`scripts/deploy-azure.sh`'s `bootstrap_key()` uses: a temporary Secret
carrying `DATABASE_URL` + `envFrom`, never the URL as a literal argument.)

### 3. Restore Weaviate from `backup-azure` snapshots

Weaviate has no client sidecar (`deploy/weaviate-client` does not exist in
this chart) and its own image ships no shell/curl — trigger the restore the
same way `templates/weaviate/backup-cronjob.yaml` triggers a backup: from a
pod the chart's NetworkPolicy already allows to reach `weaviate:8080`. The
`public-api` pod already carries `WEAVIATE_API_KEY` in its env and Python
(`python:3.11-slim` base image), so `kubectl exec` into it rather than
standing up a new pod/label. (The outer shell quoting below is
double-quoted deliberately — Python's own strings inside are single-quoted
throughout, so nothing needs escaping across the two layers.)

```bash
POD=$(kubectl get pods -n inherent -l app.kubernetes.io/component=public-api -o jsonpath='{.items[0].metadata.name}')

# List available snapshots first (from the DR-region storage account):
az storage blob list --account-name <dr-storage-account> --container-name weaviate-backups -o table

kubectl exec -n inherent "$POD" -- python3 -c "
import json, os, urllib.request

backup_id = '<backup-id>'  # from the az storage blob list above
url = 'http://weaviate:8080/v1/backups/azure/' + backup_id + '/restore'
req = urllib.request.Request(
    url,
    data=json.dumps({'include': ['Document']}).encode(),
    method='POST',
    headers={
        'Authorization': 'Bearer ' + os.environ['WEAVIATE_API_KEY'],
        'Content-Type': 'application/json',
    },
)
print(urllib.request.urlopen(req).read().decode())
"

# Poll until status == 'SUCCESS':
kubectl exec -n inherent "$POD" -- python3 -c "
import json, os, urllib.request

backup_id = '<backup-id>'
url = 'http://weaviate:8080/v1/backups/azure/' + backup_id + '/restore'
req = urllib.request.Request(
    url,
    headers={'Authorization': 'Bearer ' + os.environ['WEAVIATE_API_KEY']},
)
print(json.loads(urllib.request.urlopen(req).read())['status'])
"
```

`<backup-id>` is the most recent successful snapshot in the DR-region
Storage Account's `weaviate-backups` container (`weaviate.backup.container`
in chart values) — the daily CronJob names them `scheduled-YYYYMMDD`.

### 4. Re-mirror MinIO from the Blob GRS replica

The mirror CronJob (`templates/minio/mirror-cronjob.yaml`) uses `rclone`,
not `mc` — Azure Blob has no S3-compatible endpoint for `mc mirror` to
target — against a Blob container named **`minio-mirror`** (chart value
`minio.mirror.azureBlob.container`, not `inherent-mirror`). Restore reuses
the exact same `rclone` remotes the CronJob already has configured via env,
reversing the sync direction, as a one-off pod carrying the CronJob's own
component label (already allowed MinIO ingress by NetworkPolicy):

```bash
S3_SECRET=$(kubectl get cronjob inh-minio-mirror -n inherent \
  -o jsonpath='{.spec.jobTemplate.spec.template.spec.containers[0].env[?(@.name=="RCLONE_CONFIG_MINIO_ACCESS_KEY_ID")].valueFrom.secretKeyRef.name}')
BLOB_SECRET=$(kubectl get cronjob inh-minio-mirror -n inherent \
  -o jsonpath='{.spec.jobTemplate.spec.template.spec.containers[0].env[?(@.name=="RCLONE_CONFIG_AZUREBLOB_CONNECTION_STRING")].valueFrom.secretKeyRef.name}')

kubectl run minio-restore --rm -i --restart=Never --quiet \
  --namespace inherent --image rclone/rclone:1.67 \
  --labels="app.kubernetes.io/component=minio-mirror" \
  --overrides="$(cat <<JSON
{
  "spec": {
    "containers": [{
      "name": "minio-restore",
      "image": "rclone/rclone:1.67",
      "env": [
        {"name": "RCLONE_CONFIG_MINIO_TYPE", "value": "s3"},
        {"name": "RCLONE_CONFIG_MINIO_PROVIDER", "value": "Minio"},
        {"name": "RCLONE_CONFIG_MINIO_ENDPOINT", "value": "http://minio:9000"},
        {"name": "RCLONE_CONFIG_MINIO_ACCESS_KEY_ID", "valueFrom": {"secretKeyRef": {"name": "$S3_SECRET", "key": "MINIO_ROOT_USER"}}},
        {"name": "RCLONE_CONFIG_MINIO_SECRET_ACCESS_KEY", "valueFrom": {"secretKeyRef": {"name": "$S3_SECRET", "key": "MINIO_ROOT_PASSWORD"}}},
        {"name": "RCLONE_CONFIG_AZUREBLOB_TYPE", "value": "azureblob"},
        {"name": "RCLONE_CONFIG_AZUREBLOB_CONNECTION_STRING", "valueFrom": {"secretKeyRef": {"name": "$BLOB_SECRET", "key": "AZURE_STORAGE_CONNECTION_STRING"}}}
      ],
      "command": ["rclone", "sync", "azureblob:minio-mirror", "minio:inherent-documents", "--checksum", "--transfers", "8"]
    }]
  }
}
JSON
)"
```

This re-populates the fresh MinIO StatefulSet in the DR region from whatever
the mirror CronJob last pushed (≤1h old) — see the RPO note above.

### 5. Re-set up Temporal schema

Reuse the exact commands `templates/temporal/schema-setup-job.yaml` runs as
this release's pre-install hook — same pinned
`temporalio/admin-tools:1.24.2-tctl-1.18.1-cli-1.0.0` image, same `v12`
schema path (not `v96` — the image's schema tree dropped that PG 9.6-era
directory long before 1.24), same `--tls` flags Azure PG Flexible's
`require_secure_transport=ON` requires:

```bash
PG_HOST=$(terraform -chdir=infra/azure output -raw pg_fqdn)
TEMPORAL_PG_SECRET="${RESOURCE_PREFIX:-inherent}-temporal-postgres" # kubernetes_secret.temporal_postgres

kubectl run temporal-schema-setup-dr --rm -i --restart=Never --quiet \
  --namespace inherent --image temporalio/admin-tools:1.24.2-tctl-1.18.1-cli-1.0.0 \
  --labels="app.kubernetes.io/component=temporal-schema-setup" \
  --overrides="$(cat <<JSON
{
  "spec": {
    "containers": [{
      "name": "temporal-schema-setup-dr",
      "image": "temporalio/admin-tools:1.24.2-tctl-1.18.1-cli-1.0.0",
      "env": [
        {"name": "POSTGRES_SEEDS", "value": "$PG_HOST"},
        {"name": "DB_PORT", "value": "5432"},
        {"name": "POSTGRES_USER", "value": "pgadmin"},
        {"name": "SQL_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "$TEMPORAL_PG_SECRET", "key": "POSTGRES_PASSWORD"}}}
      ],
      "command": ["/bin/sh", "-c", "set -e; SQL_TOOL=\"temporal-sql-tool --plugin postgres12 --endpoint \$POSTGRES_SEEDS --port \$DB_PORT --user \$POSTGRES_USER --tls --tls-enable-host-verification --tls-server-name \$POSTGRES_SEEDS\"; for db in temporal:temporal temporal_visibility:visibility; do name=\${db%%:*}; kind=\${db##*:}; \$SQL_TOOL --database \"\$name\" create-database || true; \$SQL_TOOL --database \"\$name\" setup-schema -v 0.0 || true; \$SQL_TOOL --database \"\$name\" update-schema -d \"/etc/temporal/schema/postgresql/v12/\$kind/versioned\"; done"]
    }]
  }
}
JSON
)"
```

`POSTGRES_USER` above (`pgadmin`) is `modules/data`'s hardcoded PG Flexible
Server admin login (`administrator_login` in `modules/data/main.tf`) — it is
not currently re-exported as a root Terraform output, so it's a fixed value
here rather than something to read back with `terraform output`.
`TEMPORAL_PG_SECRET`'s `POSTGRES_PASSWORD` key (mapped above to the
`SQL_PASSWORD` env var `temporal-sql-tool` reads) is the same admin password
`secrets.tf`'s `kubernetes_secret.temporal_postgres` materializes — see the
[security section](azure.md#7-enterprise-vnet-integration) for what that
secret handling means.

### 6. Cut over DNS

```bash
DR_LB_IP=$(kubectl get svc ingress-nginx-controller -n ingress-nginx \
  -o jsonpath='{.status.loadBalancer.ingress[0].ip}')

az network dns record-set a update \
  --resource-group "$RG" \
  --zone-name "$DNS_ZONE_NAME" \
  --name "$DNS_RECORD" \
  --set aRecords[0].ipv4Address="$DR_LB_IP"
```

TLS certificates re-issue automatically via cert-manager (Let's Encrypt)
against the new ingress IP once DNS propagates. `ingress_profile = "appgw_waf"`
is not a deployable ingress path today (see [docs/deploy/azure.md §10](azure.md#10-limitations-roadmap)),
so there is no AppGW WAF certificate binding to re-provision.

### 7. Verify

```bash
API_FQDN=$(terraform -chdir=infra/azure output -raw api_fqdn)
curl -s "https://${API_FQDN}/health"
curl -s "https://${API_FQDN}/health/ready"
```

Both must return `200` before declaring the region cutover complete. Run a
smoke upload/search cycle before pointing production traffic here.

## Point-in-Time Restore (Accidental Deletion)

Never restore over the live primary. Restore into a scratch resource, verify,
then selectively copy the needed rows/objects back.

```bash
# Postgres PITR into a scratch server, same region
az postgres flexible-server restore \
  --resource-group "$RG" \
  --name inherent-pg-scratch-restore \
  --source-server "<primary-server-resource-id>" \
  --restore-time "2026-08-30T09:15:00Z"

# Cosmos Mongo vCore PITR into a scratch cluster
az cosmosdb mongocluster restore \
  --resource-group "$RG" \
  --cluster-name inherent-cosmos-scratch-restore \
  --source-cluster-name "<primary-cluster-name>" \
  --point-in-time "2026-08-30T09:15:00Z"
```

Query the scratch restore for the deleted rows/documents, then write them
back into production with a scoped `INSERT`/`upsert` — never point the
application at the scratch restore directly (both restores above sit behind
a private endpoint like the primary; query them via `kubectl exec`/`kubectl run`
inside the cluster, the same reachability note as [step 2](#2-restore-postgres-geo-restore)
above). Tear the scratch resource down once recovery is confirmed.

## Quarterly DR Drill Checklist

Run every quarter. Time each step against the RPO/RTO table above and file
findings (including this doc) if reality has drifted from the numbers.

- [ ] Confirm RPO/RTO targets in this doc still match business requirements
- [ ] Trigger a Postgres PITR restore into a scratch resource group; verify row counts against a known-good query
- [ ] Trigger a Cosmos Mongo vCore PITR restore; verify workspace records are readable
- [ ] Trigger a Weaviate `backup-azure` restore into a scratch collection; verify object count and a sample query
- [ ] Run the `rclone sync` restore command (step 4 above) with `--dry-run` from the DR Blob replica into a scratch MinIO instance; verify object count matches production
- [ ] `terraform apply` the stack into a scratch state key in the DR region; confirm AKS, ingress, and the `migrate` Job come up cleanly, then `terraform destroy` it
- [ ] Re-run the Temporal schema-setup commands (step 5 above) against a scratch Postgres restore; confirm both databases update cleanly
- [ ] Dry-run the DNS cutover against a test subdomain, not the production record
- [ ] Record actual elapsed time for each step; compare against the RPO/RTO table and update it if drift is found
- [ ] Confirm this runbook's commands still match the current `infra/azure/modules/*` and `charts/inherent/templates/*` (module/chart renames or refactors since the last drill)
- [ ] Tear down every scratch resource created during the drill

## See Also

- [Deploy to Azure](azure.md) — architecture, tuning, TCO
- [Taking Inherent to Production](production.md) — backup guidance that applies to every deployment target
