# Infraestructura de Palco (I3)

La receta de AWS en Terraform. Un módulo (`modulos/palco`) y un entorno por
carpeta (`entornos/sandbox`, luego `entornos/produccion`): lo que se prueba
en sandbox es lo mismo que llega a producción, solo cambian los tamaños.

| Archivo del módulo | Qué crea |
|---|---|
| `red.tf` | VPC en 2 zonas: subredes públicas (balanceador, contenedores) y privadas (base, Valkey) |
| `seguridad.tf` | Quién habla con quién; todo lo demás cerrado |
| `datos.tf` | PostgreSQL 16 (RDS) y Valkey (ElastiCache), cifrados |
| `secretos.tf` | Parameter Store: los que genera Terraform y los que pone una persona |
| `imagenes.tf` | ECR con etiquetas inmutables |
| `balanceador.tf` | La única entrada; HTTPS cuando haya certificado |
| `contenedores.tf` | ECS Fargate: API (autoescala), barrido, bandeja y la tarea de migrar |

## Antes de la primera vez

1. `aws login --profile palco` (permisos temporales, hasta 12 h).
2. El perfil `palco-tf` (puente para Terraform), ver la guía.
3. El bucket del estado `palco-tfstate-530629633953` (una sola vez).

## Uso

```powershell
cd infra\entornos\sandbox
$env:AWS_PROFILE = "palco-tf"
terraform init
terraform plan  -var imagen_tag=<commit>
terraform apply -var imagen_tag=<commit>
```

## Reglas

- Nunca cambiar a mano en la consola algo que creó Terraform: el siguiente
  `apply` lo regresa (o falla). Se cambia aquí y se aplica.
- Los secretos que pone una persona se cambian con
  `aws ssm put-parameter --overwrite`, nunca en un archivo del repositorio.
- El estado tiene secretos: el bucket es privado, cifrado y versionado.
- Desplegar es cambiar `imagen_tag`. Antes, correr la migración
  (`terraform output comando_migrar`).
