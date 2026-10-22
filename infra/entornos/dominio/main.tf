# El dominio de PSD (psdevents.com): lo que es de la cuenta y no de un
# entorno. Sandbox y produccion lo usan; ninguno de los dos lo crea.
#
#   - El certificado de la pagina (psdevents.com y *.psdevents.com). Lo usa
#     CloudFront, que solo acepta certificados de Virginia (us-east-1).
#   - El dominio como remitente en SES (Ohio): DKIM, MAIL FROM y DMARC, para
#     que los boletos no caigan en spam.
#
# Cada entorno pone sus propios nombres (pruebas.psdevents.com el sandbox,
# psdevents.com y www produccion) en su receta.
#
# La zona de Route 53 se creo una vez a mano, antes de cambiar los
# servidores de nombres en GoDaddy (borrarla y volver a crearla cambia esos
# servidores):
#   aws route53 create-hosted-zone --name psdevents.com --caller-reference psdevents-20261021
#
# Correr despues de que el dominio ya responda con los servidores de AWS:
# el certificado y SES se validan con registros en el DNS y Terraform espera.
#
# Uso (PowerShell, desde esta carpeta):
#   $env:AWS_PROFILE = "palco-tf"
#   terraform init
#   terraform plan  -out plan.tfplan
#   terraform apply plan.tfplan

terraform {
  required_version = ">= 1.10"

  backend "s3" {
    bucket       = "palco-tfstate-530629633953"
    key          = "dominio/terraform.tfstate"
    region       = "us-east-2"
    encrypt      = true
    use_lockfile = true
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

variable "dominio" {
  description = "El dominio de PSD, ya con su zona en Route 53."
  type        = string
  default     = "psdevents.com"
}

locals {
  etiquetas = {
    Proyecto = "Palco"
    Entorno  = "dominio"
    Gestion  = "Terraform"
  }
  # Subdominio que SES usa como remitente tecnico (rebotes): asi SPF tambien
  # cuadra con el dominio y no con amazonses.com.
  mail_from = "correo.${var.dominio}"
}

# Ohio: SES (no existe en Mexico) y el resto por omision.
provider "aws" {
  region              = "us-east-2"
  allowed_account_ids = ["530629633953"]
  default_tags {
    tags = local.etiquetas
  }
}

# Virginia: el unico lugar de donde CloudFront toma certificados.
provider "aws" {
  alias               = "virginia"
  region              = "us-east-1"
  allowed_account_ids = ["530629633953"]
  default_tags {
    tags = local.etiquetas
  }
}

data "aws_route53_zone" "dominio" {
  name         = var.dominio
  private_zone = false
}

# --- Certificado de la pagina ------------------------------------------------

resource "aws_acm_certificate" "pagina" {
  provider = aws.virginia

  domain_name               = var.dominio
  subject_alternative_names = ["*.${var.dominio}"]
  validation_method         = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

# Por nombre de dominio: es lo unico que se conoce antes de pedir el
# certificado. psdevents.com y *.psdevents.com piden el MISMO registro, por
# eso allow_overwrite (el segundo encuentra el que dejo el primero).
resource "aws_route53_record" "validar_pagina" {
  for_each = {
    for o in aws_acm_certificate.pagina.domain_validation_options : o.domain_name => o
  }

  zone_id         = data.aws_route53_zone.dominio.zone_id
  name            = each.value.resource_record_name
  type            = each.value.resource_record_type
  records         = [each.value.resource_record_value]
  ttl             = 300
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "pagina" {
  provider = aws.virginia

  certificate_arn         = aws_acm_certificate.pagina.arn
  validation_record_fqdns = [for r in aws_route53_record.validar_pagina : r.fqdn]
}

# --- Correo: el dominio como remitente en SES ---------------------------------

resource "aws_sesv2_email_identity" "dominio" {
  email_identity = var.dominio

  # Easy DKIM: AWS genera y rota las llaves; aqui solo se publican.
  dkim_signing_attributes {
    next_signing_key_length = "RSA_2048_BIT"
  }
}

# DKIM: tres CNAME que firman cada correo con el dominio.
resource "aws_route53_record" "dkim" {
  count = 3

  zone_id = data.aws_route53_zone.dominio.zone_id
  name    = "${aws_sesv2_email_identity.dominio.dkim_signing_attributes[0].tokens[count.index]}._domainkey.${var.dominio}"
  type    = "CNAME"
  records = ["${aws_sesv2_email_identity.dominio.dkim_signing_attributes[0].tokens[count.index]}.dkim.amazonses.com"]
  ttl     = 1800
}

resource "aws_sesv2_email_identity_mail_from_attributes" "dominio" {
  email_identity         = aws_sesv2_email_identity.dominio.email_identity
  mail_from_domain       = local.mail_from
  behavior_on_mx_failure = "USE_DEFAULT_VALUE"
}

resource "aws_route53_record" "mail_from_mx" {
  zone_id = data.aws_route53_zone.dominio.zone_id
  name    = local.mail_from
  type    = "MX"
  records = ["10 feedback-smtp.us-east-2.amazonses.com"]
  ttl     = 1800
}

resource "aws_route53_record" "mail_from_spf" {
  zone_id = data.aws_route53_zone.dominio.zone_id
  name    = local.mail_from
  type    = "TXT"
  records = ["v=spf1 include:amazonses.com ~all"]
  ttl     = 1800
}

# Gmail y Yahoo piden DMARC a quien manda correo en cantidad. "none": solo
# se publica, no se rechaza nada; se endurece cuando haya reportes.
resource "aws_route53_record" "dmarc" {
  zone_id = data.aws_route53_zone.dominio.zone_id
  name    = "_dmarc.${var.dominio}"
  type    = "TXT"
  records = ["v=DMARC1; p=none"]
  ttl     = 1800
}

# --- Salidas: las leen las recetas de sandbox y produccion --------------------

output "dominio" {
  value = var.dominio
}

output "certificado_pagina" {
  description = "ARN del certificado de Virginia para CloudFront."
  value       = aws_acm_certificate_validation.pagina.certificate_arn
}

output "zona" {
  value = data.aws_route53_zone.dominio.zone_id
}

output "servidores_de_nombres" {
  description = "Los que van en GoDaddy."
  value       = data.aws_route53_zone.dominio.name_servers
}
