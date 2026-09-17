terraform {
  required_version = ">= 1.9"

  # State is remote via the "azurerm" backend (partial config in backend.tf, filled in by
  # `terraform init -backend-config=backend.hcl` — see backend.hcl.example). The state file
  # contains every secret this root writes to Key Vault (postgres/cosmos/redis credentials,
  # API keys) — the backend storage account MUST be private + RBAC'd, same discipline as
  # ../backend.hcl.example for Hetzner.

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.16"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.33"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
  }
}
