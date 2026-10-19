output "url_api" {
  description = "Donde responde la API."
  value       = "${local.con_https ? "https" : "http"}://${aws_lb.palco.dns_name}/api/v1"
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
    "--cluster ${aws_ecs_cluster.palco.name}",
    "--task-definition ${aws_ecs_task_definition.proceso["migrar"].family}",
    "--launch-type FARGATE",
    "--network-configuration 'awsvpcConfiguration={subnets=[${join(",", aws_subnet.publica[*].id)}],securityGroups=[${aws_security_group.contenedores.id}],assignPublicIp=ENABLED}'",
    "--query 'tasks[0].taskArn' --output text",
  ])
}
