# 0016 — Límites contra bots y fuerza bruta

Fecha: 2026-10-16 · Estado: aceptado

## Contexto

Hasta hoy cualquiera podía generar hold_id nuevos y apartar 8 butacas
con cada uno: un bot vaciaba el mapa durante 10 minutos, una y otra vez
(ADR 0007). Y el login aceptaba intentos sin fin. Son B1, B2 y B5 del
plan de blindaje, P0 para el piloto del 26 de octubre.

## Decisión

Tres defensas en Redis (`app/services/limites.py`):

**1. Límite de velocidad por IP** (ventana fija de 60 s):

| Grupo | Rutas | Por minuto |
|---|---|---|
| `login` | `POST /auth/login` | 10 |
| `apartar` | apartar y soltar butacas | 30 |
| `checkout` | iniciar el pago | 10 |
| `confirmar` | `POST /ordenes/{uuid}/confirmar` | 20 |
| `consulta` | consultar apartado, orden y boletos | 60 |
| `publico` | página pública del evento | 120 |

Al rebasarlo: 429 `demasiadas_solicitudes`, encabezado `Retry-After` y
un mensaje para personas. El webhook de Stripe no se limita.

**2. Acaparamiento:**

- Máximo **4 apartados vigentes por IP**. Sumar butacas a un apartado
  que ya cuenta siempre pasa; soltarlo completo libera el lugar. 429
  `demasiados_apartados`.
- Máximo **2 órdenes pendientes por correo** con apartados distintos.
  Repetir el pago del mismo apartado no cuenta. 409 `demasiadas_ordenes`.

**3. Candado de login por correo:** 5 fallos en 15 minutos bloquean la
cuenta 15 minutos, **aunque luego llegue la contraseña correcta**. Se
bloquea igual un correo que no existe: si no, el bloqueo delataría qué
cuentas existen. Entrar bien borra los fallos. Al bloquear se anota una
alerta en `palco.seguridad` con el dominio, no el correo completo.

**La IP real:** con `PROXIES_DE_CONFIANZA=0` se usa la del socket y se
ignora `X-Forwarded-For`, que cualquiera puede inventar. Detrás del
balanceador de AWS se pone 1 y se toma la entrada que agregó él, la
última de la cadena.

**Sin Redis se deja pasar** y se anota: estas defensas no deben tumbar
la venta.

**Fuera de `ENTORNO=local` no se pueden apagar**: la app no arranca con
`LIMITES_ACTIVOS=false`. En las pruebas se apagan por omisión y
`tests/test_limites.py` las enciende con su propio Redis.

## Consecuencias

- Las redes de celular comparten IP entre muchas personas (CGNAT). Por
  eso los topes son holgados: esto es la red de seguridad. La defensa
  principal contra bots es la verificación humana (B3) y el WAF (B4).
- Los valores viven en constantes del módulo. Si el piloto muestra que
  algún tope estorba a compradores reales, se ajusta ahí.
