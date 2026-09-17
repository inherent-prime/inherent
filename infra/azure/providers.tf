provider "azurerm" {
  features {
    key_vault {
      # Purge protection is set on the vault resource itself; this only controls what
      # `terraform destroy` does to *this run's* soft-deleted vault — recover, don't purge,
      # so an accidental destroy/apply cycle can still restore secrets within the retention
      # window (soft_delete_retention_days on the vault, modules/security).
      purge_soft_delete_on_destroy    = false
      recover_soft_deleted_key_vaults = true
    }
  }
}

# helm/kubernetes providers are wired from the aks module's outputs (per the spec's
# cross-module interface: cluster_name + host/client-cert via kube_config — the aks module
# exposes these as four discrete kube_config_* outputs rather than a list(object), so wire
# each field individually). `try()` guards every field so `terraform validate` on this root
# doesn't hard-fail while modules/aks is missing or mid-change in a given checkout — a real
# apply still needs modules/aks applied first (main.tf's module "apps" depends_on covers
# that ordering). azurerm returns these PEM values base64-encoded; both providers expect
# raw PEM, hence base64decode().
provider "helm" {
  kubernetes {
    host                   = try(module.aks.kube_config_host, "")
    client_certificate     = try(base64decode(module.aks.kube_config_client_certificate), "")
    client_key             = try(base64decode(module.aks.kube_config_client_key), "")
    cluster_ca_certificate = try(base64decode(module.aks.kube_config_cluster_ca_certificate), "")
  }
}

provider "kubernetes" {
  host                   = try(module.aks.kube_config_host, "")
  client_certificate     = try(base64decode(module.aks.kube_config_client_certificate), "")
  client_key             = try(base64decode(module.aks.kube_config_client_key), "")
  cluster_ca_certificate = try(base64decode(module.aks.kube_config_cluster_ca_certificate), "")
}
