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
