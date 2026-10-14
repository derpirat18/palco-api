"""Errores de dominio.

Se traducen a respuestas HTTP en app/main.py. Los handlers de la API no
arman respuestas de error a mano: levantan estas excepciones.
"""


class ErrorDeDominio(Exception):
    codigo = "error_de_dominio"
    http_status = 400

    def __init__(self, mensaje: str, detalle: dict | None = None) -> None:
        super().__init__(mensaje)
        self.mensaje = mensaje
        self.detalle = detalle or {}


class AsientoNoDisponible(ErrorDeDominio):
    codigo = "asiento_no_disponible"
    http_status = 409


class ApartadoExpirado(ErrorDeDominio):
    codigo = "apartado_expirado"
    http_status = 409


class ApartadoAjeno(ErrorDeDominio):
    codigo = "apartado_ajeno"
    http_status = 403


class LimiteDeAsientos(ErrorDeDominio):
    codigo = "limite_de_asientos"
    http_status = 422


class RecursoNoEncontrado(ErrorDeDominio):
    codigo = "no_encontrado"
    http_status = 404


# Mensajes de "no existe". Los usan los servicios cuando el recurso no esta
# y `verificar_acceso_cliente` cuando es de otro organizador: tienen que ser
# el mismo texto para que las dos respuestas no se distingan.
ORGANIZADOR_NO_EXISTE = "El organizador no existe."
EVENTO_NO_EXISTE = "El evento no existe."
FUNCION_NO_EXISTE = "La funcion no existe."


class FuncionNoALaVenta(ErrorDeDominio):
    """La funcion existe y es publica, pero hoy no se le puede comprar."""

    codigo = "funcion_no_a_la_venta"
    http_status = 409


class DejaButacaSuelta(ErrorDeDominio):
    """La seleccion dejaria una butaca libre sola en su fila."""

    codigo = "deja_butaca_suelta"
    http_status = 409
