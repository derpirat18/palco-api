# Todo lo que cambia entre sandbox y produccion entra por aqui. El resto
# del modulo es igual para los dos: lo que se prueba en sandbox es lo que
# llega a produccion.

variable "entorno" {
  description = "sandbox o produccion. Va en nombres, etiquetas y rutas de secretos."
  type        = string

  validation {
    condition     = contains(["sandbox", "produccion"], var.entorno)
    error_message = "entorno debe ser sandbox o produccion."
  }
}

variable "entorno_app" {
  description = "Valor de ENTORNO para la app (staging en sandbox, prod en produccion)."
  type        = string
}

# --- Red ---------------------------------------------------------------------

variable "cidr_vpc" {
  description = "Rango de IPs privadas de la red. Distinto por entorno para poder unirlas algun dia."
  type        = string
}

# --- Imagen y contenedores -----------------------------------------------------

variable "imagen_tag" {
  description = "Etiqueta de la imagen en ECR (el commit corto). Cambiarla es desplegar."
  type        = string
}

variable "arquitectura" {
  description = "X86_64 o ARM64 (Graviton, 20 % mas barato). Debe coincidir con la imagen."
  type        = string
  default     = "X86_64"

  validation {
    condition     = contains(["X86_64", "ARM64"], var.arquitectura)
    error_message = "arquitectura debe ser X86_64 o ARM64."
  }
}

variable "api" {
  description = "Tamano y copias de la API. cpu en unidades de Fargate (256 = 0.25 vCPU), memoria en MB."
  type = object({
    cpu     = number
    memoria = number
    minimo  = number
    maximo  = number
  })
}

variable "trabajador" {
  description = "Tamano de cada trabajador (barrido y bandeja)."
  type = object({
    cpu     = number
    memoria = number
  })
}

variable "pool_db" {
  description = "Conexiones por proceso a PostgreSQL. (copias x (tamano + reserva)) no debe pasar el limite de la base."
  type = object({
    tamano  = number
    reserva = number
  })
}

# --- Datos -------------------------------------------------------------------

variable "postgres" {
  description = "Instancia de PostgreSQL."
  type = object({
    clase              = string
    disco_gb           = number
    disco_maximo_gb    = number
    multi_az           = bool
    dias_de_respaldo   = number
    proteger_de_borrar = bool
  })
}

variable "valkey" {
  description = "Redis compatible (Valkey). nodos = 1 sin replica; 2 = primario + replica en otra zona."
  type = object({
    clase          = string
    nodos          = number
    dias_de_copias = number
  })
}

# --- Aplicacion --------------------------------------------------------------

variable "front_url" {
  description = "Base de las ligas de los correos (Ver mis boletos). Vacio: la direccion de CloudFront, donde vive el front."
  type        = string
  default     = ""
}

variable "cors_origenes" {
  description = "Otros dominios que llaman a la API desde el navegador (el front en desarrollo). El de CloudFront no hace falta: es el mismo dominio."
  type        = list(string)
  default     = []
}

variable "correo_remitente" {
  description = "Remitente de los correos. En SES debe ser una identidad verificada."
  type        = string
}

variable "turnstile_site_key" {
  description = "Llave publica de Turnstile. En sandbox sirve la de prueba de Cloudflare."
  type        = string
}

variable "con_cloudfront" {
  description = "CloudFront delante del balanceador: HTTPS sin dominio propio y el balanceador cerrado al publico."
  type        = bool
  default     = true
}

variable "certificado_arn" {
  description = "Certificado de ACM para HTTPS. Vacio = solo HTTP (sandbox sin dominio todavia)."
  type        = string
  default     = ""
}

variable "dias_de_registros" {
  description = "Cuantos dias se guardan los registros en CloudWatch."
  type        = number
}

variable "permitir_borrar_imagenes" {
  description = "true en sandbox: destruir el entorno borra tambien las imagenes."
  type        = bool
  default     = false
}
