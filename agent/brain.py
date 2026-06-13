# agent/brain.py — Cerebro del agente: conexión con cualquier LLM via LiteLLM
import os
import logging
import litellm
from dotenv import load_dotenv

from agent.tenants import gestor_tenants, DEFAULT_TENANT_ID

load_dotenv()
logger = logging.getLogger("agentkit")

litellm.suppress_debug_info = True

PRIMARY_MODEL = os.getenv("LLM_MODEL", "anthropic/claude-sonnet-4-6")

# Fallback automático: si el primario falla, LiteLLM prueba cada uno en orden
FALLBACK_MODELS = [
    "groq/llama-3.3-70b-versatile",
    "gemini/gemini-1.5-pro",
    "openai/gpt-4o",
    "perplexity/sonar",
]

# Compatibilidad: si LLM_API_KEY está seteada (variable legacy) y no hay
# ANTHROPIC_API_KEY, la mapeamos para no romper deploys existentes
_legacy_key = os.getenv("LLM_API_KEY")
if _legacy_key and not os.getenv("ANTHROPIC_API_KEY"):
    os.environ["ANTHROPIC_API_KEY"] = _legacy_key


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


async def generar_respuesta(mensaje: str, historial: list[dict],
                            tenant_id: str = DEFAULT_TENANT_ID,
                            contexto_rag: str = "",
                            contexto_memoria: str = "") -> str:
    """Genera una respuesta usando el LLM configurado, con fallback automático."""
    if not mensaje or len(mensaje.strip()) < 2:
        return obtener_mensaje_fallback(tenant_id)

    system_prompt = _componer_system_prompt(
        cargar_system_prompt(tenant_id), contexto_rag, contexto_memoria
    )

    # LiteLLM usa el formato OpenAI: system va como primer mensaje
    mensajes = [{"role": "system", "content": system_prompt}]
    for msg in historial:
        mensajes.append({"role": msg["role"], "content": msg["content"]})
    mensajes.append({"role": "user", "content": mensaje})

    try:
        response = await litellm.acompletion(
            model=PRIMARY_MODEL,
            messages=mensajes,
            max_tokens=1024,
            timeout=30,
            num_retries=2,
            fallbacks=FALLBACK_MODELS,
        )
        modelo_usado = response.model or PRIMARY_MODEL
        respuesta = response.choices[0].message.content
        logger.info(
            f"Respuesta generada — modelo: {modelo_usado} "
            f"({response.usage.prompt_tokens} in / {response.usage.completion_tokens} out)"
        )
        return respuesta

    except Exception as e:
        logger.error(f"Todos los proveedores LLM fallaron: {e}")
        return obtener_mensaje_error(tenant_id)
