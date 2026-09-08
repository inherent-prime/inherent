output "server_ipv4" {
  description = "Public IPv4 address"
  value       = hcloud_server.default.ipv4_address
}

output "server_ipv6" {
  description = "Public IPv6 address"
  value       = hcloud_server.default.ipv6_address
}

output "server_id" {
  description = "Hetzner server ID"
  value       = hcloud_server.default.id
}

output "server_name" {
  description = "Server name"
  value       = hcloud_server.default.name
}
