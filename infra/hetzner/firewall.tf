# Firewall protecting the Inherent server.
# This is the only network barrier for Docker-published ports (SSH, Public API).
# Restrict ssh_allowed_ips / api_allowed_ips in production; defaults are open.
resource "hcloud_firewall" "default" {
  name = "${var.server_name}-firewall"

  rule {
    direction   = "in"
    protocol    = "tcp"
    source_ips  = var.ssh_allowed_ips
    port        = "22"
    description = "SSH"
  }

  rule {
    direction   = "in"
    protocol    = "tcp"
    source_ips  = var.api_allowed_ips
    port        = "18000"
    description = "Inherent Public API"
  }

  rule {
    direction   = "in"
    protocol    = "icmp"
    source_ips  = ["0.0.0.0/0", "::/0"]
    description = "ICMP (ping)"
  }
}

# Attach the firewall to the server
resource "hcloud_firewall_attachment" "default" {
  firewall_id = hcloud_firewall.default.id
  server_ids  = [hcloud_server.default.id]
}
