# Red de Palco: dos zonas, cada una con una subred publica (balanceador y
# contenedores) y una privada (PostgreSQL y Valkey, sin internet).
#
# Sin NAT Gateway (~70 USD/mes en dos zonas): los contenedores salen a
# Stripe, Cloudflare y SES con IP publica propia, y su grupo de seguridad
# solo acepta conexiones del balanceador.

locals {
  nombre = "palco-${var.entorno}"
  zonas  = slice(data.aws_availability_zones.disponibles.names, 0, 2)
}

data "aws_availability_zones" "disponibles" {
  state = "available"
}

resource "aws_vpc" "palco" {
  cidr_block           = var.cidr_vpc
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = local.nombre }
}

resource "aws_internet_gateway" "palco" {
  vpc_id = aws_vpc.palco.id
  tags   = { Name = local.nombre }
}

resource "aws_subnet" "publica" {
  count = 2

  vpc_id                  = aws_vpc.palco.id
  availability_zone       = local.zonas[count.index]
  cidr_block              = cidrsubnet(var.cidr_vpc, 8, count.index)
  map_public_ip_on_launch = false

  tags = { Name = "${local.nombre}-publica-${count.index + 1}" }
}

resource "aws_subnet" "privada" {
  count = 2

  vpc_id            = aws_vpc.palco.id
  availability_zone = local.zonas[count.index]
  cidr_block        = cidrsubnet(var.cidr_vpc, 8, count.index + 10)

  tags = { Name = "${local.nombre}-privada-${count.index + 1}" }
}

resource "aws_route_table" "publica" {
  vpc_id = aws_vpc.palco.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.palco.id
  }

  tags = { Name = "${local.nombre}-publica" }
}

resource "aws_route_table_association" "publica" {
  count = 2

  subnet_id      = aws_subnet.publica[count.index].id
  route_table_id = aws_route_table.publica.id
}

# Las privadas usan la tabla principal de la VPC: solo trafico interno.
