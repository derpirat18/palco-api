#!/bin/bash
# Postgres ejecuta todo lo que encuentre en /docker-entrypoint-initdb.d/
# la primera vez que arranca con el volumen vacio.
#
# El contenedor ya crea palco_dev por POSTGRES_DB. Aqui se agrega la de
# pruebas, para que nadie tenga que crearla a mano.
set -e

# --dbname es obligatorio: sin el, psql intenta conectarse a una base
# con el mismo nombre del usuario ("palco"), que no existe. El error
# aborta la inicializacion y el contenedor se detiene.
psql -v ON_ERROR_STOP=1 \
     --username "$POSTGRES_USER" \
     --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE DATABASE palco_test OWNER $POSTGRES_USER;
EOSQL

echo "Base palco_test creada."
