terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
      # aws.correo: la region de SES (ver correo.tf).
      configuration_aliases = [aws.correo]
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}
