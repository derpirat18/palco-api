# Almacen de imagenes (ECR). Etiquetas inmutables: una etiqueta (el commit)
# siempre es la misma imagen, asi un despliegue no puede cambiar a
# escondidas.

resource "aws_ecr_repository" "palco" {
  name                 = local.nombre
  image_tag_mutability = "IMMUTABLE"
  force_delete         = var.permitir_borrar_imagenes

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_lifecycle_policy" "palco" {
  repository = aws_ecr_repository.palco.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Conservar las ultimas 30 imagenes"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 30
      }
      action = { type = "expire" }
    }]
  })
}
