# Balanceador: la unica puerta de entrada. Con certificado, HTTP redirige a
# HTTPS; sin certificado (sandbox sin dominio), atiende HTTP.
#
# Tiempo de inactividad de 60 s: la API mantiene conexiones 75 s
# (--timeout-keep-alive en el Dockerfile). Si fuera al reves, el balanceador
# reusaria conexiones ya cerradas y daria 502.

locals {
  con_https = var.certificado_arn != ""
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
    type             = local.con_https ? "redirect" : "forward"
    target_group_arn = local.con_https ? null : aws_lb_target_group.api.arn

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
  certificate_arn   = var.certificado_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}
