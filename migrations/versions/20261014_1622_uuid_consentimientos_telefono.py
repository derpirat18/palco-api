"""UUID publicos, consentimientos como registro y telefono en E.164

Lo que el documento de compatibilidad con el CRM marco como "antes de la
primera venta": despues son migraciones sobre datos vivos.

Revisado a mano. El autogenerado borraba `acepta_marketing` sin rescatar
nada, no traia el CHECK del telefono y recortaba la columna sin normalizar
lo que ya hubiera.

Revision ID: 3a0ce722e84a
Revises: 0564b8628c29
Create Date: 2026-10-14 16:22:21.116777-06:00
"""

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3a0ce722e84a"
down_revision: str | None = "0564b8628c29"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLAS_CON_UUID = ("clients", "events", "performances", "customers", "orders", "tickets")
PATRON_E164 = r"^\+[1-9][0-9]{7,14}$"


def upgrade() -> None:
    # 1. UUID publicos. Postgres llena las filas que ya existen, una distinta
    #    por fila, porque gen_random_uuid() se evalua por renglon.
    for tabla in TABLAS_CON_UUID:
        op.add_column(
            tabla,
            sa.Column(
                "uuid",
                sa.UUID(),
                server_default=sa.text("gen_random_uuid()"),
                nullable=False,
            ),
        )
        op.create_unique_constraint(op.f(f"uq_{tabla}_uuid"), tabla, ["uuid"])

    # 2. Consentimientos como registro.
    op.create_table(
        "consents",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("customer_id", sa.BigInteger(), nullable=False),
        sa.Column("finalidad", sa.String(length=20), nullable=False),
        sa.Column("otorgado", sa.Boolean(), nullable=False),
        sa.Column("origen", sa.String(length=40), nullable=False),
        sa.Column("aviso_version", sa.String(length=20), nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "registrado_en",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "finalidad IN ('operacion','marketing')",
            name=op.f("ck_consents_finalidad_valida"),
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            name=op.f("fk_consents_customer_id_customers"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name=op.f("fk_consents_order_id_orders"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_consents")),
    )
    op.create_index(
        "ix_consents_vigente",
        "consents",
        ["customer_id", "finalidad", "registrado_en"],
        unique=False,
    )

    # Rescate de lo que ya hubiera: un "si" de marketing pasa a la bitacora.
    # Origen y aviso quedan marcados como anteriores: no se inventa lo que
    # no se guardo.
    op.execute(
        """
        INSERT INTO consents
            (customer_id, finalidad, otorgado, origen, aviso_version, registrado_en)
        SELECT id, 'marketing', true, 'migracion', 'anterior', creado_en
        FROM customers
        WHERE acepta_marketing
        """
    )
    op.drop_column("customers", "acepta_marketing")

    # 3. Telefonos en E.164. Lo que ya exista se normaliza; lo que no se
    #    pueda normalizar detiene la migracion en lugar de perderse.
    _normalizar_telefonos_existentes()
    op.alter_column(
        "customers",
        "telefono",
        existing_type=sa.VARCHAR(length=40),
        type_=sa.String(length=16),
        existing_nullable=True,
    )
    op.create_check_constraint(
        op.f("ck_customers_telefono_e164"),
        "customers",
        f"telefono IS NULL OR telefono ~ '{PATRON_E164}'",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_customers_telefono_e164"), "customers", type_="check")
    op.alter_column(
        "customers",
        "telefono",
        existing_type=sa.String(length=16),
        type_=sa.VARCHAR(length=40),
        existing_nullable=True,
    )

    # El booleano vuelve con el consentimiento de marketing vigente.
    op.add_column(
        "customers",
        sa.Column(
            "acepta_marketing",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    op.execute(
        """
        UPDATE customers c SET acepta_marketing = v.otorgado
        FROM (
            SELECT DISTINCT ON (customer_id) customer_id, otorgado
            FROM consents
            WHERE finalidad = 'marketing'
            ORDER BY customer_id, registrado_en DESC, id DESC
        ) v
        WHERE v.customer_id = c.id
        """
    )
    op.alter_column("customers", "acepta_marketing", server_default=None)
    op.drop_index("ix_consents_vigente", table_name="consents")
    op.drop_table("consents")

    for tabla in reversed(TABLAS_CON_UUID):
        op.drop_constraint(op.f(f"uq_{tabla}_uuid"), tabla, type_="unique")
        op.drop_column(tabla, "uuid")


def _normalizar_telefonos_existentes() -> None:
    """Pasa a E.164 los telefonos que ya existan.

    La logica se copia aqui a proposito: una migracion no debe importar el
    codigo de la aplicacion, que va a cambiar despues de que esta corra.
    """
    import phonenumbers

    conexion = op.get_bind()
    filas = conexion.execute(
        sa.text(
            "SELECT id, telefono FROM customers "
            "WHERE telefono IS NOT NULL AND telefono !~ :patron"
        ),
        {"patron": PATRON_E164},
    ).all()

    malos: list[int] = []
    for customer_id, telefono in filas:
        limpio = re.sub(r"[^\d+]", "", telefono)
        if m := re.match(r"^\+?521(\d{10})$", limpio):
            limpio = "+52" + m.group(1)
        elif m := re.match(r"^(?:044|045|01)(\d{10})$", limpio):
            limpio = m.group(1)
        try:
            numero = phonenumbers.parse(limpio, "MX")
        except phonenumbers.NumberParseException:
            numero = None
        if numero is None or not phonenumbers.is_valid_number(numero):
            if limpio:
                malos.append(customer_id)
                continue
            nuevo = None
        else:
            nuevo = phonenumbers.format_number(
                numero, phonenumbers.PhoneNumberFormat.E164
            )
        conexion.execute(
            sa.text("UPDATE customers SET telefono = :t WHERE id = :id"),
            {"t": nuevo, "id": customer_id},
        )

    if malos:
        raise RuntimeError(
            "Hay telefonos que no se pueden normalizar en los compradores "
            f"{malos}. Corrigelos o vacialos a mano y vuelve a migrar; no se "
            "borran solos."
        )
