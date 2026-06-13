# agent/tools/crm.py — Tools de CRM: registrar y consultar leads en la DB

import logging
from sqlalchemy import select

from agent.tools.base import ToolDefinition, ContextoEjecucion
from agent.memory.short_term import async_session, Lead

logger = logging.getLogger("agentkit")

_CALIFICACIONES = ("frio", "tibio", "caliente")


async def _registrar_lead(contexto: ContextoEjecucion, nombre: str | None = None,
                          email: str | None = None, telefono: str | None = None,
                          interes: str = "", calificacion: str = "tibio") -> str:
    """Registra un lead asociado al usuario actual."""
    if calificacion not in _CALIFICACIONES:
        calificacion = "tibio"
    async with async_session() as session:
        lead = Lead(
            tenant_id=contexto.tenant_id,
            usuario_id=contexto.usuario_id or 0,
            nombre=nombre, email=email, telefono=telefono,
            interes=interes, calificacion=calificacion,
        )
        session.add(lead)
        await session.commit()
        await session.refresh(lead)
    logger.info(f"[{contexto.tenant_id}] Lead #{lead.id} registrado ({calificacion})")
    return f"Lead registrado correctamente (#{lead.id}) para {nombre or 'el cliente'}."


async def _consultar_leads(contexto: ContextoEjecucion) -> str:
    """Devuelve los leads del usuario actual."""
    async with async_session() as session:
        query = select(Lead).where(
            Lead.tenant_id == contexto.tenant_id,
            Lead.usuario_id == (contexto.usuario_id or 0),
        ).order_by(Lead.creado.desc())
        leads = list((await session.execute(query)).scalars().all())
    if not leads:
        return "No hay leads registrados para este cliente."
    return "\n".join(
        f"#{l.id} {l.nombre or 's/n'} — {l.interes or 's/interés'} "
        f"({l.calificacion}, {l.estado})"
        for l in leads
    )


TOOL_REGISTRAR = ToolDefinition(
    nombre="registrar_lead",
    descripcion=(
        "Registra al cliente como lead cuando muestra interés real en contratar o "
        "comprar. Capturá su nombre y, si los da, email/teléfono e interés. Calificá "
        "el lead como 'frio', 'tibio' o 'caliente' según su intención de compra."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "nombre": {"type": "string", "description": "Nombre del cliente"},
            "email": {"type": "string", "description": "Email del cliente (si lo da)"},
            "telefono": {"type": "string", "description": "Teléfono del cliente (si lo da)"},
            "interes": {"type": "string", "description": "Qué servicio/producto le interesa"},
            "calificacion": {
                "type": "string",
                "enum": list(_CALIFICACIONES),
                "description": "Nivel de interés del lead",
            },
        },
        "required": ["interes"],
    },
    funcion=_registrar_lead,
)


TOOL_CONSULTAR = ToolDefinition(
    nombre="consultar_leads",
    descripcion="Consulta los leads ya registrados de este cliente.",
    input_schema={"type": "object", "properties": {}},
    funcion=_consultar_leads,
)
