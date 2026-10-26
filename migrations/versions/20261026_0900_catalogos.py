"""Catálogos de ciudades (GEO1) y categorías de eventos (CAT1).

- `cities`: sembrada con las ciudades principales de cada estado de México
  (app/core/geo_mx.py). Los recintos que ya existen se acomodan: «CDMX»
  pasa a «Ciudad de México» y se les pone el estado si se puede deducir.
- `event_categories`: sembrada con las categorías comunes y las que ya
  usan los eventos; las que solo cambian en mayúsculas o acentos se juntan
  en una.

Solo agrega y acomoda datos: el código anterior sigue funcionando.

Revision ID: 2b6e8d4f1c73
Revises: 7d3f9a2c4e81
Create Date: 2026-10-26 09:00:00-06:00
"""

from collections import Counter, defaultdict
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.core.geo_mx import ESTADOS_MX, clave, estado_canonico, ubicar

revision: str = "2b6e8d4f1c73"
down_revision: str | None = "7d3f9a2c4e81"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CATEGORIAS_BASE = [
    "Circo",
    "Teatro",
    "Conciertos",
    "Familiares",
    "Comedia",
    "Danza",
    "Ópera",
    "Deportes",
    "Festivales",
    "Exposiciones",
    "Conferencias",
]


def upgrade() -> None:
    ciudades = op.create_table(
        "cities",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("pais", sa.String(2), nullable=False),
        sa.Column("estado", sa.String(80), nullable=False),
        sa.Column("nombre", sa.String(80), nullable=False),
        sa.Column("clave", sa.String(200), nullable=False),
        sa.Column("agregada_por", sa.BigInteger(), nullable=True),
        sa.Column(
            "agregada_en",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["agregada_por"],
            ["users.id"],
            name=op.f("fk_cities_agregada_por_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cities")),
        sa.UniqueConstraint("clave", name=op.f("uq_cities_clave")),
    )
    op.create_index("ix_cities_pais_estado", "cities", ["pais", "estado"])

    categorias = op.create_table(
        "event_categories",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("nombre", sa.String(60), nullable=False),
        sa.Column("clave", sa.String(60), nullable=False),
        sa.Column("estado", sa.String(12), nullable=False),
        sa.Column("solicitada_por", sa.BigInteger(), nullable=True),
        sa.Column("para_evento_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "solicitada_en",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("revisada_por", sa.BigInteger(), nullable=True),
        sa.Column("revisada_en", sa.DateTime(timezone=True), nullable=True),
        sa.Column("motivo_rechazo", sa.String(200), nullable=True),
        sa.CheckConstraint(
            "estado IN ('aprobada','solicitada','rechazada')",
            name=op.f("ck_event_categories_estado_valido"),
        ),
        sa.ForeignKeyConstraint(
            ["solicitada_por"],
            ["users.id"],
            name=op.f("fk_event_categories_solicitada_por_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["revisada_por"],
            ["users.id"],
            name=op.f("fk_event_categories_revisada_por_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["para_evento_id"],
            ["events.id"],
            name=op.f("fk_event_categories_para_evento_id_events"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_event_categories")),
        sa.UniqueConstraint("clave", name=op.f("uq_event_categories_clave")),
    )

    con = op.get_bind()

    # --- Ciudades: la semilla y los recintos que ya existen.
    filas: dict[str, dict] = {}
    for estado, nombres in ESTADOS_MX.items():
        for nombre in nombres:
            filas[clave("MX", estado, nombre)] = {
                "pais": "MX",
                "estado": estado,
                "nombre": nombre,
                "clave": clave("MX", estado, nombre),
            }
    recintos = con.execute(
        sa.text("SELECT id, pais, estado_geo, ciudad FROM venues")
    ).fetchall()
    for vid, pais, estado_geo, ciudad in recintos:
        if pais != "MX":
            continue
        lugar = ubicar(ciudad, estado_geo)
        if lugar is None and estado_canonico(estado_geo):
            # Una ciudad que no está en la semilla, con su estado: se agrega.
            lugar = (estado_canonico(estado_geo), " ".join(ciudad.split()))
            k = clave("MX", *lugar)
            filas.setdefault(
                k, {"pais": "MX", "estado": lugar[0], "nombre": lugar[1], "clave": k}
            )
        if lugar is not None:
            con.execute(
                sa.text("UPDATE venues SET estado_geo = :e, ciudad = :c WHERE id = :id"),
                {"e": lugar[0], "c": lugar[1], "id": vid},
            )
    op.bulk_insert(ciudades, list(filas.values()))

    # --- Categorías: las comunes y las que ya se usan, sin repetidas.
    usadas = con.execute(
        sa.text("SELECT categoria FROM events WHERE categoria IS NOT NULL")
    ).fetchall()
    por_clave: dict[str, Counter] = defaultdict(Counter)
    for nombre in CATEGORIAS_BASE:
        por_clave[clave(nombre)][nombre] += 1000  # la forma de la lista gana
    for (nombre,) in usadas:
        limpio = " ".join(nombre.split())
        if limpio:
            limpio = limpio[0].upper() + limpio[1:]
            por_clave[clave(limpio)][limpio] += 1
    canonicas = {k: c.most_common(1)[0][0] for k, c in por_clave.items()}
    op.bulk_insert(
        categorias,
        [{"nombre": n, "clave": k, "estado": "aprobada"} for k, n in canonicas.items()],
    )
    for (nombre,) in usadas:
        k = clave(nombre)
        if k in canonicas and canonicas[k] != nombre:
            con.execute(
                sa.text("UPDATE events SET categoria = :n WHERE categoria = :viejo"),
                {"n": canonicas[k], "viejo": nombre},
            )


def downgrade() -> None:
    op.drop_table("event_categories")
    op.drop_index("ix_cities_pais_estado", table_name="cities")
    op.drop_table("cities")
