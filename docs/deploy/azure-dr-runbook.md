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
| Weaviate (vectors) | Region loss | = `backup-azure` snapshot interval (recommend hourly for ≤1h RPO) | ≤ 3 hours (restore time scales with index size) | Restore from `backup-azure` module snapshots in the DR-region storage account |
| MinIO / document blobs | Zone loss | 0 | ~1–5 min | `Premium_ZRS` PVC is zone-redundant |
| MinIO / document blobs | Region loss | **Up to 24 hours by default** — bounded by the `minio-mirror` CronJob schedule, not the 1h system target | ≤ 2 hours (re-mirror time scales with data volume) | Re-mirror from the GRS Blob replica in the DR region |
| Temporal (workflow state) | Zone/region loss | Follows Postgres (state lives in the `temporal`/`temporal_visibility` databases) | Schema re-setup Job + Postgres restore time | `temporalio/admin-tools` schema-setup Job re-run against restored Postgres |

**Object storage is the floor on system-wide RPO.** The system target is
RPO ≤ 1h / RTO ≤ 4h, and the MinIO mirror CronJob runs hourly to stay
inside it — an object blob written since the last mirror run is not yet in
the DR-region Blob replica, so the worst case is ~1 h of object loss. If
you need a smaller window, tighten the mirror CronJob's schedule (chart
value `minio.mirror.schedule`) before you need this runbook, not during a
region-loss event.

## Region-Loss Restore Procedure

Run these in order. `$RG`, `$LOCATION_DR` (default `centralus`),
`$RG_STATE`, and `$PROD_TFVARS` are your environment's actual values.

### 1. Apply Terraform in the paired region

```bash
cd infra/azure

terraform init -backend-config=backend.hcl

terraform apply \
  -var-file="$PROD_TFVARS" \
  -var="location=$LOCATION_DR" \
  -var="location_dr=eastus2" \
  -var="pg_geo_replica=false"
```

This stands up a fresh network/security/AKS/data/AI stack in the DR region.
It does **not** contain application data yet — that comes from the restores
below. If you keep `pg_geo_replica=true` in steady state, skip the Postgres
restore step and go straight to [step 6](#6-cut-over-dns) once the standing
replica promotes.

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
your state-import approach). Verify:

```bash
psql "$DATABASE_URL" -c "select count(*) from processed_documents;"
psql "$DATABASE_URL" -c "select count(*) from api_keys;"
```

### 3. Restore Weaviate from `backup-azure` snapshots

```bash
kubectl exec -n inherent deploy/weaviate-client -- \
  curl -s -X POST http://weaviate:8080/v1/backups/azure/<backup-id>/restore \
  -H "Content-Type: application/json" \
  -d '{"include": ["Document"]}'

# Poll until status = SUCCESS
kubectl exec -n inherent deploy/weaviate-client -- \
  curl -s http://weaviate:8080/v1/backups/azure/<backup-id>/restore
```

`<backup-id>` is the most recent successful snapshot in the DR-region Storage
Account's `weaviate-backups` container — list them with
`az storage blob list --account-name <dr-storage-account> --container-name weaviate-backups`.

### 4. Re-mirror MinIO from the Blob GRS replica

```bash
mc alias set drblob "https://<dr-storage-account>.blob.core.windows.net" \
  "$AZURE_STORAGE_ACCOUNT_KEY" ""
mc alias set minio-dr "http://minio.inherent.svc.cluster.local:9000" \
  "$MINIO_ACCESS_KEY" "$MINIO_SECRET_KEY"

mc mirror --overwrite drblob/inherent-mirror minio-dr/inherent-documents
```

This re-populates the fresh MinIO StatefulSet in the DR region from whatever
the mirror CronJob last pushed — see the RPO caveat above.

### 5. Re-set up Temporal schema

```bash
kubectl apply -f - <<'EOF'
apiVersion: batch/v1
kind: Job
metadata:
  name: temporal-schema-setup-dr
  namespace: inherent
spec:
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: schema-setup
          image: temporalio/admin-tools:latest
          command: ["tctl", "admin", "cluster", "add-search-attributes"]
          # plus temporal-sql-tool create-database / update-schema against
          # the restored `temporal` and `temporal_visibility` databases,
          # then register the default + audit namespaces (see the network
          # build's init Job for the exact command sequence).
EOF

kubectl wait --for=condition=complete job/temporal-schema-setup-dr -n inherent --timeout=10m
```

### 6. Cut over DNS

```bash
az network dns record-set a update \
  --resource-group "$RG" \
  --zone-name "$DNS_ZONE_NAME" \
  --name "$DNS_RECORD" \
  --set aRecords[0].ipv4Address="$(terraform output -raw api_fqdn_ip)"
```

TLS certificates re-issue automatically via cert-manager (Let's Encrypt)
against the new ingress IP once DNS propagates, or re-provision the AppGW WAF
certificate binding if `ingress_profile = "appgw_waf"`.

### 7. Verify

```bash
curl -s "https://$DNS_RECORD/health"
curl -s "https://$DNS_RECORD/health/ready"
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
application at the scratch restore directly. Tear the scratch resource down
once recovery is confirmed.

## Quarterly DR Drill Checklist

Run every quarter. Time each step against the RPO/RTO table above and file
findings (including this doc) if reality has drifted from the numbers.

- [ ] Confirm RPO/RTO targets in this doc still match business requirements
- [ ] Trigger a Postgres PITR restore into a scratch resource group; verify row counts against a known-good query
- [ ] Trigger a Cosmos Mongo vCore PITR restore; verify workspace records are readable
- [ ] Trigger a Weaviate `backup-azure` restore into a scratch collection; verify object count and a sample query
- [ ] Run `mc mirror --overwrite --dry-run` from the DR Blob replica into a scratch MinIO instance; verify object count matches production
- [ ] `terraform apply` the stack into a scratch state key in the DR region; confirm AKS, ingress, and the `migrate` Job come up cleanly, then `terraform destroy` it
- [ ] Re-run the Temporal schema-setup Job against a scratch Postgres restore; confirm both namespaces register
- [ ] Dry-run the DNS cutover against a test subdomain, not the production record
- [ ] Record actual elapsed time for each step; compare against the RPO/RTO table and update it if drift is found
- [ ] Confirm this runbook's commands still match the current `infra/azure/modules/*` (module renames/refactors since the last drill)
- [ ] Tear down every scratch resource created during the drill

## See Also

- [Deploy to Azure](azure.md) — architecture, tuning, TCO
- [Taking Inherent to Production](production.md) — backup guidance that applies to every deployment target
