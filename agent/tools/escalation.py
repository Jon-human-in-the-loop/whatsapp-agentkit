# agent/tools/escalation.py — Tool: escalar la conversación a un humano

import json
import logging

from agent.tools.base import ToolDefinition, ContextoEjecucion
from agent.memory.short_term import async_session, Usuario

logger = logging.getLogger("agentkit")


async def _escalar(contexto: ContextoEjecucion, motivo: str = "") -> str:
    """Marca al usuario para atención humana guardando el flag en su metadata."""
    if contexto.usuario_id:
        async with async_session() as session:
            usuario = await session.get(Usuario, contexto.usuario_id)
            if usuario:
                try:
                    meta = json.loads(usuario.metadata_json or "{}")
                except json.JSONDecodeError:
                    meta = {}
                meta["escalado"] = True
                meta["motivo_escalado"] = motivo
                usuario.metadata_json = json.dumps(meta, ensure_ascii=False)
                await session.commit()
    logger.info(f"[{contexto.tenant_id}] Conversación escalada a humano: {motivo}")
    return (
        "Listo, dejé tu consulta marcada para que una persona del equipo la atienda "
        "y te contacte a la brevedad."
    )


TOOL = ToolDefinition(
    nombre="escalar_a_humano",
    descripcion=(
        "Escalá la conversación a un humano cuando el cliente lo pide explícitamente, "
        "está muy frustrado, o la consulta excede lo que podés resolver. Indicá el motivo."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "motivo": {
                "type": "string",
                "description": "Motivo del escalado (qué necesita y por qué requiere un humano).",
            }
        },
        "required": ["motivo"],
    },
    funcion=_escalar,
)
