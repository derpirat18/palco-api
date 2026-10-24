"""Configuracion de la aplicacion.

Todo valor configurable entra por variable de entorno. Nunca hardcodear
credenciales, llaves de Stripe ni secretos de firma en el codigo.
"""

import re
from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, RedisDsn, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Llaves de prueba de Cloudflare Turnstile: 1x... aprueba, 2x... rechaza,
# 3x... responde "token ya usado". Todas tienen esta forma.
_ES_LLAVE_DE_PRUEBA = re.compile(r"^[0-9]x0{20,}A{2}$")

COMO_GENERAR_LLAVE = (
    'Genera una con: python -c "import secrets; print(secrets.token_urlsafe(48))"'
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- Entorno ---
    ENTORNO: Literal["local", "dev", "staging", "prod"] = "local"
    DEBUG: bool = False
    APP_NAME: str = "Palco Boletera"
    API_V1_PREFIX: str = "/api/v1"

    # --- Base de datos ---
    DATABASE_URL: PostgresDsn
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_ECHO: bool = False
    # Cifrar la conexion a PostgreSQL. RDS la exige (rds.force_ssl=1 desde
    # PostgreSQL 15); el Postgres local de Docker no la ofrece.
    DB_SSL: bool = False

    # --- Redis ---
    REDIS_URL: RedisDsn
    # Cada corrutina concurrente toma una conexion del pool. Con miles
    # de compradores simultaneos un pool chico se agota y las peticiones
    # fallan con MaxConnectionsError antes de llegar a Redis.
    REDIS_MAX_CONNECTIONS: int = 200
    # Segundos que una peticion espera por una conexion libre antes de
    # rendirse. Esperar un poco es mejor que fallarle al comprador.
    REDIS_POOL_TIMEOUT: int = 20

    # --- Apartados ---
    # Minutos que una butaca queda apartada mientras el comprador decide.
    HOLD_TTL_SEGUNDOS: int = 600
    # Se extiende al entrar al checkout para cubrir el tiempo de pago.
    HOLD_TTL_CHECKOUT_SEGUNDOS: int = 900
    # Tope de butacas por orden. Defensa basica contra acaparamiento.
    MAX_ASIENTOS_POR_ORDEN: int = 8
    # Tarjetas rechazadas por orden. Al llegar al tope se cancela la compra y
    # los lugares vuelven a la venta (PSD, 20 de octubre de 2026).
    MAX_RECHAZOS_POR_ORDEN: int = 2

    # --- Seguridad ---
    SECRET_KEY: str = Field(min_length=32)
    # Llave independiente para firmar el QR de los boletos. Si se rota,
    # los boletos emitidos dejan de validar: rotar solo entre temporadas.
    TICKET_SIGNING_KEY: str = Field(min_length=32)
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 8

    # --- Stripe ---
    STRIPE_SECRET_KEY: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""
    # La publica (pk_test_/pk_live_) la necesita el front para montar el
    # formulario de tarjeta. Se entrega en la respuesta del checkout para
    # que el front no la tenga escrita en su codigo.
    STRIPE_PUBLISHABLE_KEY: str = ""

    # --- CORS ---
    CORS_ORIGINS: list[str] = ["http://localhost:5173"]

    # --- Limites contra bots y fuerza bruta (ADR 0016) ---
    # Apagarlos solo en pruebas; fuera de local siempre estan encendidos.
    LIMITES_ACTIVOS: bool = True
    # Cuantos proxies de confianza hay delante de la API (balanceador,
    # CDN). Con 0 se ignora X-Forwarded-For: cualquiera puede escribirlo.
    PROXIES_DE_CONFIANZA: int = 0

    # --- Verificacion humana: Cloudflare Turnstile (ADR 0019) ---
    # Sin la secreta, la verificacion esta apagada (local y pruebas). La
    # publica (sitekey) la lee el front de /publico/configuracion.
    # Para el sandbox sirven las de prueba de Cloudflare: secreta
    # 1x0000000000000000000000000000000AA y sitekey 1x00000000000000000000AA.
    TURNSTILE_SECRET_KEY: str = ""
    TURNSTILE_SITE_KEY: str = ""

    # --- Proteccion (B12) ---
    # Tope de una peticion. Un mapa de 50,000 butacas en CSV cabe holgado.
    MAX_CUERPO_BYTES: int = 4 * 1024 * 1024

    # --- Sala de espera (ADR 0017) ---
    # Apagada, la puerta del turno no consulta nada. Encendida, exige pase
    # solo en los eventos con la sala activa.
    SALA_DE_ESPERA_HABILITADA: bool = False

    # --- Front ---
    # Base de los enlaces que salen en correos ("Ver mis boletos").
    FRONT_URL: str = "http://localhost:5173"

    # --- Correo (ADR 0015) ---
    # SMTP sirve igual para el buzon local (Mailpit, puerto 1025, sin
    # usuario) que para Amazon SES (puerto 587, STARTTLS, usuario SMTP).
    CORREO_SMTP_HOST: str = "localhost"
    CORREO_SMTP_PUERTO: int = 1025
    CORREO_SMTP_USUARIO: str = ""
    CORREO_SMTP_PASSWORD: str = ""
    CORREO_SMTP_STARTTLS: bool = False
    CORREO_REMITENTE: str = "Palco <boletos@palco.local>"
    # Lo que sale en el correo de confirmacion (plantilla de Nadia). Lo que
    # quede vacio no se muestra: mejor sin renglon que con "[Razon social]".
    MARCA: str = "PSD Boletera"
    SOPORTE_EMAIL: str = ""
    EMPRESA_RAZON_SOCIAL: str = ""
    EMPRESA_DOMICILIO: str = ""
    # Las mismas del pie del sitio (palco-web, src/lib/enlaces.ts). En el
    # .env: REDES_SOCIALES='[{"nombre":"Instagram","icono":"instagram",
    # "url":"https://..."}]'. Iconos: instagram, facebook, tiktok, x, youtube.
    REDES_SOCIALES: list[dict[str, str]] = [
        {"nombre": "Instagram", "icono": "instagram", "url": "https://www.instagram.com/"},
        {"nombre": "Facebook", "icono": "facebook", "url": "https://www.facebook.com/"},
        {"nombre": "TikTok", "icono": "tiktok", "url": "https://www.tiktok.com/"},
        {"nombre": "X", "icono": "x", "url": "https://x.com/"},
        {"nombre": "YouTube", "icono": "youtube", "url": "https://www.youtube.com/"},
    ]  # fmt: skip

    @field_validator("SECRET_KEY", "TICKET_SIGNING_KEY")
    @classmethod
    def _rechazar_relleno(cls, valor: str, info) -> str:
        """Los valores de .env.example pasan el largo minimo.

        Sin esta validacion se puede desarrollar meses con la llave de
        ejemplo sin notarlo, y esa llave esta en el repositorio: cualquiera
        que la lea podria firmar un QR valido.
        """
        if valor.lower().startswith("cambiame"):
            raise ValueError(
                f"{info.field_name} sigue con el valor de ejemplo de "
                f".env.example. {COMO_GENERAR_LLAVE}"
            )
        return valor

    @field_validator(
        "STRIPE_SECRET_KEY", "STRIPE_PUBLISHABLE_KEY", "STRIPE_WEBHOOK_SECRET"
    )
    @classmethod
    def _rechazar_relleno_de_stripe(cls, valor: str, info) -> str:
        """`sk_test_xxx` de .env.example parece una llave y no lo es.

        Con el relleno la app arranca y el primer cobro falla con un 502
        que no dice por que. Vacia, la app arranca y el checkout dice
        claramente que falta configurar Stripe.
        """
        if valor.strip().lower().endswith("_xxx"):
            donde = (
                "la configuracion del webhook en Stripe, o la que imprime "
                "`stripe listen`"
                if info.field_name == "STRIPE_WEBHOOK_SECRET"
                else "tu panel de Stripe (Developers -> API keys)"
            )
            raise ValueError(
                f"{info.field_name} sigue con el valor de ejemplo de "
                f".env.example. Pon la de {donde}, o deja la variable vacia."
            )
        return valor.strip()

    @model_validator(mode="after")
    def _llaves_distintas(self) -> "Settings":
        """Una sola llave para sesiones y boletos acopla dos riesgos.

        Si se filtrara la de sesion, el atacante podria ademas falsificar
        boletos de entrada.
        """
        if self.SECRET_KEY == self.TICKET_SIGNING_KEY:
            raise ValueError(
                "SECRET_KEY y TICKET_SIGNING_KEY deben ser distintas. "
                f"{COMO_GENERAR_LLAVE}"
            )
        return self

    @model_validator(mode="after")
    def _limites_fuera_de_local(self) -> "Settings":
        """Sin limites, un bot acapara el mapa o adivina contrasenas."""
        if self.ENTORNO != "local" and not self.LIMITES_ACTIVOS:
            raise ValueError("Fuera de ENTORNO=local, LIMITES_ACTIVOS debe ser true.")
        return self

    @model_validator(mode="after")
    def _correo_cifrado_fuera_de_local(self) -> "Settings":
        """Fuera de local el correo lleva datos del comprador y sus boletos.

        Mandarlo sin cifrar al proveedor expone el QR a cualquiera en la
        red. En local se permite porque el buzon de pruebas no cifra.
        """
        if self.ENTORNO != "local" and not self.CORREO_SMTP_STARTTLS:
            raise ValueError(
                "Fuera de ENTORNO=local el correo debe ir cifrado: "
                "CORREO_SMTP_STARTTLS=true."
            )
        return self

    @field_validator("TURNSTILE_SECRET_KEY", "TURNSTILE_SITE_KEY")
    @classmethod
    def _sin_espacios(cls, valor: str) -> str:
        return valor.strip()

    @model_validator(mode="after")
    def _verificacion_humana(self) -> "Settings":
        """Las dos llaves van juntas, y produccion no usa las de prueba.

        Con solo la secreta, el servidor exige una verificacion que el
        front no sabe hacer y nadie podria comprar. Las llaves de prueba
        de Cloudflare aprueban siempre: en produccion dejarian pasar a
        cualquier bot.
        """
        secreta, publica = self.TURNSTILE_SECRET_KEY, self.TURNSTILE_SITE_KEY
        if bool(secreta) != bool(publica):
            raise ValueError(
                "TURNSTILE_SECRET_KEY y TURNSTILE_SITE_KEY van juntas: "
                "las dos o ninguna."
            )
        if self.ENTORNO != "local" and not secreta:
            raise ValueError(
                "Fuera de ENTORNO=local la verificacion humana es obligatoria: "
                "configura TURNSTILE_SECRET_KEY y TURNSTILE_SITE_KEY."
            )
        if self.ENTORNO == "prod" and _ES_LLAVE_DE_PRUEBA.match(secreta):
            raise ValueError(
                "En produccion TURNSTILE_SECRET_KEY no puede ser una llave de "
                "prueba de Cloudflare: esas aprueban a cualquiera."
            )
        return self

    @model_validator(mode="after")
    def _base_cifrada_fuera_de_local(self) -> "Settings":
        """Fuera de local la base guarda datos de compradores y ordenes."""
        if self.ENTORNO != "local" and not self.DB_SSL:
            raise ValueError("Fuera de ENTORNO=local, DB_SSL debe ser true.")
        return self

    @property
    def db_connect_args_async(self) -> dict:
        """Argumentos de conexion para asyncpg (la app)."""
        return {"ssl": "require"} if self.DB_SSL else {}

    @property
    def db_connect_args_sync(self) -> dict:
        """Argumentos de conexion para psycopg (Alembic)."""
        return {"sslmode": "require"} if self.DB_SSL else {}

    @property
    def db_url_async(self) -> str:
        return str(self.DATABASE_URL).replace("postgresql://", "postgresql+asyncpg://")

    @property
    def db_url_sync(self) -> str:
        """Alembic corre en modo sincrono."""
        return str(self.DATABASE_URL).replace("postgresql+asyncpg://", "postgresql://")


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


settings = get_settings()
