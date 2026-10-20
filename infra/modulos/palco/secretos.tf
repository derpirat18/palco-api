# Secretos en Parameter Store (SecureString, cifrados con KMS). ECS los
# entrega a cada contenedor al arrancar; nunca estan en la imagen ni en el
# codigo.
#
# Dos clases:
#
# - Los que genera Terraform (llaves de firma, URLs con contrasena, el
#   usuario y la contrasena SMTP de SES, ver correo.tf).
# - Los que pone una persona (Stripe, Turnstile): Terraform los
#   crea con "pendiente" y NO vuelve a tocarlos (ignore_changes). Se cambian
#   con `aws ssm put-parameter --overwrite` desde la terminal de quien los
#   tiene, nunca pegandolos en un chat ni en un archivo del repositorio.
#
# Ojo: los que genera Terraform quedan tambien en su estado (el bucket S3
# privado y cifrado). Por eso el bucket no se comparte.

locals {
  ruta_secretos = "/palco/${var.entorno}"

  secretos_generados = {
    SECRET_KEY           = random_password.secret_key.result
    TICKET_SIGNING_KEY   = random_password.ticket_signing_key.result
    DATABASE_URL         = "postgresql://palco:${random_password.postgres.result}@${aws_db_instance.palco.address}:5432/palco"
    REDIS_URL            = "rediss://:${random_password.valkey.result}@${aws_elasticache_replication_group.palco.primary_endpoint_address}:6379/0"
    CORREO_SMTP_USUARIO  = aws_iam_access_key.correo.id
    CORREO_SMTP_PASSWORD = aws_iam_access_key.correo.ses_smtp_password_v4
  }

  secretos_manuales = toset([
    "STRIPE_SECRET_KEY",
    "STRIPE_PUBLISHABLE_KEY",
    "STRIPE_WEBHOOK_SECRET",
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

# Los de SMTP antes eran manuales (con "pendiente"); ahora los genera
# Terraform. Se mueven en el estado para que no se borren y se vuelvan a crear.
moved {
  from = aws_ssm_parameter.manual["CORREO_SMTP_USUARIO"]
  to   = aws_ssm_parameter.generado["CORREO_SMTP_USUARIO"]
}

moved {
  from = aws_ssm_parameter.manual["CORREO_SMTP_PASSWORD"]
  to   = aws_ssm_parameter.generado["CORREO_SMTP_PASSWORD"]
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
