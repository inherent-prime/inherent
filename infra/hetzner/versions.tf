terraform {
  required_version = ">= 1.5.0"

  # .terraform.lock.hcl = provider dependency lock (committed to git).
  # State (*.tfstate) is remote via Hetzner Object Storage (S3-compatible)
  # — never commit state files.
  #
  # All paths use Hetzner Object Storage (S3-compatible) via partial backend "s3".
  # Prod / long-lived: backend.hcl stable key (e.g. inherent/prod/...), AWS_* env,
  #   then: terraform init -backend-config=backend.hcl
  # Laptop test:       backend.hcl key e.g. inherent/local/laptop/terraform.tfstate
  #   (see docs/getting-started/local-vm-test.md). Never prod or CI keys.
  # CI e2e:            backend-ci.hcl with inherent/ci/<run_id>/terraform.tfstate
  #   Never the prod key.
  backend "s3" {}

  required_providers {
    hcloud = {
      source  = "hetznercloud/hcloud"
      version = "~> 1.45"
    }
  }
}
