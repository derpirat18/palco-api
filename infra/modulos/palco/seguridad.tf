# Quien puede hablar con quien. Todo lo que no esta aqui esta cerrado.
#
#   internet  -> balanceador     80 / 443 (sin CloudFront)
#   CloudFront -> balanceador    80       (con CloudFront: nadie mas entra)
#   balanceador -> contenedores  8000
#   contenedores -> PostgreSQL   5432
#   contenedores -> Valkey       6379
#   contenedores -> internet     cualquiera (Stripe, Cloudflare, SES, ECR)

resource "aws_security_group" "balanceador" {
  name        = "${local.nombre}-balanceador"
  description = "Entrada publica de Palco"
  vpc_id      = aws_vpc.palco.id
  tags        = { Name = "${local.nombre}-balanceador" }
}

data "aws_ec2_managed_prefix_list" "cloudfront" {
  name = "com.amazonaws.global.cloudfront.origin-facing"
}

resource "aws_vpc_security_group_ingress_rule" "balanceador_desde_cloudfront" {
  count = var.con_cloudfront ? 1 : 0

  security_group_id = aws_security_group.balanceador.id
  description       = "Solo CloudFront"
  prefix_list_id    = data.aws_ec2_managed_prefix_list.cloudfront.id
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
}

resource "aws_vpc_security_group_ingress_rule" "balanceador_http" {
  count = var.con_cloudfront ? 0 : 1

  security_group_id = aws_security_group.balanceador.id
  description       = "HTTP desde internet"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
}

resource "aws_vpc_security_group_ingress_rule" "balanceador_https" {
  count = var.con_cloudfront ? 0 : 1

  security_group_id = aws_security_group.balanceador.id
  description       = "HTTPS desde internet"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_security_group_egress_rule" "balanceador_a_contenedores" {
  security_group_id            = aws_security_group.balanceador.id
  description                  = "Solo hacia la API"
  referenced_security_group_id = aws_security_group.contenedores.id
  ip_protocol                  = "tcp"
  from_port                    = 8000
  to_port                      = 8000
}

resource "aws_security_group" "contenedores" {
  name        = "${local.nombre}-contenedores"
  description = "API, barrido y bandeja"
  vpc_id      = aws_vpc.palco.id
  tags        = { Name = "${local.nombre}-contenedores" }
}

resource "aws_vpc_security_group_ingress_rule" "contenedores_desde_balanceador" {
  security_group_id            = aws_security_group.contenedores.id
  description                  = "Solo el balanceador entra a la API"
  referenced_security_group_id = aws_security_group.balanceador.id
  ip_protocol                  = "tcp"
  from_port                    = 8000
  to_port                      = 8000
}

resource "aws_vpc_security_group_egress_rule" "contenedores_a_internet" {
  security_group_id = aws_security_group.contenedores.id
  description       = "Stripe, Cloudflare, SES, ECR, base y Valkey"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

resource "aws_security_group" "postgres" {
  name        = "${local.nombre}-postgres"
  description = "PostgreSQL de Palco"
  vpc_id      = aws_vpc.palco.id
  tags        = { Name = "${local.nombre}-postgres" }
}

resource "aws_vpc_security_group_ingress_rule" "postgres_desde_contenedores" {
  security_group_id            = aws_security_group.postgres.id
  description                  = "Solo los contenedores de Palco"
  referenced_security_group_id = aws_security_group.contenedores.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

resource "aws_security_group" "valkey" {
  name        = "${local.nombre}-valkey"
  description = "Valkey de Palco"
  vpc_id      = aws_vpc.palco.id
  tags        = { Name = "${local.nombre}-valkey" }
}

resource "aws_vpc_security_group_ingress_rule" "valkey_desde_contenedores" {
  security_group_id            = aws_security_group.valkey.id
  description                  = "Solo los contenedores de Palco"
  referenced_security_group_id = aws_security_group.contenedores.id
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
}
