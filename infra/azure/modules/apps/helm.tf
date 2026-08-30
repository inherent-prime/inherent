# charts/inherent helm_release — path is relative to THIS module's directory
# (infra/azure/modules/apps), computed per the build spec:
#   apps -> modules -> azure -> infra -> repo root -> charts/inherent
#   "${path.module}/../../../../charts/inherent"

locals {
  chart_values = {
    image = {
      registry = var.image_registry
      tag      = var.inherent_version
    }
    namespace = kubernetes_namespace.inherent.metadata[0].name
    serviceAccount = {
      annotations = {
        "azure.workload.identity/client-id" = var.workload_identity_client_id
      }
    }
    publicApi = {
      replicas = {
        min = var.api_replicas_min
        max = var.api_replicas_max
      }
      env = {
        # List, matching the chart's schema — the app does exact-IP matching (no CIDR
        # support), so a CIDR like var.aks_pod_cidr can never be a valid entry here; empty
        # list is the chart default (values.yaml documents the resulting rate-limiting
        # caveat behind a shared-IP ingress). See networkPolicy.podCidr below for where
        # aks_pod_cidr actually gets used.
        trustedProxies = []
      }
    }
    ingestion = {
      replicas = var.worker_replicas
    }
    weaviate = {
      persistence = {
        sizeGi                = var.weaviate_disk_gb
        createZrsStorageClass = var.enable_ha
      }
      # NOTE: weaviate keeps its own createZrsStorageClass key (chart-owned name, predates
      # this coordination) — minio.persistence below uses the current shared schema name
      # useZrsClass for the same gate. Both mean "storage-class-level ZRS", just spelled
      # differently per the chart's schema at the time each was written.
      backup = {
        enabled   = var.enable_dr
        container = var.weaviate_backup_container
        connectionStringSecretRef = {
          name = var.enable_dr ? kubernetes_secret.backup_blob[0].metadata[0].name : ""
          key  = "AZURE_STORAGE_CONNECTION_STRING"
        }
      }
    }
    minio = {
      persistence = {
        sizeGi = var.minio_disk_gb
        # Same enable_ha gate as weaviate.persistence.createZrsStorageClass above — both
        # point at the one shared templates/storageclass.yaml, rendered once.
        useZrsClass = var.enable_ha
      }
      mirror = {
        enabled = var.enable_dr
        azureBlob = {
          container = var.minio_mirror_container
          connectionStringSecretRef = {
            name = var.enable_dr ? kubernetes_secret.backup_blob[0].metadata[0].name : ""
            key  = "AZURE_STORAGE_CONNECTION_STRING"
          }
        }
      }
    }
    temporal = {
      server = {
        # Always 1, regardless of enable_ha: temporalio/server's ringpop gossip membership
        # (TCP 6933-6939 between replicas) is not wired through the chart's NetworkPolicy,
        # so a 2nd replica would boot isolated rather than joining one cluster — see
        # charts/inherent/values.yaml's temporal.server.replicas comment. HA for Temporal
        # comes from AKS rescheduling a killed pod (workflow state lives in PG, not in the
        # process) rather than from a multi-replica ringpop cluster.
        replicas = 1
      }
      postgres = {
        host = var.pg_fqdn
        user = var.pg_admin_user
        passwordSecretRef = {
          name = kubernetes_secret.temporal_postgres.metadata[0].name
          key  = "POSTGRES_PASSWORD"
        }
      }
    }
    embedding = merge(
      {
        profile = var.embedding_profile
        # TEI's CPU deployment (charts/inherent) is pinned to a 384-dim model — only
        # openai_embedding_dim (var, default 1536 for text-embedding-3-small) applies to
        # the azure_openai profile; hardcoding 384 for tei keeps the two profiles from
        # silently sharing a dimension that only happens to be right for one of them.
        dim = var.embedding_profile == "tei" ? 384 : var.openai_embedding_dim
      },
      var.embedding_profile == "azure_openai" ? {
        serviceUrl = var.openai_endpoint
        modelId    = var.openai_embedding_deployment_name
      } : {}
    )
    secrets = merge(
      {
        postgres  = { name = kubernetes_secret.postgres.metadata[0].name }
        mongodb   = { name = kubernetes_secret.mongodb.metadata[0].name }
        weaviate  = { name = kubernetes_secret.weaviate.metadata[0].name }
        redis     = { name = kubernetes_secret.redis.metadata[0].name }
        s3        = { name = kubernetes_secret.s3.metadata[0].name }
        ingestion = { name = kubernetes_secret.ingestion.metadata[0].name }
      },
      var.embedding_profile == "azure_openai" ? {
        embeddingApiKey = { name = kubernetes_secret.embedding_api_key[0].metadata[0].name }
      } : {}
    )
    networkPolicy = {
      # AKS CNI Overlay pod/service address spaces (modules/aks) — the chart's
      # NetworkPolicy egress rules use these instead of 0.0.0.0/0 so the netpol posture
      # actually restricts anything.
      podCidr     = var.aks_pod_cidr
      serviceCidr = var.aks_service_cidr
    }
  }
}

resource "helm_release" "inherent" {
  name              = "inherent"
  namespace         = kubernetes_namespace.inherent.metadata[0].name
  create_namespace  = false
  chart             = "${path.module}/../../../../charts/inherent"
  wait              = true
  timeout           = 900
  dependency_update = false

  # Rendered once as a single values blob rather than a long chain of `set`
  # blocks — every kubernetes_secret.*.metadata[0].name reference above
  # makes this helm_release implicitly depend on all of secrets.tf, so the
  # chart is never installed before its Secrets exist.
  values = [yamlencode(local.chart_values)]
}
