output "url_api" {
  description = "Donde responde la API."
  value = (var.con_cloudfront
    ? "https://${aws_cloudfront_distribution.api[0].domain_name}/api/v1"
    : "${local.con_https ? "https" : "http"}://${aws_lb.palco.dns_name}/api/v1"
  )
}

output "repositorio_imagenes" {
  description = "A donde se sube la imagen (docker push)."
  value       = aws_ecr_repository.palco.repository_url
}

output "cluster" {
  value = aws_ecs_cluster.palco.name
}

output "region" {
  value = data.aws_region.actual.region
}

output "ruta_secretos" {
  description = "Prefijo de los secretos en Parameter Store."
  value       = local.ruta_secretos
}

output "comando_migrar" {
  description = "Corre las migraciones con la imagen desplegada (PowerShell, una linea)."
  value = join(" ", [
    "aws ecs run-task",
    # Con region: el perfil puede apuntar a otra (produccion vive en Mexico).
    "--region ${data.aws_region.actual.region}",
    "--cluster ${aws_ecs_cluster.palco.name}",
    "--task-definition ${aws_ecs_task_definition.proceso["migrar"].family}",
    "--launch-type FARGATE",
    "--network-configuration 'awsvpcConfiguration={subnets=[${join(",", aws_subnet.publica[*].id)}],securityGroups=[${aws_security_group.contenedores.id}],assignPublicIp=ENABLED}'",
    "--query 'tasks[0].taskArn' --output text",
  ])
}

output "url_front" {
  description = "Donde vive la pagina de compra."
  value       = var.con_cloudfront ? "https://${aws_cloudfront_distribution.api[0].domain_name}" : ""
}

output "bucket_front" {
  description = "A donde se suben los archivos del front (scripts/publicar.ps1 de palco-web)."
  value       = var.con_cloudfront ? aws_s3_bucket.front[0].bucket : ""
}

output "distribucion" {
  description = "Id de CloudFront, para refrescar el front despues de publicarlo."
  value       = var.con_cloudfront ? aws_cloudfront_distribution.api[0].id : ""
}
