# agent/tools/calendar.py — Tool opcional: agendar citas
#
# Tool opcional (se activa por tenant). Por defecto registra la solicitud de
# cita en la metadata del usuario y confirma; si se integra Google Calendar,
# la llamada real iría en _crear_evento_google (gancho dejado preparado).

import os
import json
import logging

from agent.tools.base import ToolDefinition, ContextoEjecucion
from agent.memory.short_term import async_session, Usuario

logger = logging.getLogger("agentkit")


async def _crear_evento_google(fecha: str, hora: str, asunto: str) -> bool:
    """Gancho de integración con Google Calendar (no configurado por defecto)."""
    if not os.getenv("GOOGLE_CALENDAR_TOKEN"):
        return False
    # TODO: integrar Google Calendar API cuando el tenant aporte credenciales.
    return False


async def _agendar_cita(contexto: ContextoEjecucion, fecha: str, hora: str,
                        asunto: str = "Reunión") -> str:
    """Registra una solicitud de cita para el usuario actual."""
    creado_en_google = await _crear_evento_google(fecha, hora, asunto)

    if contexto.usuario_id:
        async with async_session() as session:
            usuario = await session.get(Usuario, contexto.usuario_id)
            if usuario:
                try:
                    meta = json.loads(usuario.metadata_json or "{}")
                except json.JSONDecodeError:
                    meta = {}
                meta.setdefault("citas", []).append(
                    {"fecha": fecha, "hora": hora, "asunto": asunto}
                )
                usuario.metadata_json = json.dumps(meta, ensure_ascii=False)
                await session.commit()

    logger.info(f"[{contexto.tenant_id}] Cita solicitada: {fecha} {hora} — {asunto}")
    if creado_en_google:
        return f"Listo, agendé tu cita para el {fecha} a las {hora}. Te llegará la confirmación."
    return (
        f"Perfecto, anoté tu solicitud de cita para el {fecha} a las {hora} "
        f"({asunto}). El equipo te confirma la disponibilidad a la brevedad."
    )


TOOL = ToolDefinition(
    nombre="agendar_cita",
    descripcion=(
        "Registra una solicitud de cita o reunión cuando el cliente quiere agendar. "
        "Pedí fecha y hora antes de usar esta herramienta."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "fecha": {"type": "string", "description": "Fecha de la cita (ej: 2026-06-20)"},
            "hora": {"type": "string", "description": "Hora de la cita (ej: 15:30)"},
            "asunto": {"type": "string", "description": "Motivo o asunto de la cita"},
        },
        "required": ["fecha", "hora"],
    },
    funcion=_agendar_cita,
)
