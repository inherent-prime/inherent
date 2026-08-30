# Cross-module interface (azure-build-spec.md): identical shape whether this module created
# the VNet or reused an existing one via existing_vnet_id/existing_subnet_ids.

output "vnet_id" {
  description = "VNet resource ID (created here, or existing_vnet_id passed through)."
  value       = local.vnet_id
}

output "subnet_ids" {
  description = "Subnet IDs keyed \"aks\", \"data\", \"appgw\"."
  value       = local.subnet_ids
}

output "private_dns_zone_ids" {
  description = "Private DNS zone IDs keyed \"postgres\", \"redis\", \"cosmos\", \"vault\", \"blob\". Empty map when enable_private_endpoints = false."
  value       = { for k, z in azurerm_private_dns_zone.this : k => z.id }
}

output "nat_gateway_id" {
  description = "NAT gateway ID for the AKS subnet (null in BYO-VNet mode)."
  value       = local.create_vnet ? azurerm_nat_gateway.aks[0].id : null
}
