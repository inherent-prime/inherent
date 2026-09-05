# Issue #321 — network foundation: VNet, subnets, NSGs (deny-inbound-by-default),
# private DNS zones for the data-layer privatelink names, NAT gateway for AKS egress.
#
# BYO-VNet mode (existing_vnet_id set): skip creating VNet/subnets/NSGs/NAT gateway and
# reuse the caller's existing_subnet_ids. Private DNS zones are still created here — they
# are logical, low-blast-radius resources an enterprise VNet owner rarely pre-provisions
# with these exact privatelink names, and every module downstream expects the same
# private_dns_zone_ids output shape regardless of who owns the VNet.
locals {
  create_vnet = var.existing_vnet_id == ""

  name_prefix = "${var.resource_prefix}-${var.environment}"

  # privatelink zone names are fixed by Azure per service — not user-configurable.
  private_dns_zone_names = {
    postgres = "privatelink.postgres.database.azure.com"
    redis    = "privatelink.redis.cache.windows.net"
    cosmos   = "privatelink.mongocluster.cosmos.azure.com"
    vault    = "privatelink.vaultcore.azure.net"
    blob     = "privatelink.blob.core.windows.net"
  }
}

resource "azurerm_virtual_network" "this" {
  count = local.create_vnet ? 1 : 0

  name                = "${local.name_prefix}-vnet"
  resource_group_name = var.resource_group_name
  location            = var.location
  address_space       = [var.vnet_cidr]
  tags                = var.tags
}

# AKS node subnet. NAT gateway attached below for stable, node-count-independent egress
# (avoids SNAT port exhaustion from per-node public IPs / LB outbound rules at scale).
resource "azurerm_subnet" "aks" {
  count = local.create_vnet ? 1 : 0

  name                 = "${local.name_prefix}-aks"
  resource_group_name  = var.resource_group_name
  virtual_network_name = azurerm_virtual_network.this[0].name
  address_prefixes     = [var.aks_subnet_cidr]
}

# PG Flexible Server subnet — delegated to Microsoft.DBforPostgreSQL/flexibleServers, PG
# only. Azure forbids private endpoints in a delegated subnet, so this subnet must not also
# host the Cosmos/Redis/Blob/Key Vault private endpoints — see azurerm_subnet.pe below.
resource "azurerm_subnet" "data" {
  count = local.create_vnet ? 1 : 0

  name                 = "${local.name_prefix}-data"
  resource_group_name  = var.resource_group_name
  virtual_network_name = azurerm_virtual_network.this[0].name
  address_prefixes     = [var.data_subnet_cidr]

  delegation {
    name = "pg-flexible-server"
    service_delegation {
      name    = "Microsoft.DBforPostgreSQL/flexibleServers"
      actions = ["Microsoft.Network/virtualNetworks/subnets/join/action"]
    }
  }
}

# Private-endpoint subnet: Cosmos Mongo vCore, Redis, Key Vault, Blob. Deliberately separate
# from azurerm_subnet.data — a subnet delegated to a service (PG Flexible above) cannot also
# host azurerm_private_endpoint resources; Azure rejects the PE create with a delegation
# conflict. No delegation on this subnet.
resource "azurerm_subnet" "pe" {
  count = local.create_vnet ? 1 : 0

  name                 = "${local.name_prefix}-pe"
  resource_group_name  = var.resource_group_name
  virtual_network_name = azurerm_virtual_network.this[0].name
  address_prefixes     = [var.pe_subnet_cidr]
}

resource "azurerm_subnet" "appgw" {
  count = local.create_vnet ? 1 : 0

  name                 = "${local.name_prefix}-appgw"
  resource_group_name  = var.resource_group_name
  virtual_network_name = azurerm_virtual_network.this[0].name
  address_prefixes     = [var.appgw_subnet_cidr]
}

# --- NSGs: deny-inbound-by-default posture -----------------------------------------------
# No explicit inbound rules on aks/data/pe — Azure's own implicit rule set already gives
# this posture: AllowVnetInBound (65000, intra-VNet only) + AllowAzureLoadBalancerInBound
# (65001, health probes) + a catch-all DenyAllInbound at 65500. An explicit "DenyAllInbound"
# rule at priority 4096 (evaluated before 65000/65001, lower number = higher priority) would
# shadow both implicit allows: AKS node-to-node/kubelet traffic and the platform Standard LB's
# health probes are intra-VNet or from the AzureLoadBalancer tag, so a 4096 deny-all blocks
# them just as surely as external traffic, breaking the cluster (nodes NotReady, LB backend
# pool marked unhealthy) and PG/Cosmos/Redis/Blob's own private-endpoint reachability from
# workloads in the same VNet. The implicit 65500 deny already blocks everything else,
# including all inbound internet traffic — nothing below needs restating that.

resource "azurerm_network_security_group" "aks" {
  count = local.create_vnet ? 1 : 0

  name                = "${local.name_prefix}-aks-nsg"
  resource_group_name = var.resource_group_name
  location            = var.location
  tags                = var.tags
}

resource "azurerm_network_security_group" "data" {
  count = local.create_vnet ? 1 : 0

  name                = "${local.name_prefix}-data-nsg"
  resource_group_name = var.resource_group_name
  location            = var.location
  tags                = var.tags
}

resource "azurerm_network_security_group" "pe" {
  count = local.create_vnet ? 1 : 0

  name                = "${local.name_prefix}-pe-nsg"
  resource_group_name = var.resource_group_name
  location            = var.location
  tags                = var.tags
}

# App Gateway v2 requires these exact inbound allowances on its own subnet (no NSG rule can
# override them, but Azure validates the subnet's NSG contains no conflicting deny at these
# priorities) — see https://learn.microsoft.com/azure/application-gateway/configuration-infrastructure#network-security-groups
resource "azurerm_network_security_group" "appgw" {
  count = local.create_vnet ? 1 : 0

  name                = "${local.name_prefix}-appgw-nsg"
  resource_group_name = var.resource_group_name
  location            = var.location
  tags                = var.tags

  security_rule {
    name                       = "AllowGatewayManagerInbound"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "65200-65535"
    source_address_prefix      = "GatewayManager"
    destination_address_prefix = "*"
    description                = "Required control-plane channel for App Gateway v2."
  }

  security_rule {
    name                       = "AllowAzureLoadBalancerInbound"
    priority                   = 110
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "*"
    source_port_range          = "*"
    destination_port_range     = "*"
    source_address_prefix      = "AzureLoadBalancer"
    destination_address_prefix = "*"
    description                = "Health probes."
  }

  security_rule {
    name                       = "AllowHttpHttpsInbound"
    priority                   = 120
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_ranges    = ["80", "443"]
    source_address_prefix      = "Internet"
    destination_address_prefix = "*"
    description                = "Public ingress traffic (ingress_profile = appgw_waf)."
  }

  security_rule {
    name                       = "DenyAllInbound"
    priority                   = 4096
    direction                  = "Inbound"
    access                     = "Deny"
    protocol                   = "*"
    source_port_range          = "*"
    destination_port_range     = "*"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
    description                = "Issue #321: everything else denied."
  }
}

resource "azurerm_subnet_network_security_group_association" "aks" {
  count                     = local.create_vnet ? 1 : 0
  subnet_id                 = azurerm_subnet.aks[0].id
  network_security_group_id = azurerm_network_security_group.aks[0].id
}

resource "azurerm_subnet_network_security_group_association" "data" {
  count                     = local.create_vnet ? 1 : 0
  subnet_id                 = azurerm_subnet.data[0].id
  network_security_group_id = azurerm_network_security_group.data[0].id
}

resource "azurerm_subnet_network_security_group_association" "pe" {
  count                     = local.create_vnet ? 1 : 0
  subnet_id                 = azurerm_subnet.pe[0].id
  network_security_group_id = azurerm_network_security_group.pe[0].id
}

resource "azurerm_subnet_network_security_group_association" "appgw" {
  count                     = local.create_vnet ? 1 : 0
  subnet_id                 = azurerm_subnet.appgw[0].id
  network_security_group_id = azurerm_network_security_group.appgw[0].id
}

# --- NAT gateway on the AKS subnet --------------------------------------------------------
# Regional (no `zones`) on both resources: azurerm_nat_gateway is itself a regional
# (non-zonal) resource in this provider version — a zone-pinned public IP cannot associate
# to it (Azure rejects the association: zone mismatch). Regional NAT still gets a
# zone-redundant public IP's underlying platform reliability without pinning to a zone at
# all, so nothing is lost by omitting `zones` here. modules/aks correspondingly sets
# outbound_type = "userAssignedNATGateway" (not "loadBalancer") for the AKS subnet — a
# NAT-gateway-attached subnet cannot also use the platform LB for outbound SNAT.

resource "azurerm_public_ip" "nat" {
  count = local.create_vnet ? 1 : 0

  name                = "${local.name_prefix}-nat-pip"
  resource_group_name = var.resource_group_name
  location            = var.location
  allocation_method   = "Static"
  sku                 = "Standard"
  tags                = var.tags
}

resource "azurerm_nat_gateway" "aks" {
  count = local.create_vnet ? 1 : 0

  name                = "${local.name_prefix}-nat"
  resource_group_name = var.resource_group_name
  location            = var.location
  sku_name            = "Standard"
  tags                = var.tags
}

resource "azurerm_nat_gateway_public_ip_association" "aks" {
  count                = local.create_vnet ? 1 : 0
  nat_gateway_id       = azurerm_nat_gateway.aks[0].id
  public_ip_address_id = azurerm_public_ip.nat[0].id
}

resource "azurerm_subnet_nat_gateway_association" "aks" {
  count          = local.create_vnet ? 1 : 0
  subnet_id      = azurerm_subnet.aks[0].id
  nat_gateway_id = azurerm_nat_gateway.aks[0].id
}

# --- Private DNS zones + VNet links -------------------------------------------------------

resource "azurerm_private_dns_zone" "this" {
  for_each = var.enable_private_endpoints ? local.private_dns_zone_names : {}

  name                = each.value
  resource_group_name = var.resource_group_name
  tags                = var.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "this" {
  for_each = var.enable_private_endpoints ? local.private_dns_zone_names : {}

  name                  = "${local.name_prefix}-${each.key}-link"
  resource_group_name   = var.resource_group_name
  private_dns_zone_name = azurerm_private_dns_zone.this[each.key].name
  virtual_network_id    = local.vnet_id
  registration_enabled  = false
  tags                  = var.tags
}

locals {
  vnet_id = local.create_vnet ? azurerm_virtual_network.this[0].id : var.existing_vnet_id

  subnet_ids = local.create_vnet ? {
    aks   = azurerm_subnet.aks[0].id
    data  = azurerm_subnet.data[0].id
    pe    = azurerm_subnet.pe[0].id
    appgw = azurerm_subnet.appgw[0].id
    } : {
    aks   = var.existing_subnet_ids["aks"]
    data  = var.existing_subnet_ids["data"]
    pe    = var.existing_subnet_ids["pe"]
    appgw = var.existing_subnet_ids["appgw"]
  }
}
