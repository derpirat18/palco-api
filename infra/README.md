# Infraestructura de Palco (I3)

La receta de AWS en Terraform. Un módulo (`modulos/palco`) y un entorno por
carpeta (`entornos/sandbox`, luego `entornos/produccion`): lo que se prueba
en sandbox es lo mismo que llega a producción, solo cambian los tamaños.

| Archivo del módulo | Qué crea |
|---|---|
| `red.tf` | VPC en 2 zonas: subredes públicas (balanceador, contenedores) y privadas (base, Valkey) |
| `seguridad.tf` | Quién habla con quién; todo lo demás cerrado |
| `datos.tf` | PostgreSQL 16 (RDS) y Valkey (ElastiCache), cifrados |
| `correo.tf` | Usuario de SES que solo puede mandar desde el remitente; su llave SMTP va directo a Parameter Store |
| `secretos.tf` | Parameter Store: los que genera Terraform y los que pone una persona |
| `imagenes.tf` | ECR con etiquetas inmutables |
| `cdn.tf` | CloudFront: una sola dirección HTTPS para la página de compra (archivos en S3) y la API (`/api/*`, sin caché) |
| `balanceador.tf` | Detrás de CloudFront, solo atiende a nuestra distribución (encabezado secreto); HTTPS cuando haya certificado |
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

## Dominio

`psdevents.com`, con su zona en Route 53. En GoDaddy solo se cambiaron los
servidores de nombres por los de la zona. `entornos/dominio` guarda lo que es
de la cuenta y comparten los dos entornos:

- el certificado de `psdevents.com` y `*.psdevents.com` en Virginia (CloudFront
  solo toma certificados de ahi);
- el dominio como remitente en SES de Ohio: DKIM, MAIL FROM (`correo.psdevents.com`)
  y DMARC.

Cada entorno pone sus nombres con la variable `dominio` del modulo:

| Entorno | Pagina | Balanceador (origen de CloudFront) |
|---|---|---|
| sandbox | `pruebas.psdevents.com` | `origen-pruebas.psdevents.com` |
| produccion | `psdevents.com` (`www` redirige) | `origen.psdevents.com` |

Con dominio, de CloudFront al balanceador tambien va por HTTPS. La direccion
`*.cloudfront.net` sigue sirviendo.

```powershell
cd infra\entornos\dominio
$env:AWS_PROFILE = "palco-tf"
terraform init
terraform plan -out plan.tfplan
terraform apply plan.tfplan
```

Se corre una vez, cuando el dominio ya responde con los servidores de AWS
(`Resolve-DnsName psdevents.com -Type NS -Server 8.8.8.8`); antes, el certificado
no se valida. Despues, los entornos.

## Produccion

`entornos/produccion`: el mismo modulo con tamanos para vender (2 copias de la API,
`db.t4g.small`, 7 dias de respaldo, proteccion contra borrado). Region: Mexico
(`mx-central-1`) por omision u Ohio (`-var region=us-east-2`). El correo sale
siempre de Ohio porque SES no existe en Mexico (proveedor `aws.correo`).

Antes de la primera vez:

1. Cuenta en plan de pago y la region Mexico activada (Cuenta -> Regiones).
2. `.\scripts\revisar_region.ps1 -Region mx-central-1`: si algo sale FALTA, Ohio.
3. `entornos/dominio` aplicado y SES de Ohio fuera del modo de pruebas.
4. Llaves de Turnstile de produccion en Cloudflare.

```powershell
cd infra\entornos\produccion
$env:AWS_PROFILE = "palco-tf"
terraform init
terraform plan -var imagen_tag=<commit> -var correo_remitente="PSD Boletera <boletos@psdevents.com>" -var turnstile_site_key=<llave> -out plan.tfplan
terraform apply plan.tfplan
```

La imagen va al ECR de produccion: `.\scripts\subir_imagen.ps1 -Region mx-central-1 -Repositorio palco-produccion`.
