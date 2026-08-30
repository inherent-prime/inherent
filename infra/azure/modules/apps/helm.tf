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
        trustedProxies = var.aks_pod_cidr
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
        # 2 replicas when enable_ha (root var) — see charts/inherent
        # templates/temporal/deployment.yaml comment on why splitting
        # frontend/history/matching/worker isn't done at this scale.
        replicas = var.enable_ha ? 2 : 1
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
        dim     = var.openai_embedding_dim
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
      # Tighten from the chart's 0.0.0.0/0 default once the network module's
      # private-endpoint/data subnet CIDR is known at this call site — left
      # as the chart default here; wire a var + pass it through if the
      # integrator wants this tightened at apply time.
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
