# Taking Inherent to Production

The published stack (`docker-compose.release.yml`) is a **zero-setup demo**: it
runs entirely from published images with bundled databases so you can evaluate
Inherent in minutes. Those defaults — an in-container S3 mock, an unauthenticated
Mongo, `ENVIRONMENT: development` — are correct for a demo and wrong for real
users and real data.

This page lists what to change before you expose Inherent to production traffic.
Every item configures behavior that already exists in the code; nothing here is
future or hosted-only.

**Golden rule:** only `inh-public-api-svc` should be internet-facing. Keep every
datastore on an internal network (the release stack already binds them to
loopback).

## Pre-flight Checklist

- [ ] Strong secrets set: `POSTGRES_PASSWORD`, `WEAVIATE_API_KEY`, `INGESTION_API_KEY`
- [ ] Seeded `ink_dev_local_key_001` **not** reused — your own `ink_` key created
- [ ] `ENVIRONMENT=production` on the public API
- [ ] Real object storage configured; `s3rver` removed
- [ ] MongoDB authentication enabled
- [ ] Valkey eviction policy changed from `allkeys-lru` to `noeviction`
- [ ] Temporal running on a managed/provisioned cluster (not `auto-setup`)
- [ ] TLS terminated by a reverse proxy in front of the public API
- [ ] Datastores kept on the internal network only
- [ ] Backups scheduled for Postgres, MongoDB, Weaviate, and object storage

---

## 1. Replace every dev secret

The release stack already refuses to boot with default values for
`POSTGRES_PASSWORD`, `WEAVIATE_API_KEY`, and `INGESTION_API_KEY` (the `:?` guards
in `docker-compose.release.yml`). Set strong, unique values for all three.

Do **not** reuse the seeded application API key `ink_dev_local_key_001` or the
dev ingestion key. Create your own — see [§8](#8-provision-workspaces-and-api-keys).

## 2. Set `ENVIRONMENT=production`

The release stack ships `ENVIRONMENT: development` on the public API
(`docker-compose.release.yml:279`). This flag is not cosmetic — it gates real
security posture:

| Behavior | development | production |
| --- | --- | --- |
| `/docs` and `/redoc` OpenAPI UIs | served | disabled (`main.py:63-64`) |
| HSTS response header | off | on (`middleware/security_headers.py:32`) |
| Error responses | include internal detail | detail suppressed (`middleware/error_handler.py:135,246`) |
| Logs | human-readable | JSON (`main.py:35`) |

Set `ENVIRONMENT=production` before exposing the API to anything real.

## 3. Use real object storage

The demo stores document blobs in `s3rver`, a Node-based S3 mock, with
credentials defaulting to `S3RVER`. Replace it with real S3-compatible storage.

The application supports `s3`, `gcs`, and `azure` backends
(`services/inh-ingestion-svc/src/temporal/models.py`). To switch:

1. Remove the `s3rver` service and the `depends_on: s3rver` entries from your
   compose file.
2. In `docker-compose.release.yml`, `AWS_S3_ENDPOINT` is **hardcoded** to
   `http://s3rver:9000` (lines 229, 287) rather than read from an env var, so
   you must edit those lines to point at your provider (or omit them entirely for
   AWS S3).
3. Set real credentials and target:

   ```bash
   STORAGE_BACKEND=s3
   AWS_S3_ENDPOINT=https://s3.us-east-1.amazonaws.com   # or your provider
   AWS_ACCESS_KEY_ID=<real>
   AWS_SECRET_ACCESS_KEY=<real>
   AWS_S3_BUCKET=<your-bucket>
   AWS_REGION=<your-region>
   ```

## 4. Enable MongoDB authentication

The bundled Mongo runs with no authentication, and the connection strings carry
no credentials (`MONGODB_URI: mongodb://mongodb:27017`). Enable auth:

1. Set root credentials on the Mongo service:

   ```yaml
   environment:
     MONGO_INITDB_ROOT_USERNAME: <user>
     MONGO_INITDB_ROOT_PASSWORD: <password>
   ```

2. Put the credentials in `MONGODB_URI` for **both** services:

   ```bash
   MONGODB_URI=mongodb://<user>:<password>@mongodb:27017/main?authSource=admin
   ```

## 5. Fix the event-queue eviction policy

The MQ runs on Valkey (Redis) Streams. The demo starts Valkey with
`--maxmemory-policy allkeys-lru` (`docker-compose.release.yml:118`). Streams are
keys, so under memory pressure LRU eviction can **silently drop undelivered
upload events**, causing documents to never be ingested.

Change the policy so the durable queue is never evicted:

```bash
valkey-server --appendonly yes --maxmemory 512mb --maxmemory-policy noeviction
```

Raise `maxmemory` to fit your throughput, or run a dedicated Valkey instance for
the queue separate from any cache use. Monitor memory headroom.

## 6. Run Temporal on a real cluster

The demo uses `temporalio/auto-setup` (`docker-compose.release.yml:174`), which
upstream documents as **not for production** — it auto-provisions schema on
startup. Use a properly provisioned Temporal cluster (managed schema) or Temporal
Cloud, and point the services at it:

```bash
TEMPORAL_ENABLED=true
TEMPORAL_HOST=<your-temporal-host>:7233
TEMPORAL_NAMESPACE=<your-namespace>
TEMPORAL_TASK_QUEUE=document-ingestion
```

## 7. Terminate TLS at a reverse proxy

Nothing in the stack terminates TLS. Put a reverse proxy (Caddy, Traefik, or
nginx) in front of `inh-public-api-svc` (container port `8080`), terminate HTTPS
there, and forward to the service over the internal network.

Do **not** publish Postgres, MongoDB, Weaviate, Valkey, s3rver, or Temporal on a
public interface. The release stack already binds them to `127.0.0.1`; on a
multi-host deployment, keep them on a private network reachable only by the two
services.

## 8. Provision workspaces and API keys

Inherent has **no key-management REST API** today — application keys are stored
as an SHA-256 hash in the PostgreSQL `api_keys` table plus a workspace record in
MongoDB. The `bootstrap.sh` script creates both. Run it with your own values
instead of the seeded defaults:

```bash
API_KEY=ink_<your-strong-key> WORKSPACE_ID=<your-workspace> \
PG_CONTAINER=inherent-oss-postgres MONGO_CONTAINER=inherent-oss-mongodb \
  bash bootstrap.sh
```

Application keys must start with `ink_` (any other prefix is rejected). Rotate
the `INGESTION_API_KEY` on the same schedule as your other secrets.

> Programmatic key/workspace management (create, list, revoke via API) does not
> exist yet — provisioning is script- or SQL-driven. Track this before you need
> self-service key rotation.

## 9. Back up your data

`make clean` only destroys data; there is no backup tooling in the repo. Schedule
snapshots for every stateful store:

| Store | Holds | Losing it means |
| --- | --- | --- |
| PostgreSQL | Document metadata, `api_keys`, Temporal state | Lost auth + metadata; re-ingest required |
| MongoDB | Workspace records, raw documents | Lost control-plane + source docs |
| Weaviate | Vector embeddings | Rebuildable by re-ingesting from source |
| Object storage | Uploaded file blobs | Lost source documents — unrecoverable |

Weaviate is the only store you can fully rebuild (re-embed from the originals),
and only if the object storage blobs survive. Protect object storage and Postgres
first.

## 10. (Optional) Point at managed dependencies

For a hosted deployment, swap the bundled containers for managed services via
env. `.env.example` includes commented Cloud SQL connector settings
(`USE_CLOUD_SQL_CONNECTOR`, `CLOUD_SQL_INSTANCE`, …). The same pattern applies to
managed Weaviate, managed Mongo, real S3, and Temporal Cloud — set the relevant
connection env vars and drop the corresponding bundled service from your compose
file.

---

## See Also

- [Getting Started Locally](../getting-started/local.md) — the demo stack this
  page hardens
- [Request Examples](../examples/README.md) — endpoint-by-endpoint API reference
- [ADR 0002 — Weaviate multi-tenancy](../adr/0002-weaviate-multi-tenancy-scale.md)
  — how workspace isolation is enforced
