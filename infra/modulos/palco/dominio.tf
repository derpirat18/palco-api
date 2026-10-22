# El dominio de PSD en este entorno.
#
#   comprador -> https://psdevents.com     (CloudFront, certificado de Virginia)
#   CloudFront -> https://origen.psdevents.com   (balanceador, certificado de la region)
#
# Con dominio, de CloudFront al balanceador tambien va cifrado: por ahi
# pasan contrasenas del panel, tokens y datos del comprador. Sin dominio
# (sandbox de antes) va por HTTP, porque el balanceador no tiene a nombre de
# quien sacar certificado.
#
# Lo que es de la cuenta (la zona, el certificado de la pagina y el
# remitente en SES) vive en entornos/dominio, porque lo comparten los dos
# entornos.

locals {
  con_dominio      = var.dominio != null
  sitio_principal  = local.con_dominio ? var.dominio.sitio[0] : ""
  sitios_que_mudan = local.con_dominio ? slice(var.dominio.sitio, 1, length(var.dominio.sitio)) : []
}

data "aws_route53_zone" "dominio" {
  count = local.con_dominio ? 1 : 0

  name         = var.dominio.zona
  private_zone = false
}

# --- El balanceador con nombre y certificado propio ---------------------------

resource "aws_acm_certificate" "origen" {
  count = local.con_dominio ? 1 : 0

  domain_name       = var.dominio.origen
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_route53_record" "validar_origen" {
  count = local.con_dominio ? 1 : 0

  zone_id = data.aws_route53_zone.dominio[0].zone_id
  name    = one(aws_acm_certificate.origen[0].domain_validation_options).resource_record_name
  type    = one(aws_acm_certificate.origen[0].domain_validation_options).resource_record_type
  records = [one(aws_acm_certificate.origen[0].domain_validation_options).resource_record_value]
  ttl     = 300
}

resource "aws_acm_certificate_validation" "origen" {
  count = local.con_dominio ? 1 : 0

  certificate_arn         = aws_acm_certificate.origen[0].arn
  validation_record_fqdns = [aws_route53_record.validar_origen[0].fqdn]
}

resource "aws_route53_record" "origen" {
  count = local.con_dominio ? 1 : 0

  zone_id = data.aws_route53_zone.dominio[0].zone_id
  name    = var.dominio.origen
  type    = "A"

  alias {
    name                   = aws_lb.palco.dns_name
    zone_id                = aws_lb.palco.zone_id
    evaluate_target_health = false
  }
}

# --- La pagina: los nombres apuntan a CloudFront ------------------------------

resource "aws_route53_record" "sitio" {
  for_each = local.con_dominio && var.con_cloudfront ? {
    for par in setproduct(var.dominio.sitio, ["A", "AAAA"]) : "${par[0]} ${par[1]}" => { nombre = par[0], tipo = par[1] }
  } : {}

  zone_id = data.aws_route53_zone.dominio[0].zone_id
  name    = each.value.nombre
  type    = each.value.tipo

  alias {
    name                   = aws_cloudfront_distribution.api[0].domain_name
    zone_id                = aws_cloudfront_distribution.api[0].hosted_zone_id
    evaluate_target_health = false
  }
}
