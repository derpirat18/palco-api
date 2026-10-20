# Correo con Amazon SES por SMTP.
#
# La bandeja manda los correos por SMTP (CORREO_SMTP_*). SES pide un usuario
# y una contrasena SMTP, que salen de una llave de acceso de IAM. Terraform
# crea un usuario que SOLO puede mandar correo desde el remitente de Palco,
# genera su llave y la guarda directo en Parameter Store: nadie la ve ni la
# copia.
#
# La identidad del remitente (el correo o, en produccion, el dominio) se
# verifica aparte, una vez, porque pide abrir un enlace o poner registros en
# el DNS:
#   aws sesv2 create-email-identity --email-identity <correo o dominio>
#
# Mientras la cuenta de SES este en modo de pruebas, solo se puede mandar a
# direcciones verificadas. Antes del piloto se pide la salida a AWS.
#
# Para rotar la llave: terraform apply -replace=module.palco.aws_iam_access_key.correo
# y reiniciar bandeja (force-new-deployment).

locals {
  # "Palco <boletos@palco.mx>" -> boletos@palco.mx y palco.mx
  correo_de_remitente  = regex("[^<\\s]+@[^>\\s]+", var.correo_remitente)
  dominio_de_remitente = split("@", local.correo_de_remitente)[1]
}

resource "aws_iam_user" "correo" {
  name = "${local.nombre}-correo"
  tags = { Uso = "SMTP de SES para la bandeja" }
}

data "aws_iam_policy_document" "mandar_correo" {
  statement {
    sid     = "SoloMandarDesdeElRemitente"
    actions = ["ses:SendRawEmail"]
    # Sirve igual si se verifico el correo o el dominio completo.
    resources = [
      "arn:aws:ses:${data.aws_region.actual.region}:${data.aws_caller_identity.actual.account_id}:identity/${local.correo_de_remitente}",
      "arn:aws:ses:${data.aws_region.actual.region}:${data.aws_caller_identity.actual.account_id}:identity/${local.dominio_de_remitente}",
    ]
  }
}

resource "aws_iam_user_policy" "mandar_correo" {
  name   = "mandar-correo"
  user   = aws_iam_user.correo.name
  policy = data.aws_iam_policy_document.mandar_correo.json
}

resource "aws_iam_access_key" "correo" {
  user = aws_iam_user.correo.name
}
