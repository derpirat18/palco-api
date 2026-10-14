# 0001 — Stack: FastAPI, Postgres, Redis, React

Fecha: 2026-10-08 · Estado: aceptado

## Contexto

Boletera con butaca numerada, picos de concurrencia al abrir ventas y
control de acceso sin conexión. El equipo que la mantendrá viene de PHP
(CodeIgniter y Laravel), Java y React.

## Decisión

FastAPI con SQLAlchemy 2.0 async, PostgreSQL y Redis. Front en React con
TypeScript, en repositorio aparte, consumiendo API REST.

## Por qué

La ruta crítica es I/O puro bajo concurrencia, que es donde async rinde.
FastAPI genera el esquema OpenAPI solo, y de ahí sale el cliente
tipado de TypeScript, lo que elimina una clase entera de errores de
integración entre front y back.

Se consideró Django con DRF. Su mayor ventaja habría sido el admin
automático, pero el panel ya está diseñado a medida en React, así que esa
ventaja no aplica.

## Costo asumido

Ni Mario ni Pedro vienen de Python. La curva es corta desde PHP y Java, y
FastAPI es de los frameworks más legibles, pero no es cero.
