# Produccion de Palco: el mismo modulo que el sandbox, con tamanos para
# vender y protecciones encendidas.
#
# Region: Mexico (mx-central-1) por omision; Ohio (us-east-2) si Mexico no
# tiene algo que usamos (scripts\revisar_region.ps1 lo revisa antes).
# Cambiar de region despues de vender es mudar la base: se decide una vez.
#
# El correo sale siempre de Ohio: SES no existe en Mexico.
#
# Antes de la primera vez (ver infra/README.md, "Produccion"):
#   - cuenta en plan de pago y la region Mexico activada
#   - .\scripts\revisar_region.ps1 -Region mx-central-1
#   - remitente verificado en SES de Ohio y SES fuera del modo de pruebas
#   - llaves de Turnstile de produccion (Cloudflare)
#
# Uso (PowerShell, desde esta carpeta):
#   $env:AWS_PROFILE = "palco-tf"
#   terraform init
#   terraform plan  -var imagen_tag=<commit> -out plan.tfplan
#   terraform apply plan.tfplan

terraform {
  required_version = ">= 1.10"

  # El mismo bucket del estado que el sandbox (vive en Ohio), otra llave.
  backend "s3" {
    bucket       = "palco-tfstate-530629633953"
    key          = "produccion/terraform.tfstate"
    region       = "us-east-2"
    encrypt      = true
    use_lockfile = true
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

variable "imagen_tag" {
  description = "Commit corto de la imagen subida al ECR de produccion."
  type        = string
}

variable "region" {
  description = "Donde vive produccion. Se decide una vez, antes de vender."
  type        = string
  default     = "mx-central-1"

  validation {
    condition     = contains(["mx-central-1", "us-east-2"], var.region)
    error_message = "region debe ser mx-central-1 (Mexico) o us-east-2 (Ohio)."
  }
}

variable "correo_remitente" {
  description = "Remitente de los boletos, p. ej. \"PSD Boletera <boletos@psdigital.mx>\". Verificado en SES de Ohio."
  type        = string
}

variable "turnstile_site_key" {
  description = "Llave publica de Turnstile de produccion (Cloudflare). Nunca la de prueba."
  type        = string

  validation {
    # Las de prueba de Cloudflare empiezan con 1x, 2x o 3x y aprueban o
    # rechazan siempre.
    condition     = !can(regex("^[123]x0000", var.turnstile_site_key))
    error_message = "Esa es una llave de prueba de Cloudflare: en produccion va la real."
  }
}

locals {
  etiquetas = {
    Proyecto = "Palco"
    Entorno  = "produccion"
    Gestion  = "Terraform"
  }
}

provider "aws" {
  region = var.region

  # Candado: este estado solo puede tocar esta cuenta.
  allowed_account_ids = ["530629633953"]

  default_tags {
    tags = local.etiquetas
  }
}

# Para SES (correo.tf): no existe en Mexico.
provider "aws" {
  alias  = "ohio"
  region = "us-east-2"

  allowed_account_ids = ["530629633953"]

  default_tags {
    tags = local.etiquetas
  }
}

module "palco" {
  source = "../../modulos/palco"

  providers = {
    aws        = aws
    aws.correo = aws.ohio
  }

  entorno     = "produccion"
  entorno_app = "prod"
  # Distinto del sandbox (10.20) para poder unirlas algun dia.
  cidr_vpc = "10.30.0.0/16"

  imagen_tag   = var.imagen_tag
  arquitectura = "X86_64" # la imagen se arma en tu computadora

  # Dos copias siempre: si una se cae o se despliega, la otra atiende.
  api        = { cpu = 512, memoria = 1024, minimo = 2, maximo = 4 }
  trabajador = { cpu = 256, memoria = 512 }

  # db.t4g.small acepta ~180 conexiones: 4 API + 2 trabajadores + migrar,
  # cada uno con hasta 10, son 70.
  pool_db = { tamano = 5, reserva = 5 }

  postgres = {
    clase           = "db.t4g.small"
    disco_gb        = 20
    disco_maximo_gb = 100
    # Una sola zona por costo (~25 USD/mes mas en dos). Los respaldos de 7
    # dias permiten recuperar; pasar a true cuando las ventas lo paguen.
    multi_az           = false
    dias_de_respaldo   = 7
    proteger_de_borrar = true
  }

  # Los apartados viven minutos: si Valkey se reinicia se pierden los que
  # estaban en vuelo, nunca una venta (la verdad esta en Postgres).
  valkey = {
    clase          = "cache.t4g.micro"
    nodos          = 1
    dias_de_copias = 0
  }

  # Solo el dominio de CloudFront (el mismo de la API); sin localhost.
  cors_origenes      = []
  correo_remitente   = var.correo_remitente
  turnstile_site_key = var.turnstile_site_key

  dias_de_registros        = 30
  permitir_borrar_imagenes = false
}

output "url_api" {
  value = module.palco.url_api
}

output "url_front" {
  value = module.palco.url_front
}

output "repositorio_imagenes" {
  value = module.palco.repositorio_imagenes
}

output "cluster" {
  value = module.palco.cluster
}

output "ruta_secretos" {
  value = module.palco.ruta_secretos
}

output "comando_migrar" {
  value = module.palco.comando_migrar
}

output "bucket_front" {
  value = module.palco.bucket_front
}

output "distribucion" {
  value = module.palco.distribucion
}
