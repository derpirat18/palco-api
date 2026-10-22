"""Escaner en linea de la puerta (E3a).

Cada lectura de un QR (o del codigo impreso, si la camara no lee) dice en
el momento si la persona entra, y con que datos: zona, asiento y a nombre
de quien. Pueden escanear muchas personas a la vez, cada una con su
celular, en la misma funcion.

Las dos garantias:

- Un boleto entra una sola vez. El paso a `usado` es un UPDATE
  condicionado (`WHERE estado = 'valido'`): si dos puertas leen el mismo
  boleto en el mismo instante, Postgres deja pasar a una y la otra ve
  "ya entro". No hay lectura-y-luego-escritura que se pueda cruzar.
- Una lectura repetida no cambia nada. Cada lectura trae su
  `client_scan_id` (lo genera el celular): si la red se cae y el celular
  reintenta, la respuesta es la misma de la primera vez, no un "ya entro"
  falso para quien acaba de entrar.

Lo que se muestra es lo minimo para dejar pasar: sin correo ni telefono.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import and_, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import EstadoBoleto, EstadoOrden
from app.models.catalog import Performance
from app.models.sales import Customer, Order, OrderItem, Scan, Ticket
from app.models.users import User
from app.services.boletos import QRInvalido, leer_qr
from app.services.lista_de_acceso import EN_LA_LISTA, titular_de

VALIDO = "valido"
DUPLICADO = "duplicado"
OTRA_FUNCION = "otra_funcion"
INVALIDO = "invalido"
CANCELADO = "cancelado"

# Codigo impreso bajo el QR: 10 caracteres Crockford base 32 (XXXXX-XXXXX).
_CODIGO = re.compile(r"^[0-9A-Z]{10}$")
# Lo que una persona escribe por error y Crockford lee igual.
_CONFUSIONES = str.maketrans({"O": "0", "I": "1", "L": "1", "U": "V"})


@dataclass(frozen=True)
class DatosDelBoleto:
    codigo: str
    folio: str
    zona: str
    asiento: str
    titular: str
    cortesia: bool


@dataclass(frozen=True)
class Contador:
    total: int
    usados: int


@dataclass(frozen=True)
class Escaneo:
    resultado: str
    escaneado_en: datetime
    boleto: DatosDelBoleto | None = None
    # duplicado: la primera vez que entro, y con quien.
    entro_en: datetime | None = None
    entro_con: str | None = None
    # cancelado: por que (cancelada, reembolsada, revocada, transferido).
    motivo: str | None = None
    # otra_funcion: cuando es la suya, si es del mismo evento.
    su_funcion_inicia_en: datetime | None = None
    contador: Contador | None = None


def codigo_impreso(texto: str) -> str | None:
    """El codigo escrito a mano, normalizado al formato con que se guardo."""
    limpio = re.sub(r"[\s\-]", "", texto.upper()).translate(_CONFUSIONES)
    if not _CODIGO.match(limpio):
        return None
    return f"{limpio[:5]}-{limpio[5:]}"


class EscanerService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ------------------------------------------------------------ lectura

    async def escanear(
        self,
        funcion: Performance,
        texto: str,
        client_scan_id: str,
        usuario: User,
    ) -> Escaneo:
        previo = await self.db.scalar(
            select(Scan).where(Scan.client_scan_id == client_scan_id)
        )
        if previo is not None:
            return await self._repetir(previo, funcion)

        ahora = datetime.now(UTC)
        boleto = await self._buscar(texto)
        resultado, motivo = await self._decidir(boleto, funcion)

        if resultado == VALIDO:
            # El candado: solo una lectura gana el paso de valido a usado.
            gano = await self.db.scalar(
                update(Ticket)
                .where(
                    Ticket.id == boleto.id,
                    Ticket.estado == EstadoBoleto.VALIDO.value,
                )
                .values(estado=EstadoBoleto.USADO.value)
                .returning(Ticket.id)
            )
            if gano is None:
                resultado = DUPLICADO

        insertado = await self.db.scalar(
            insert(Scan)
            .values(
                ticket_id=boleto.id if boleto else None,
                performance_id=funcion.id,
                user_id=usuario.id,
                # El codigo impreso, no el QR: el QR es la credencial del
                # boleto y no tiene por que quedar copiado en otra tabla.
                codigo_leido=boleto.codigo if boleto else texto.strip()[:80],
                resultado=resultado,
                escaneado_en=ahora,
                offline=False,
                client_scan_id=client_scan_id,
                nota=motivo,
            )
            .on_conflict_do_nothing(index_elements=["client_scan_id"])
            .returning(Scan.id)
        )
        if insertado is None:
            # La misma lectura llego dos veces a la par: vale la que se guardo.
            previo = await self.db.scalar(
                select(Scan).where(Scan.client_scan_id == client_scan_id)
            )
            return await self._repetir(previo, funcion)

        await self.db.flush()
        return await self._armar(resultado, ahora, boleto, funcion, motivo)

    async def _buscar(self, texto: str) -> Ticket | None:
        texto = texto.strip()
        try:
            boleto_uuid = leer_qr(texto)
        except QRInvalido:
            codigo = codigo_impreso(texto)
            if codigo is None:
                return None
            return await self.db.scalar(select(Ticket).where(Ticket.codigo == codigo))
        return await self.db.scalar(select(Ticket).where(Ticket.uuid == boleto_uuid))

    async def _decidir(
        self, boleto: Ticket | None, funcion: Performance
    ) -> tuple[str, str | None]:
        if boleto is None:
            return INVALIDO, None
        partida, orden = (
            await self.db.execute(
                select(OrderItem, Order)
                .join(Order, Order.id == OrderItem.order_id)
                .where(OrderItem.id == boleto.order_item_id)
            )
        ).one()
        if orden.performance_id != funcion.id:
            return OTRA_FUNCION, None
        if orden.cerrada_motivo == "cortesia_revocada":
            return CANCELADO, "revocada"
        if orden.estado != EstadoOrden.PAGADA.value:
            return CANCELADO, orden.estado
        if partida.anulado:
            return CANCELADO, "cancelada"
        if boleto.estado == EstadoBoleto.CANCELADO.value:
            return CANCELADO, "cancelada"
        if boleto.estado == EstadoBoleto.TRANSFERIDO.value:
            return CANCELADO, "transferido"
        if boleto.estado == EstadoBoleto.USADO.value:
            return DUPLICADO, None
        return VALIDO, None

    async def _repetir(self, previo: Scan, funcion: Performance) -> Escaneo:
        boleto = (
            await self.db.get(Ticket, previo.ticket_id) if previo.ticket_id else None
        )
        return await self._armar(
            previo.resultado, previo.escaneado_en, boleto, funcion, previo.nota
        )

    # ---------------------------------------------------------- respuesta

    async def _armar(
        self,
        resultado: str,
        escaneado_en: datetime,
        boleto: Ticket | None,
        funcion: Performance,
        motivo: str | None,
    ) -> Escaneo:
        datos = None
        su_funcion = None
        entro_en = entro_con = None
        if boleto is not None:
            partida, orden, titular = (
                await self.db.execute(
                    select(OrderItem, Order, Customer)
                    .join(Order, Order.id == OrderItem.order_id)
                    .join(Customer, Customer.id == boleto.holder_customer_id)
                    .where(OrderItem.id == boleto.order_item_id)
                )
            ).one()
            if resultado == OTRA_FUNCION:
                # Datos de una funcion ajena no salen: solo la fecha, y solo
                # si es otra funcion del mismo evento ("es para el sabado").
                suya = await self.db.get(Performance, orden.performance_id)
                if suya is not None and suya.event_id == funcion.event_id:
                    su_funcion = suya.inicia_en
            else:
                datos = DatosDelBoleto(
                    codigo=boleto.codigo,
                    folio=orden.folio,
                    zona=partida.zona_snap,
                    asiento=partida.asiento_snap,
                    titular=titular_de(orden, boleto, titular),
                    cortesia=orden.canal == "cortesia",
                )
            if resultado == DUPLICADO:
                entro_en, entro_con = await self._primera_entrada(boleto.id)
        return Escaneo(
            resultado=resultado,
            escaneado_en=escaneado_en,
            boleto=datos,
            entro_en=entro_en,
            entro_con=entro_con,
            motivo=motivo,
            su_funcion_inicia_en=su_funcion,
            contador=await self.contador(funcion.id),
        )

    async def _primera_entrada(
        self, ticket_id: int
    ) -> tuple[datetime | None, str | None]:
        fila = (
            await self.db.execute(
                select(Scan.escaneado_en, Scan.device_id, User.nombre)
                .outerjoin(User, User.id == Scan.user_id)
                .where(Scan.ticket_id == ticket_id, Scan.resultado == VALIDO)
                .order_by(Scan.escaneado_en)
                .limit(1)
            )
        ).first()
        if fila is None:
            return None, None
        escaneado_en, device_id, nombre = fila
        if nombre:
            return escaneado_en, nombre
        return escaneado_en, "un dispositivo de acceso" if device_id else None

    async def contador(self, performance_id: int) -> Contador:
        """Cuantos boletos pueden entrar a la funcion y cuantos ya entraron."""
        return (await self.contadores([performance_id]))[performance_id]

    async def contadores(self, performance_ids: list[int]) -> dict[int, Contador]:
        if not performance_ids:
            return {}
        filas = (
            await self.db.execute(
                select(
                    Order.performance_id,
                    func.count(Ticket.id),
                    func.count(Ticket.id).filter(
                        Ticket.estado == EstadoBoleto.USADO.value
                    ),
                )
                .select_from(Ticket)
                .join(OrderItem, OrderItem.id == Ticket.order_item_id)
                .join(
                    Order,
                    and_(
                        Order.id == OrderItem.order_id,
                        Order.estado == EstadoOrden.PAGADA.value,
                    ),
                )
                .where(
                    Order.performance_id.in_(performance_ids),
                    OrderItem.anulado.is_(False),
                    Ticket.estado.in_(EN_LA_LISTA),
                )
                .group_by(Order.performance_id)
            )
        ).all()
        salida = {pid: Contador(0, 0) for pid in performance_ids}
        for pid, total, usados in filas:
            salida[pid] = Contador(total, usados)
        return salida
