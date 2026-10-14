"""Alta de usuarios del personal desde la linea de comandos.

El primer admin de PSD se crea con esto, porque no hay forma de entrar
al sistema sin un usuario previo.

    python scripts/crear_usuario.py admin@psdigital.mx "Juan Manuel" admin_psd
    python scripts/crear_usuario.py org@circo.mx "Nayeli" organizador --cliente 1
"""

import argparse
import asyncio
import getpass
import sys

from sqlalchemy import select

from app.core.db import SessionLocal
from app.core.security import hash_password
from app.models.base import RolUsuario
from app.models.users import User


async def crear(email: str, nombre: str, rol: str, client_id: int | None) -> None:
    email = email.strip().lower()

    if rol not in {r.value for r in RolUsuario}:
        sys.exit(f"Rol invalido. Opciones: {[r.value for r in RolUsuario]}")

    if rol == RolUsuario.ADMIN_PSD and client_id is not None:
        sys.exit("Un admin de PSD no pertenece a ningun organizador.")

    if rol != RolUsuario.ADMIN_PSD and client_id is None:
        sys.exit(f"El rol {rol} requiere --cliente <id>.")

    password = getpass.getpass("Contraseña: ")
    if len(password) < 10:
        sys.exit("Minimo 10 caracteres.")
    if password != getpass.getpass("Confirmar: "):
        sys.exit("No coinciden.")

    async with SessionLocal() as db:
        if await db.scalar(select(User).where(User.email == email)):
            sys.exit(f"Ya existe un usuario con {email}.")

        db.add(
            User(
                email=email,
                nombre=nombre,
                password_hash=hash_password(password),
                rol=rol,
                client_id=client_id,
            )
        )
        await db.commit()

    print(f"Usuario {email} creado con rol {rol}.")


def main() -> None:
    p = argparse.ArgumentParser(description="Alta de personal")
    p.add_argument("email")
    p.add_argument("nombre")
    p.add_argument("rol", choices=[r.value for r in RolUsuario])
    p.add_argument("--cliente", type=int, default=None, help="id del organizador")
    a = p.parse_args()
    asyncio.run(crear(a.email, a.nombre, a.rol, a.cliente))


if __name__ == "__main__":
    main()
