variable "server_name" {
  description = "Name of the Hetzner server"
  type        = string
  default     = "inherent-prod"
}

variable "server_type" {
  description = "Hetzner server type (e.g., cpx22, cpx32)"
  type        = string
  default     = "cpx22"
}

variable "server_image" {
  description = "OS image for the server"
  type        = string
  default     = "ubuntu-24.04"
}

variable "server_location" {
  description = "Hetzner datacenter location"
  type        = string
  default     = "fsn1"
}

variable "server_backups" {
  description = "Enable automatic backups"
  type        = bool
  default     = false
}

variable "ssh_public_key_path" {
  description = "Path to the SSH public key file"
  type        = string
  default     = "~/.ssh/id_ed25519.pub"
}

variable "ssh_key_name" {
  description = "Name for the SSH key resource in Hetzner"
  type        = string
  default     = "inherent-prod-key"
}

variable "inherent_version" {
  description = "Version of Inherent to deploy (Docker image tag)"
  type        = string
  default     = "latest"
}

# CI should pass the release tag; local/prod default main.
variable "compose_git_ref" {
  description = "Git ref (branch, tag, or SHA) used to download docker-compose.release.yml on the VM"
  type        = string
  default     = "main"
}

variable "env_file_content" {
  description = "Content of the .env file for Docker Compose. Use to inject production secrets at apply time. If empty, safe defaults are used."
  type        = string
  default     = ""
  sensitive   = true
}

# CIDR allowlists for the Hetzner firewall (only barrier for Docker-published ports).
variable "ssh_allowed_ips" {
  description = "Source CIDRs allowed to SSH (port 22)"
  type        = list(string)
  default     = ["0.0.0.0/0", "::/0"]
}

variable "api_allowed_ips" {
  description = "Source CIDRs allowed to the Public API (port 18000)"
  type        = list(string)
  default     = ["0.0.0.0/0", "::/0"]
}

# Server label (e.g. production, ci). CI e2e passes environment=ci.
variable "environment" {
  description = "Environment label applied to the Hetzner server"
  type        = string
  default     = "production"
}
