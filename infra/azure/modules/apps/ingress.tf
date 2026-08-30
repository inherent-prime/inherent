# Ingress — issue #323. Two mutually exclusive paths selected by
# var.ingress_profile:
#   "nginx"      -> ingress-nginx + cert-manager (Let's Encrypt) + a plain
#                   kubernetes_ingress_v1 for api_hostname.
#   "appgw_waf"  -> azurerm_application_gateway (WAF_v2, OWASP 3.2) + AGIC.
#                   AGIC is a CLUSTER ADD-ON (modules/aks's
#                   ingress_application_gateway block, var appgw_id) — this
#                   module only creates the gateway resource itself and the
#                   Ingress object AGIC watches; it does NOT install AGIC.
#                   IMPORTANT for the integrator: root main.tf must wire
#                   module.aks.appgw_id = module.apps.appgw_id. This does
#                   NOT create a module dependency cycle — the App Gateway
#                   resource below has no dependency on anything from
#                   modules/aks, only the reverse (aks -> apps for this id,
#                   apps' helm_release/kubernetes_ingress_v1 -> aks for the
#                   kubernetes/helm provider). See PR description / final
#                   report for the full reasoning.

# ---------------------------------------------------------------------
# nginx profile
# ---------------------------------------------------------------------

resource "helm_release" "ingress_nginx" {
  count = var.ingress_profile == "nginx" ? 1 : 0

  name             = "ingress-nginx"
  namespace        = "ingress-nginx"
  create_namespace = true
  repository       = "https://kubernetes.github.io/ingress-nginx"
  chart            = "ingress-nginx"
  version          = var.ingress_nginx_chart_version # pinned
  wait             = true
  timeout          = 600

  values = [yamlencode({
    controller = {
      service = {
        annotations = {
          # AKS/Azure LB health probe — without this the platform LB's
          # default TCP probe against the wrong port can flap the backend
          # pool under rolling updates.
          "service.beta.kubernetes.io/azure-load-balancer-health-probe-request-path" = "/healthz"
        }
      }
    }
  })]
}

resource "helm_release" "cert_manager" {
  count = var.ingress_profile == "nginx" ? 1 : 0

  name             = "cert-manager"
  namespace        = "cert-manager"
  create_namespace = true
  repository       = "https://charts.jetstack.io"
  chart            = "cert-manager"
  version          = var.cert_manager_chart_version # pinned
  wait             = true
  timeout          = 600

  set {
    name  = "crds.enabled"
    value = "true"
  }
}

resource "kubernetes_manifest" "letsencrypt_cluster_issuer" {
  count = var.ingress_profile == "nginx" ? 1 : 0

  manifest = {
    apiVersion = "cert-manager.io/v1"
    kind       = "ClusterIssuer"
    metadata = {
      name = "letsencrypt"
    }
    spec = {
      acme = {
        server = "https://acme-v02.api.letsencrypt.org/directory"
        email  = var.letsencrypt_email
        privateKeySecretRef = {
          name = "letsencrypt-account-key"
        }
        solvers = [{
          http01 = {
            ingress = {
              ingressClassName = "nginx"
            }
          }
        }]
      }
    }
  }

  depends_on = [helm_release.cert_manager]
}

resource "kubernetes_ingress_v1" "api_nginx" {
  count = var.ingress_profile == "nginx" ? 1 : 0

  metadata {
    name      = "inh-public-api"
    namespace = kubernetes_namespace.inherent.metadata[0].name
    annotations = {
      "cert-manager.io/cluster-issuer" = "letsencrypt"
    }
  }

  spec {
    ingress_class_name = "nginx"

    tls {
      hosts       = [var.api_hostname]
      secret_name = "inh-public-api-tls"
    }

    rule {
      host = var.api_hostname
      http {
        path {
          path      = "/"
          path_type = "Prefix"
          backend {
            service {
              name = "inh-public-api"
              port {
                number = 8080
              }
            }
          }
        }
      }
    }
  }

  depends_on = [
    helm_release.ingress_nginx,
    kubernetes_manifest.letsencrypt_cluster_issuer,
    helm_release.inherent,
  ]
}

# ---------------------------------------------------------------------
# appgw_waf profile
# ---------------------------------------------------------------------

resource "azurerm_public_ip" "appgw" {
  count = var.ingress_profile == "appgw_waf" ? 1 : 0

  name                = "${var.resource_prefix}-${var.environment}-appgw-pip"
  resource_group_name = var.resource_group_name
  location            = var.location
  allocation_method   = "Static"
  sku                 = "Standard" # required for zone-redundant / WAF_v2 gateways
  tags                = var.tags
}

resource "azurerm_application_gateway" "this" {
  count = var.ingress_profile == "appgw_waf" ? 1 : 0

  name                = "${var.resource_prefix}-${var.environment}-appgw"
  resource_group_name = var.resource_group_name
  location            = var.location
  tags                = var.tags

  sku {
    name = "WAF_v2"
    tier = "WAF_v2"
  }

  autoscale_configuration {
    min_capacity = 2
    max_capacity = 10
  }

  gateway_ip_configuration {
    name      = "appgw-ip-config"
    subnet_id = var.appgw_subnet_id
  }

  frontend_ip_configuration {
    name                 = "appgw-frontend-ip"
    public_ip_address_id = azurerm_public_ip.appgw[0].id
  }

  frontend_port {
    name = "port-443"
    port = 443
  }

  frontend_port {
    name = "port-80"
    port = 80
  }

  # Placeholder backend wiring — AGIC (the AKS add-on, modules/aks's
  # ingress_application_gateway block) rewrites all of the below to route to
  # inh-public-api's pod IPs based on the Ingress object AGIC watches
  # (kubernetes_ingress_v1.api_appgw). Terraform still needs a
  # syntactically-complete gateway to create the resource at all — these
  # values are never actually used once AGIC takes over.
  backend_address_pool {
    name = "placeholder-pool"
  }

  backend_http_settings {
    name                  = "placeholder-http-settings"
    cookie_based_affinity = "Disabled"
    port                  = 80
    protocol              = "Http"
    request_timeout       = 30
  }

  http_listener {
    name                           = "placeholder-listener"
    frontend_ip_configuration_name = "appgw-frontend-ip"
    frontend_port_name             = "port-80"
    protocol                       = "Http"
  }

  request_routing_rule {
    name                       = "placeholder-rule"
    rule_type                  = "Basic"
    http_listener_name         = "placeholder-listener"
    backend_address_pool_name  = "placeholder-pool"
    backend_http_settings_name = "placeholder-http-settings"
    priority                   = 100
  }

  waf_configuration {
    enabled          = true
    firewall_mode    = "Prevention"
    rule_set_type    = "OWASP"
    rule_set_version = "3.2"
  }

  lifecycle {
    ignore_changes = [
      # AGIC rewrites these post-create to route real traffic — don't fight
      # it on every apply.
      backend_address_pool,
      backend_http_settings,
      http_listener,
      request_routing_rule,
      frontend_port,
      probe,
      url_path_map,
      redirect_configuration,
      ssl_certificate,
    ]
  }
}

resource "kubernetes_ingress_v1" "api_appgw" {
  count = var.ingress_profile == "appgw_waf" ? 1 : 0

  metadata {
    name      = "inh-public-api"
    namespace = kubernetes_namespace.inherent.metadata[0].name
    annotations = {
      "kubernetes.io/ingress.class"                  = "azure/application-gateway"
      "appgw.ingress.kubernetes.io/backend-protocol" = "http"
    }
  }

  spec {
    rule {
      host = var.api_hostname
      http {
        path {
          path      = "/"
          path_type = "Prefix"
          backend {
            service {
              name = "inh-public-api"
              port {
                number = 8080
              }
            }
          }
        }
      }
    }
    # NOTE: no `tls` block — AGIC does not automate certificate issuance the
    # way cert-manager does for the nginx path. Terminate TLS at the gateway
    # via a pre-provisioned certificate (Key Vault-backed
    # appgw-ssl-certificate, wired through AGIC annotations) — documented as
    # a manual step in docs/deploy/azure.md for the appgw_waf profile, not
    # automated by this module.
  }

  depends_on = [helm_release.inherent]
}
