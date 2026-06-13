# agent/tools/__init__.py — Registro central de herramientas del agente
#
# Cada tool se registra por nombre. Un tenant habilita las que quiera en su
# config (config/tenants/{id}/tenant.yaml → tools). El bucle agentivo de
# brain.py pide las definiciones con obtener_tools() y las ejecuta con
# ejecutar_tool().

import logging

from agent.tools.base import ToolDefinition, ContextoEjecucion
from agent.tools import knowledge_search, crm, escalation, calendar

logger = logging.getLogger("agentkit")

_REGISTRO: dict[str, ToolDefinition] = {
    "knowledge_search": knowledge_search.TOOL,
    "registrar_lead": crm.TOOL_REGISTRAR,
    "consultar_leads": crm.TOOL_CONSULTAR,
    "escalar_a_humano": escalation.TOOL,
    "agendar_cita": calendar.TOOL,
}


def obtener_tools(nombres: list[str]) -> list[ToolDefinition]:
    """Devuelve las definiciones de las tools pedidas que existan en el registro."""
    tools = []
    for nombre in nombres:
        tool = _REGISTRO.get(nombre)
        if tool is None:
            logger.warning(f"Tool desconocida ignorada: {nombre}")
            continue
        tools.append(tool)
    return tools


async def ejecutar_tool(nombre: str, input_dict: dict, contexto: ContextoEjecucion) -> str:
    """Ejecuta una tool por nombre con los argumentos provistos por el modelo."""
    tool = _REGISTRO.get(nombre)
    if tool is None:
        return f"Herramienta desconocida: {nombre}"
    try:
        return await tool.funcion(contexto, **(input_dict or {}))
    except TypeError as e:
        logger.error(f"Argumentos inválidos para {nombre}: {e}")
        return f"No pude ejecutar {nombre}: argumentos inválidos."
    except Exception as e:
        logger.error(f"Error ejecutando tool {nombre}: {e}")
        return f"Error ejecutando {nombre}."


__all__ = ["ToolDefinition", "ContextoEjecucion", "obtener_tools", "ejecutar_tool"]
