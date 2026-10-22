# Balanceador. Sin CloudFront es la puerta de entrada: con certificado, HTTP
# redirige a HTTPS; sin certificado, atiende HTTP.
#
# Con CloudFront (cdn.tf), el balanceador solo atiende a NUESTRA
# distribucion: el grupo de seguridad deja entrar solo a CloudFront, y la
# regla de abajo exige un encabezado secreto que solo nuestra distribucion
# conoce (cualquier otra cuenta de AWS podria apuntar su propio CloudFront
# aqui). Lo demas recibe 404.
#
# Tiempo de inactividad de 60 s: la API mantiene conexiones 75 s
# (--timeout-keep-alive en el Dockerfile). Si fuera al reves, el balanceador
# reusaria conexiones ya cerradas y daria 502.

locals {
  # Con dominio, el certificado del balanceador lo saca dominio.tf.
  con_https = local.con_dominio || var.certificado_arn != ""
  certificado_balanceador = (local.con_dominio
    ? aws_acm_certificate_validation.origen[0].certificate_arn
    : var.certificado_arn
  )
}

resource "aws_lb" "palco" {
  name               = local.nombre
  load_balancer_type = "application"
  internal           = false
  security_groups    = [aws_security_group.balanceador.id]
  subnets            = aws_subnet.publica[*].id

  idle_timeout               = 60
  drop_invalid_header_fields = true
  enable_deletion_protection = var.entorno == "produccion"
}

resource "aws_lb_target_group" "api" {
  name        = "${local.nombre}-api"
  port        = 8000
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = aws_vpc.palco.id

  # Al desplegar, cuanto espera a que una copia vieja termine lo que atiende.
  deregistration_delay = 30

  # Sonda superficial a proposito: si Redis o la base fallan un momento, no
  # se sacan todas las copias de la API a la vez.
  health_check {
    path                = "/api/v1/health"
    matcher             = "200"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.palco.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = local.con_https ? "redirect" : (var.con_cloudfront ? "fixed-response" : "forward")
    target_group_arn = local.con_https || var.con_cloudfront ? null : aws_lb_target_group.api.arn

    dynamic "fixed_response" {
      for_each = !local.con_https && var.con_cloudfront ? [1] : []
      content {
        content_type = "application/json"
        status_code  = "404"
        message_body = jsonencode({ error = { codigo = "no_encontrado", mensaje = "No encontramos lo que buscas.", detalle = {} } })
      }
    }

    dynamic "redirect" {
      for_each = local.con_https ? [1] : []
      content {
        port        = "443"
        protocol    = "HTTPS"
        status_code = "HTTP_301"
      }
    }
  }
}

resource "aws_lb_listener" "https" {
  count = local.con_https ? 1 : 0

  load_balancer_arn = aws_lb.palco.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = local.certificado_balanceador

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}

# Con CloudFront: solo pasa lo que trae el encabezado secreto de nuestra
# distribucion. Va en el escuchador que usa CloudFront (HTTP sin
# certificado; HTTPS cuando haya dominio).
resource "aws_lb_listener_rule" "desde_cloudfront" {
  count = var.con_cloudfront ? 1 : 0

  listener_arn = local.con_https ? aws_lb_listener.https[0].arn : aws_lb_listener.http.arn
  priority     = 10

  condition {
    http_header {
      http_header_name = local.encabezado_origen
      values           = [random_password.origen.result]
    }
  }

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}
