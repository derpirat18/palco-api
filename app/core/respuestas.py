"""Respuestas JSON que declaran su codificacion.

`application/json` a secas deja que cada cliente adivine. Un navegador
asume UTF-8, pero PowerShell 5.1, algunas librerias y varios clientes
viejos asumen Latin-1, y entonces "Fila A · 1" llega como "Fila A Â· 1"
y "Mexico" con acento llega roto. Declararlo cuesta nada y quita la
adivinanza (#44).

Toda respuesta JSON de la API sale de aqui: las de exito, por ser la
clase por omision de la app, y las de error, porque los handlers de
app/main.py la usan explicitamente.
"""

from fastapi.responses import JSONResponse

TIPO_JSON = "application/json; charset=utf-8"


class JSONUtf8(JSONResponse):
    media_type = TIPO_JSON
