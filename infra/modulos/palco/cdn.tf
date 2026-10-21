# CloudFront: una sola direccion para la pagina de compra y la API.
#
#   https://<algo>.cloudfront.net/            -> el front (archivos en S3)
#   https://<algo>.cloudfront.net/api/...     -> la API (el balanceador)
#
# Por que un solo dominio:
#   - HTTPS con certificado valido sin dominio propio; Stripe exige HTTPS
#     para el webhook.
#   - El front llama a la API en su mismo dominio: sin CORS y sin una
#     segunda direccion que configurar o certificar.
#   - El dia del dominio de PSD se cambia en un solo lugar.
#   - El balanceador queda cerrado al publico, y aqui se cuelga el WAF.
#
# La API va sin cache: todo es dinamico (mapa, apartados, pagos). Pasan
# todos los encabezados (Stripe-Signature, X-Palco-Turno,
# X-Palco-Verificacion, Authorization), las cookies y la cadena de consulta.
# El front si se guarda en cache: sus archivos llevan huella en el nombre;
# index.html se sube con "no-cache" para que un despliegue se vea al momento.
#
# OJO, solo en sandbox sin dominio: de CloudFront al balanceador va por
# HTTP dentro de la red de AWS. Con dominio y certificado en el balanceador,
# el origen pasa a HTTPS (origin_protocol_policy).

locals {
  encabezado_origen = "X-Palco-Origen"
  # Base de las ligas de los correos: la direccion publica, salvo que el
  # entorno fije otra (el dominio de PSD).
  front_url = var.front_url != "" ? var.front_url : (
    var.con_cloudfront ? "https://${aws_cloudfront_distribution.api[0].domain_name}" : ""
  )
}

# El secreto que solo nuestra distribucion le manda al balanceador.
resource "random_password" "origen" {
  length  = 40
  special = false
}

data "aws_cloudfront_cache_policy" "sin_cache" {
  name = "Managed-CachingDisabled"
}

data "aws_cloudfront_cache_policy" "archivos" {
  name = "Managed-CachingOptimized"
}

data "aws_cloudfront_origin_request_policy" "todo_menos_host" {
  name = "Managed-AllViewerExceptHostHeader"
}

data "aws_cloudfront_response_headers_policy" "seguridad" {
  name = "Managed-SecurityHeadersPolicy"
}

# --- El front: archivos privados en S3, solo CloudFront los lee -------------

resource "aws_s3_bucket" "front" {
  count = var.con_cloudfront ? 1 : 0

  bucket = "${local.nombre}-front-${data.aws_caller_identity.actual.account_id}"
  # Se vuelve a construir desde git; no guarda nada que no exista en otro lado.
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "front" {
  count = var.con_cloudfront ? 1 : 0

  bucket                  = aws_s3_bucket.front[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "front" {
  count = var.con_cloudfront ? 1 : 0

  bucket = aws_s3_bucket.front[0].id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_cloudfront_origin_access_control" "front" {
  count = var.con_cloudfront ? 1 : 0

  name                              = "${local.nombre}-front"
  description                       = "CloudFront lee el front de S3"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

data "aws_iam_policy_document" "front_desde_cloudfront" {
  count = var.con_cloudfront ? 1 : 0

  statement {
    sid       = "SoloNuestraDistribucion"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.front[0].arn}/*"]
    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.api[0].arn]
    }
  }
}

resource "aws_s3_bucket_policy" "front" {
  count = var.con_cloudfront ? 1 : 0

  bucket     = aws_s3_bucket.front[0].id
  policy     = data.aws_iam_policy_document.front_desde_cloudfront[0].json
  depends_on = [aws_s3_bucket_public_access_block.front]
}

# Las rutas del front (/funcion-de-prueba-palco, /orden/<uuid>) no son
# archivos: se responde index.html y React decide que pagina mostrar. Se
# hace con una funcion y no con "paginas de error" de CloudFront, porque
# esas aplican a toda la distribucion y taparian los 404 de la API.
resource "aws_cloudfront_function" "rutas_del_front" {
  count = var.con_cloudfront ? 1 : 0

  name    = "${local.nombre}-rutas-del-front"
  runtime = "cloudfront-js-2.0"
  comment = "Rutas sin extension -> /index.html"
  publish = true
  code    = <<-JS
    function handler(event) {
      var request = event.request;
      var ultimo = request.uri.split("/").pop();
      if (ultimo.indexOf(".") === -1) {
        request.uri = "/index.html";
      }
      return request;
    }
  JS
}

# --- La distribucion ----------------------------------------------------------

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

  origin {
    origin_id                = "front"
    domain_name              = aws_s3_bucket.front[0].bucket_regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.front[0].id
  }

  # La API.
  ordered_cache_behavior {
    path_pattern             = "/api/*"
    target_origin_id         = "balanceador"
    viewer_protocol_policy   = "redirect-to-https"
    allowed_methods          = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods           = ["GET", "HEAD"]
    cache_policy_id          = data.aws_cloudfront_cache_policy.sin_cache.id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.todo_menos_host.id
    compress                 = true
  }

  # Todo lo demas: la pagina de compra.
  default_cache_behavior {
    target_origin_id           = "front"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD"]
    cached_methods             = ["GET", "HEAD"]
    cache_policy_id            = data.aws_cloudfront_cache_policy.archivos.id
    response_headers_policy_id = data.aws_cloudfront_response_headers_policy.seguridad.id
    compress                   = true

    function_association {
      event_type   = "viewer-request"
      function_arn = aws_cloudfront_function.rutas_del_front[0].arn
    }
  }

  default_root_object = "index.html"

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }
}
