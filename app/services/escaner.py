"""Escaner en linea de la puerta (E3a).

Cada lectura de un QR dice en el momento si la persona entra, y con que
datos: zona, asiento y a nombre de quien.

La regla de la puerta (BOL3): solo entra el QR que cambia cada 30 s, desde
el celular. El personal de acceso no teclea codigos: cualquier problema
(sin celular, pantalla estrellada, el QR no lee) se resuelve en taquilla,
donde PSD o el organizador pueden teclear el codigo del boleto; queda
registrado quien lo hizo. Pueden escanear muchas personas a la vez, cada una con su
celular, en la misma funcion.

Las dos garantias:

- Un boleto entra una sola vez. El paso a `usado` es un UPDATE
  condicionado (`WHERE estado = 'valido'`): si dos puertas leen el mismo
  boleto en el mismo instante, Postgres deja pasar a una y la otra ve
  "ya entro". No hay lectura-y-luego-escritura que se pueda cruzar.
- Cada funcion acepta lecturas solo en su horario (E3c): de 4 horas antes
  de empezar a 12 horas despues. Fuera de ahi el boleto no se toca: un
  error del personal el jueves no deja sin entrar a quien va el viernes.
- Una lectura repetida no cambia nada. Cada lectura trae su
  `client_scan_id` (lo genera el celular): si la red se cae y el celular
  reintenta, la respuesta es la misma de la primera vez, no un "ya entro"
  falso para quien acaba de entrar.

Lo que se muestra es lo minimo para dejar pasar: sin correo ni telefono.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ErrorDeDominio, RecursoNoEncontrado
from app.models.base import EstadoBoleto, EstadoFuncion, EstadoOrden, RolUsuario
from app.models.catalog import Performance
from app.models.sales import Customer, Order, OrderItem, Scan, Ticket
from app.models.users import User
from app.services.boletos import QRInvalido, leer_cualquier_qr, verificar_rotatorio
from app.services.lista_de_acceso import EN_LA_LISTA, titular_de

VALIDO = "valido"
DUPLICADO = "duplicado"
OTRA_FUNCION = "otra_funcion"
INVALIDO = "invalido"
CANCELADO = "cancelado"
# No se guarda: fuera de horario la lectura no toca nada.
FUERA_DE_HORARIO = "fuera_de_horario"
DESHECHO = "deshecho"

# Horario de escaneo de una funcion (E3c).
ABRE_ANTES = timedelta(hours=4)
CIERRA_DESPUES = timedelta(hours=12)

BOLETO_NO_ES_DE_LA_FUNCION = "No encontramos ese boleto en esta función."


class SinEntradaQueDeshacer(ErrorDeDominio):
    codigo = "sin_entrada_que_deshacer"
    http_status = 409


def horario_de_escaneo(funcion: Performance) -> tuple[datetime, datetime]:
    return funcion.inicia_en - ABRE_ANTES, funcion.inicia_en + CIERRA_DESPUES


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
    # duplicado: cuando entro (la entrada vigente), y con quien.
    entro_en: datetime | None = None
    entro_con: str | None = None
    # cancelado: por que (cancelada, reembolsada, revocada, transferido).
    # fuera_de_horario: todavia_no, ya_termino o funcion_cancelada.
    motivo: str | None = None
    # otra_funcion: cuando es la suya, si es del mismo evento.
    su_funcion_inicia_en: datetime | None = None
    contador: Contador | None = None
    # Horario de escaneo de esta funcion.
    abre_en: datetime | None = None
    cierra_en: datetime | None = None


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
        abre, cierra = horario_de_escaneo(funcion)
        fuera = (
            "funcion_cancelada"
            if funcion.estado == EstadoFuncion.CANCELADA.value
            else "todavia_no"
            if ahora < abre
            else "ya_termino"
            if ahora > cierra
            else None
        )
        if fuera:
            return Escaneo(
                resultado=FUERA_DE_HORARIO,
                escaneado_en=ahora,
                motivo=fuera,
                contador=await self.contador(funcion.id),
                abre_en=abre,
                cierra_en=cierra,
            )

        boleto, rechazo = await self._buscar(texto, usuario, ahora)
        codigo_ajeno = None
        if rechazo:
            # El boleto existe pero lo que se mostro no deja entrar: un QR
            # vencido (captura), el QR fijo viejo o un codigo tecleado por
            # quien no es taquilla. La puerta ve el motivo para orientar.
            resultado, motivo = INVALIDO, rechazo
            if boleto is not None and not await self._es_de_la_funcion(boleto, funcion):
                # De otra funcion: sus datos no se muestran aqui.
                codigo_ajeno, boleto = boleto.codigo, None
        else:
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
                codigo_leido=(
                    boleto.codigo if boleto else codigo_ajeno or texto.strip()[:80]
                ),
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

    async def _buscar(
        self, texto: str, usuario: User, ahora: datetime
    ) -> tuple[Ticket | None, str | None]:
        """El boleto que se mostro y, si no deja entrar, por que.

        Motivos: "vencido" (QR de hace mas de un minuto: una captura, o el
        reloj del celular muy mal), "qr_fijo" (el QR viejo del correo) y
        "solo_taquilla" (codigo tecleado por el personal de acceso).
        """
        texto = texto.strip()
        try:
            leido = leer_cualquier_qr(texto)
        except QRInvalido:
            codigo = codigo_impreso(texto)
            if codigo is None:
                return None, None
            boleto = await self.db.scalar(select(Ticket).where(Ticket.codigo == codigo))
            if boleto is not None and usuario.rol == RolUsuario.ACCESO:
                return boleto, "solo_taquilla"
            return boleto, None
        boleto = await self.db.scalar(
            select(Ticket).where(Ticket.uuid == leido.boleto_uuid)
        )
        if boleto is None:
            return None, None
        if leido.es_fijo:
            return boleto, "qr_fijo"
        falla = verificar_rotatorio(leido, boleto.semilla, ahora.timestamp())
        if falla == "falso":
            # Firma que no cuadra: no se dice de que boleto es.
            return None, None
        return boleto, falla

    async def _es_de_la_funcion(self, boleto: Ticket, funcion: Performance) -> bool:
        return (
            await self.db.scalar(
                select(Order.performance_id)
                .join(OrderItem, OrderItem.order_id == Order.id)
                .where(OrderItem.id == boleto.order_item_id)
            )
        ) == funcion.id

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
                entro_en, entro_con = await self._entrada_vigente(boleto.id)
        abre, cierra = horario_de_escaneo(funcion)
        return Escaneo(
            resultado=resultado,
            escaneado_en=escaneado_en,
            boleto=datos,
            entro_en=entro_en,
            entro_con=entro_con,
            motivo=motivo,
            su_funcion_inicia_en=su_funcion,
            contador=await self.contador(funcion.id),
            abre_en=abre,
            cierra_en=cierra,
        )

    async def _entrada_vigente(
        self, ticket_id: int
    ) -> tuple[datetime | None, str | None]:
        """La ultima lectura que lo dejo pasar: si una entrada se deshizo y la
        persona volvio a entrar, cuenta la segunda."""
        fila = (
            await self.db.execute(
                select(Scan.escaneado_en, Scan.device_id, User.nombre)
                .outerjoin(User, User.id == Scan.user_id)
                .where(Scan.ticket_id == ticket_id, Scan.resultado == VALIDO)
                .order_by(Scan.escaneado_en.desc())
                .limit(1)
            )
        ).first()
        if fila is None:
            return None, None
        escaneado_en, device_id, nombre = fila
        if nombre:
            return escaneado_en, nombre
        return escaneado_en, "un dispositivo de acceso" if device_id else None

    # ------------------------------------------------------ deshacer (E3d)

    async def deshacer_entrada(
        self, funcion: Performance, codigo: str, usuario: User, motivo: str
    ) -> Contador:
        """Regresa a valido un boleto que se escaneo por error.

        El mismo UPDATE condicionado que al entrar, al reves: si dos personas
        lo deshacen a la vez, una lo hace y la otra recibe "no ha entrado".
        Queda una lectura `deshecho` con quien y por que.
        """
        normal = codigo_impreso(codigo)
        boleto = None
        if normal:
            boleto = await self.db.scalar(
                select(Ticket)
                .join(OrderItem, OrderItem.id == Ticket.order_item_id)
                .join(Order, Order.id == OrderItem.order_id)
                .where(Ticket.codigo == normal, Order.performance_id == funcion.id)
            )
        if boleto is None:
            raise RecursoNoEncontrado(BOLETO_NO_ES_DE_LA_FUNCION)
        regresado = await self.db.scalar(
            update(Ticket)
            .where(Ticket.id == boleto.id, Ticket.estado == EstadoBoleto.USADO.value)
            .values(estado=EstadoBoleto.VALIDO.value)
            .returning(Ticket.id)
        )
        if regresado is None:
            raise SinEntradaQueDeshacer(
                "Este boleto no ha entrado: no hay nada que deshacer."
            )
        self.db.add(
            Scan(
                ticket_id=boleto.id,
                performance_id=funcion.id,
                user_id=usuario.id,
                codigo_leido=boleto.codigo,
                resultado=DESHECHO,
                escaneado_en=datetime.now(UTC),
                offline=False,
                client_scan_id=f"deshacer-{uuid.uuid4().hex}",
                nota=motivo,
            )
        )
        await self.db.flush()
        return await self.contador(funcion.id)

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
