# ECS Fargate: la misma imagen corre cuatro trabajos (ver el Dockerfile).
#
#   api      servicio detras del balanceador, de minimo a maximo copias
#   barrido  servicio, una copia
#   bandeja  servicio, una copia
#   migrar   tarea suelta, se corre antes de cada despliegue

data "aws_region" "actual" {}
data "aws_caller_identity" "actual" {}

resource "aws_ecs_cluster" "palco" {
  name = local.nombre

  setting {
    name  = "containerInsights"
    value = "disabled" # cuesta aparte; las alarmas basicas bastan por ahora
  }
}

# --- Registros ---------------------------------------------------------------

resource "aws_cloudwatch_log_group" "proceso" {
  for_each = toset(["api", "barrido", "bandeja", "migrar"])

  name              = "/palco/${var.entorno}/${each.key}"
  retention_in_days = var.dias_de_registros
}

# --- Permisos ----------------------------------------------------------------

data "aws_iam_policy_document" "asumir_desde_ecs" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# Lo usa ECS para arrancar el contenedor: bajar la imagen, escribir
# registros y leer los secretos de ESTE entorno, nada mas.
resource "aws_iam_role" "ejecucion" {
  name               = "${local.nombre}-ejecucion"
  assume_role_policy = data.aws_iam_policy_document.asumir_desde_ecs.json
}

resource "aws_iam_role_policy_attachment" "ejecucion_base" {
  role       = aws_iam_role.ejecucion.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "leer_secretos" {
  statement {
    actions = ["ssm:GetParameters"]
    resources = [
      "arn:aws:ssm:${data.aws_region.actual.region}:${data.aws_caller_identity.actual.account_id}:parameter${local.ruta_secretos}/*",
    ]
  }
}

resource "aws_iam_role_policy" "ejecucion_secretos" {
  name   = "leer-secretos"
  role   = aws_iam_role.ejecucion.id
  policy = data.aws_iam_policy_document.leer_secretos.json
}

# Lo usa la app mientras corre: ECS Exec (entrar a un contenedor a dar de
# alta el primer admin) y subir las imagenes de los eventos (medios.tf).
resource "aws_iam_role" "tarea" {
  name               = "${local.nombre}-tarea"
  assume_role_policy = data.aws_iam_policy_document.asumir_desde_ecs.json
}

data "aws_iam_policy_document" "ecs_exec" {
  statement {
    actions = [
      "ssmmessages:CreateControlChannel",
      "ssmmessages:CreateDataChannel",
      "ssmmessages:OpenControlChannel",
      "ssmmessages:OpenDataChannel",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "tarea_exec" {
  name   = "ecs-exec"
  role   = aws_iam_role.tarea.id
  policy = data.aws_iam_policy_document.ecs_exec.json
}

# --- Definiciones de los contenedores ---------------------------------------

locals {
  imagen = "${aws_ecr_repository.palco.repository_url}:${var.imagen_tag}"

  # Configuracion que no es secreta. Los validadores de app/core/config.py
  # revisan al arrancar que fuera de local todo este como debe.
  variables_app = [
    for nombre, valor in {
      ENTORNO                   = var.entorno_app
      DB_SSL                    = "true"
      DB_POOL_SIZE              = tostring(var.pool_db.tamano)
      DB_MAX_OVERFLOW           = tostring(var.pool_db.reserva)
      LIMITES_ACTIVOS           = "true"
      PROXIES_DE_CONFIANZA      = var.con_cloudfront ? "2" : "1" # CloudFront y el balanceador
      SALA_DE_ESPERA_HABILITADA = "false"
      FRONT_URL                 = local.front_url
      CORS_ORIGINS              = jsonencode(var.cors_origenes)
      CORREO_SMTP_HOST          = "email-smtp.${local.region_correo}.amazonaws.com"
      CORREO_SMTP_PUERTO        = "587"
      CORREO_SMTP_STARTTLS      = "true"
      CORREO_REMITENTE          = var.correo_remitente
      TURNSTILE_SITE_KEY        = var.turnstile_site_key
      # Imagenes de los eventos (medios.tf). Sin CloudFront no hay donde
      # servirlas: la API avisa que no esta configurado.
      MEDIOS_BUCKET = var.con_cloudfront ? aws_s3_bucket.medios[0].bucket : ""
      MEDIOS_REGION = data.aws_region.actual.region
      MEDIOS_URL    = local.url_publica
    } : { name = nombre, value = valor }
  ]

  contenedor_base = {
    image       = local.imagen
    essential   = true
    environment = local.variables_app
    secrets     = local.secretos_contenedor
    # tini como PID 1: los trabajadores se detienen al instante al desplegar.
    linuxParameters = { initProcessEnabled = true }
    stopTimeout     = 30
  }

  procesos = {
    api = {
      command = null # el CMD del Dockerfile
      cpu     = var.api.cpu
      memoria = var.api.memoria
      puertos = [{ containerPort = 8000, protocol = "tcp" }]
    }
    barrido = {
      command = ["python", "-m", "app.workers.barrido"]
      cpu     = var.trabajador.cpu
      memoria = var.trabajador.memoria
      puertos = []
    }
    bandeja = {
      command = ["python", "-m", "app.workers.bandeja"]
      cpu     = var.trabajador.cpu
      memoria = var.trabajador.memoria
      puertos = []
    }
    migrar = {
      command = ["alembic", "upgrade", "head"]
      cpu     = var.trabajador.cpu
      memoria = var.trabajador.memoria
      puertos = []
    }
  }
}

resource "aws_ecs_task_definition" "proceso" {
  for_each = local.procesos

  family                   = "${local.nombre}-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = each.value.cpu
  memory                   = each.value.memoria
  execution_role_arn       = aws_iam_role.ejecucion.arn
  task_role_arn            = aws_iam_role.tarea.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.arquitectura
  }

  container_definitions = jsonencode([
    merge(local.contenedor_base, {
      name         = each.key
      command      = each.value.command
      portMappings = each.value.puertos
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.proceso[each.key].name
          awslogs-region        = data.aws_region.actual.region
          awslogs-stream-prefix = each.key
        }
      }
    })
  ])
}

# --- Servicios ---------------------------------------------------------------

locals {
  red_contenedores = {
    subnets          = aws_subnet.publica[*].id
    security_groups  = [aws_security_group.contenedores.id]
    assign_public_ip = true
  }
}

resource "aws_ecs_service" "api" {
  name            = "api"
  cluster         = aws_ecs_cluster.palco.id
  task_definition = aws_ecs_task_definition.proceso["api"].arn
  desired_count   = var.api.minimo
  launch_type     = "FARGATE"

  enable_execute_command            = true
  health_check_grace_period_seconds = 60
  propagate_tags                    = "SERVICE"

  # Siempre hay copias sanas: las nuevas arrancan antes de quitar las viejas.
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  # Si la version nueva no arranca, ECS regresa sola a la anterior.
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = local.red_contenedores.subnets
    security_groups  = local.red_contenedores.security_groups
    assign_public_ip = local.red_contenedores.assign_public_ip
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8000
  }

  # El autoescalado mueve las copias; Terraform no lo pelea.
  lifecycle {
    ignore_changes = [desired_count]
  }

  depends_on = [aws_lb_listener.http]
}

resource "aws_ecs_service" "trabajador" {
  for_each = toset(["barrido", "bandeja"])

  name            = each.key
  cluster         = aws_ecs_cluster.palco.id
  task_definition = aws_ecs_task_definition.proceso[each.key].arn
  desired_count   = 1
  launch_type     = "FARGATE"

  enable_execute_command = true
  propagate_tags         = "SERVICE"

  # Una sola copia: se detiene la vieja y luego arranca la nueva. El barrido
  # tiene ademas candado en Postgres por si coinciden dos.
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = local.red_contenedores.subnets
    security_groups  = local.red_contenedores.security_groups
    assign_public_ip = local.red_contenedores.assign_public_ip
  }
}

# --- Autoescalado de la API --------------------------------------------------

resource "aws_appautoscaling_target" "api" {
  service_namespace  = "ecs"
  resource_id        = "service/${aws_ecs_cluster.palco.name}/${aws_ecs_service.api.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  min_capacity       = var.api.minimo
  max_capacity       = var.api.maximo
}

resource "aws_appautoscaling_policy" "api_cpu" {
  name               = "${local.nombre}-api-cpu"
  service_namespace  = aws_appautoscaling_target.api.service_namespace
  resource_id        = aws_appautoscaling_target.api.resource_id
  scalable_dimension = aws_appautoscaling_target.api.scalable_dimension
  policy_type        = "TargetTrackingScaling"

  target_tracking_scaling_policy_configuration {
    target_value       = 60
    scale_out_cooldown = 60
    scale_in_cooldown  = 300

    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
  }
}
