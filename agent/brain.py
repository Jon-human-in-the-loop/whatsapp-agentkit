# agent/brain.py — Cerebro del agente: bucle agentivo con Anthropic tool use
#
# Usa el SDK de Anthropic directamente (no LiteLLM) para aprovechar el tool use
# nativo. El bucle llama al modelo, ejecuta las tools que pida y reinyecta los
# resultados hasta que el modelo cierra el turno.
# (LiteLLM se sigue usando en agent/memory para embeddings y resúmenes.)

import os
import json
import logging
from anthropic import AsyncAnthropic
from dotenv import load_dotenv

from agent.tenants import gestor_tenants, DEFAULT_TENANT_ID
from agent.tools import obtener_tools, ejecutar_tool, ContextoEjecucion

load_dotenv()
logger = logging.getLogger("agentkit")

# Compatibilidad: mapear LLM_API_KEY (legacy) a ANTHROPIC_API_KEY si hace falta
_legacy_key = os.getenv("LLM_API_KEY")
if _legacy_key and not os.getenv("ANTHROPIC_API_KEY"):
    os.environ["ANTHROPIC_API_KEY"] = _legacy_key


def _modelo() -> str:
    """Modelo de Anthropic a usar (acepta ANTHROPIC_MODEL o LLM_MODEL legacy)."""
    modelo = os.getenv("ANTHROPIC_MODEL")
    if modelo:
        return modelo
    legacy = os.getenv("LLM_MODEL", "claude-sonnet-4-6")
    # LLM_MODEL legacy puede venir como "anthropic/claude-sonnet-4-6"
    return legacy.split("/", 1)[1] if legacy.startswith("anthropic/") else legacy


MAX_TURNOS_TOOLS = 5  # tope de iteraciones del bucle agentivo (evita loops infinitos)

_cliente: AsyncAnthropic | None = None


def _get_cliente() -> AsyncAnthropic:
    """Crea el cliente Anthropic de forma diferida (no requiere key al importar)."""
    global _cliente
    if _cliente is None:
        _cliente = AsyncAnthropic(
            api_key=os.getenv("ANTHROPIC_API_KEY"),
            timeout=30.0,
            max_retries=2,
        )
    return _cliente


# ─── Configuración por tenant ────────────────────────────────────────────────

def cargar_system_prompt(tenant_id: str = DEFAULT_TENANT_ID) -> str:
    return gestor_tenants.obtener_tenant(tenant_id).system_prompt


def obtener_mensaje_error(tenant_id: str = DEFAULT_TENANT_ID) -> str:
    return gestor_tenants.obtener_tenant(tenant_id).error_message


def obtener_mensaje_fallback(tenant_id: str = DEFAULT_TENANT_ID) -> str:
    return gestor_tenants.obtener_tenant(tenant_id).fallback_message


def _componer_system_prompt(base: str, contexto_rag: str, contexto_memoria: str) -> str:
    """Inyecta el contexto RAG y la memoria de largo plazo en el system prompt."""
    extra = ""
    if contexto_rag:
        extra += f"\n\n## Información relevante del negocio\n{contexto_rag}"
    if contexto_memoria:
        extra += f"\n\n## Lo que sabés de este cliente (conversaciones previas)\n{contexto_memoria}"
    return base + extra


def _extraer_texto(content) -> str:
    """Concatena los bloques de texto de una respuesta de Anthropic."""
    return "".join(b.text for b in content if getattr(b, "type", None) == "text").strip()


# ─── Bucle agentivo ──────────────────────────────────────────────────────────

async def ejecutar_agente(mensaje: str, historial: list[dict],
                          tenant_id: str = DEFAULT_TENANT_ID,
                          contexto_rag: str = "", contexto_memoria: str = "",
                          usuario_id: int | None = None) -> tuple[str, list]:
    """
    Ejecuta el bucle agentivo con tool use. Devuelve (respuesta, tool_calls_log).
    """
    if not mensaje or len(mensaje.strip()) < 2:
        return obtener_mensaje_fallback(tenant_id), []

    cfg = gestor_tenants.obtener_tenant(tenant_id)
    system = _componer_system_prompt(cfg.system_prompt, contexto_rag, contexto_memoria)

    tools = obtener_tools(cfg.tools)
    tools_anthropic = [t.to_anthropic() for t in tools]
    contexto_tool = ContextoEjecucion(tenant_id=tenant_id, usuario_id=usuario_id)

    mensajes = [{"role": m["role"], "content": m["content"]} for m in historial]
    mensajes.append({"role": "user", "content": mensaje})

    tool_calls_log: list[dict] = []

    try:
        for _ in range(MAX_TURNOS_TOOLS):
            kwargs = dict(model=_modelo(), max_tokens=1024, system=system, messages=mensajes)
            if tools_anthropic:
                kwargs["tools"] = tools_anthropic

            response = await _get_cliente().messages.create(**kwargs)

            if response.stop_reason == "tool_use":
                # Reinyectar la respuesta del modelo y ejecutar cada tool pedida
                mensajes.append({"role": "assistant", "content": response.content})
                resultados = []
                for bloque in response.content:
                    if getattr(bloque, "type", None) != "tool_use":
                        continue
                    salida = await ejecutar_tool(bloque.name, bloque.input, contexto_tool)
                    tool_calls_log.append(
                        {"tool": bloque.name, "input": bloque.input, "output": salida}
                    )
                    resultados.append({
                        "type": "tool_result",
                        "tool_use_id": bloque.id,
                        "content": str(salida),
                    })
                mensajes.append({"role": "user", "content": resultados})
                continue

            # end_turn u otro: devolver el texto generado
            return _extraer_texto(response.content), tool_calls_log

        # Se agotaron los turnos de tools
        logger.warning("Bucle agentivo alcanzó el máximo de turnos de tools")
        return ("Disculpá, no pude completar la consulta en este momento. "
                "¿Querés que lo intente de otra forma?"), tool_calls_log

    except Exception as e:
        logger.error(f"Error en el bucle agentivo: {e}")
        return obtener_mensaje_error(tenant_id), tool_calls_log


async def generar_respuesta(mensaje: str, historial: list[dict],
                            tenant_id: str = DEFAULT_TENANT_ID,
                            contexto_rag: str = "", contexto_memoria: str = "",
                            usuario_id: int | None = None) -> str:
    """Wrapper que devuelve solo el texto de la respuesta (compatibilidad)."""
    texto, _ = await ejecutar_agente(
        mensaje, historial, tenant_id,
        contexto_rag=contexto_rag, contexto_memoria=contexto_memoria,
        usuario_id=usuario_id,
    )
    return texto


def serializar_tool_calls(tool_calls: list) -> str | None:
    """Serializa el log de tool calls a JSON para guardarlo en la DB."""
    if not tool_calls:
        return None
    try:
        return json.dumps(tool_calls, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return None
