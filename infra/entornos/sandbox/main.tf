# Sandbox de Palco: el mismo modulo que produccion, con tamanos chicos.
#
# Por ahora en Ohio (us-east-2) porque la cuenta esta en plan gratuito y
# este bloquea la region Mexico. Al pasar a plan de pago se cambia `region`
# y se vuelve a crear (el sandbox no guarda nada que haya que conservar).
#
# Uso (PowerShell, desde esta carpeta):
#   $env:AWS_PROFILE = "palco-tf"
#   terraform init
#   terraform plan  -var imagen_tag=<commit>
#   terraform apply -var imagen_tag=<commit>

terraform {
  required_version = ">= 1.10"

  backend "s3" {
    bucket       = "palco-tfstate-530629633953"
    key          = "sandbox/terraform.tfstate"
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
  description = "Commit corto de la imagen subida a ECR."
  type        = string
}

provider "aws" {
  region = "us-east-2"

  # Candado: este estado solo puede tocar esta cuenta.
  allowed_account_ids = ["530629633953"]

  default_tags {
    tags = {
      Proyecto = "Palco"
      Entorno  = "sandbox"
      Gestion  = "Terraform"
    }
  }
}

module "palco" {
  source = "../../modulos/palco"

  # SES si existe en Ohio: el correo sale de la misma region.
  providers = {
    aws        = aws
    aws.correo = aws
  }

  entorno     = "sandbox"
  entorno_app = "staging"
  cidr_vpc    = "10.20.0.0/16"

  imagen_tag   = var.imagen_tag
  arquitectura = "X86_64" # la imagen se arma en tu computadora; ARM cuando la arme el CI

  api        = { cpu = 256, memoria = 512, minimo = 1, maximo = 2 }
  trabajador = { cpu = 256, memoria = 512 }

  # db.t4g.micro acepta ~80 conexiones: 2 API + 2 trabajadores + migrar,
  # cada uno con hasta 10, quedan holgados.
  pool_db = { tamano = 5, reserva = 5 }

  postgres = {
    clase              = "db.t4g.micro"
    disco_gb           = 20
    disco_maximo_gb    = 50
    multi_az           = false
    dias_de_respaldo   = 1
    proteger_de_borrar = false
  }

  valkey = {
    clase          = "cache.t4g.micro"
    nodos          = 1
    dias_de_copias = 0
  }

  # La pagina de compra vive en la misma direccion de CloudFront que la API
  # (front_url vacio). localhost: el front corriendo en la computadora.
  cors_origenes      = ["http://localhost:5173"]
  correo_remitente   = "Palco Sandbox <juanmanuel@psdigital.mx>"
  turnstile_site_key = "1x00000000000000000000AA" # prueba de Cloudflare: aprueba siempre

  dias_de_registros        = 7
  permitir_borrar_imagenes = true
}

output "url_api" {
  value = module.palco.url_api
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

output "url_front" {
  value = module.palco.url_front
}

output "bucket_front" {
  value = module.palco.bucket_front
}

output "distribucion" {
  value = module.palco.distribucion
}
