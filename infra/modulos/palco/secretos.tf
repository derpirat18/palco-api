# Secretos en Parameter Store (SecureString, cifrados con KMS). ECS los
# entrega a cada contenedor al arrancar; nunca estan en la imagen ni en el
# codigo.
#
# Dos clases:
#
# - Los que genera Terraform (llaves de firma, URLs con contrasena).
# - Los que pone una persona (Stripe, SMTP de SES, Turnstile): Terraform los
#   crea con "pendiente" y NO vuelve a tocarlos (ignore_changes). Se cambian
#   con `aws ssm put-parameter --overwrite` desde la terminal de quien los
#   tiene, nunca pegandolos en un chat ni en un archivo del repositorio.
#
# Ojo: los que genera Terraform quedan tambien en su estado (el bucket S3
# privado y cifrado). Por eso el bucket no se comparte.

locals {
  ruta_secretos = "/palco/${var.entorno}"

  secretos_generados = {
    SECRET_KEY         = random_password.secret_key.result
    TICKET_SIGNING_KEY = random_password.ticket_signing_key.result
    DATABASE_URL       = "postgresql://palco:${random_password.postgres.result}@${aws_db_instance.palco.address}:5432/palco"
    REDIS_URL          = "rediss://:${random_password.valkey.result}@${aws_elasticache_replication_group.palco.primary_endpoint_address}:6379/0"
  }

  secretos_manuales = toset([
    "STRIPE_SECRET_KEY",
    "STRIPE_PUBLISHABLE_KEY",
    "STRIPE_WEBHOOK_SECRET",
    "CORREO_SMTP_USUARIO",
    "CORREO_SMTP_PASSWORD",
    "TURNSTILE_SECRET_KEY",
  ])
}

resource "random_password" "secret_key" {
  length  = 64
  special = false
}

resource "random_password" "ticket_signing_key" {
  length  = 64
  special = false
}

resource "aws_ssm_parameter" "generado" {
  for_each = local.secretos_generados

  name  = "${local.ruta_secretos}/${each.key}"
  type  = "SecureString"
  value = each.value
}

resource "aws_ssm_parameter" "manual" {
  for_each = local.secretos_manuales

  name  = "${local.ruta_secretos}/${each.key}"
  type  = "SecureString"
  value = "pendiente"

  lifecycle {
    ignore_changes = [value]
  }
}

locals {
  # Lo que recibe cada contenedor como secreto: nombre de variable -> ARN.
  secretos_contenedor = [
    for nombre, p in merge(aws_ssm_parameter.generado, aws_ssm_parameter.manual) : {
      name      = nombre
      valueFrom = p.arn
    }
  ]
}
