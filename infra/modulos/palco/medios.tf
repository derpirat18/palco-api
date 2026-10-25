# Imagenes de los eventos (F7). Las sube la API (app/services/medios.py) y
# las sirve CloudFront en <sitio>/medios/... desde este bucket privado.
#
# Bucket aparte del front: scripts/publicar.ps1 sincroniza el front con
# --delete y borraria lo que no viene en el build. Aqui vive lo que suben
# los organizadores: no se reconstruye desde git, asi que no se borra con
# el entorno salvo que se permita (permitir_borrar_imagenes).

resource "aws_s3_bucket" "medios" {
  count = var.con_cloudfront ? 1 : 0

  bucket        = "${local.nombre}-medios-${data.aws_caller_identity.actual.account_id}"
  force_destroy = var.permitir_borrar_imagenes
}

resource "aws_s3_bucket_public_access_block" "medios" {
  count = var.con_cloudfront ? 1 : 0

  bucket                  = aws_s3_bucket.medios[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "medios" {
  count = var.con_cloudfront ? 1 : 0

  bucket = aws_s3_bucket.medios[0].id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# Solo nuestra distribucion lee, y solo lo que esta bajo medios/.
data "aws_iam_policy_document" "medios_desde_cloudfront" {
  count = var.con_cloudfront ? 1 : 0

  statement {
    sid       = "SoloNuestraDistribucion"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.medios[0].arn}/medios/*"]
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

resource "aws_s3_bucket_policy" "medios" {
  count = var.con_cloudfront ? 1 : 0

  bucket     = aws_s3_bucket.medios[0].id
  policy     = data.aws_iam_policy_document.medios_desde_cloudfront[0].json
  depends_on = [aws_s3_bucket_public_access_block.medios]
}

# La API solo puede escribir imagenes de eventos: ni leer, ni borrar, ni
# tocar otra cosa del bucket.
data "aws_iam_policy_document" "subir_medios" {
  count = var.con_cloudfront ? 1 : 0

  statement {
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.medios[0].arn}/medios/eventos/*"]
  }
}

resource "aws_iam_role_policy" "tarea_medios" {
  count = var.con_cloudfront ? 1 : 0

  name   = "subir-medios"
  role   = aws_iam_role.tarea.id
  policy = data.aws_iam_policy_document.subir_medios[0].json
}
