# CloudFront delante del balanceador.
#
# Para que: HTTPS con certificado valido sin dominio propio
# (https://<algo>.cloudfront.net). Stripe exige HTTPS para el webhook y el
# navegador no deja que una pagina https hable con una API http. Ademas
# deja el balanceador cerrado al publico, y en produccion aqui se cuelga el
# WAF y el dominio de PSD.
#
# Sin cache: todo lo de Palco es dinamico (mapa, apartados, pagos). Pasan
# todos los encabezados (Stripe-Signature, X-Palco-Turno,
# X-Palco-Verificacion, Authorization), las cookies y la cadena de consulta.
#
# OJO, solo en sandbox sin dominio: de CloudFront al balanceador va por
# HTTP dentro de la red de AWS. Con dominio y certificado en el balanceador,
# el origen pasa a HTTPS (origin_protocol_policy).

locals {
  encabezado_origen = "X-Palco-Origen"
}

# El secreto que solo nuestra distribucion le manda al balanceador.
resource "random_password" "origen" {
  length  = 40
  special = false
}

data "aws_cloudfront_cache_policy" "sin_cache" {
  name = "Managed-CachingDisabled"
}

data "aws_cloudfront_origin_request_policy" "todo_menos_host" {
  name = "Managed-AllViewerExceptHostHeader"
}

resource "aws_cloudfront_distribution" "api" {
  count = var.con_cloudfront ? 1 : 0

  enabled         = true
  comment         = "Palco ${var.entorno}"
  is_ipv6_enabled = true
  http_version    = "http2and3"
  # Todas las ubicaciones, incluidas las de Mexico: el comprador entra por
  # la mas cercana.
  price_class = "PriceClass_All"

  origin {
    origin_id   = "balanceador"
    domain_name = aws_lb.palco.dns_name

    custom_origin_config {
      http_port                = 80
      https_port               = 443
      origin_protocol_policy   = local.con_https ? "https-only" : "http-only"
      origin_ssl_protocols     = ["TLSv1.2"]
      origin_read_timeout      = 60
      origin_keepalive_timeout = 5
    }

    custom_header {
      name  = local.encabezado_origen
      value = random_password.origen.result
    }
  }

  default_cache_behavior {
    target_origin_id         = "balanceador"
    viewer_protocol_policy   = "redirect-to-https"
    allowed_methods          = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods           = ["GET", "HEAD"]
    cache_policy_id          = data.aws_cloudfront_cache_policy.sin_cache.id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.todo_menos_host.id
    compress                 = true
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }
}
