# agent/main.py — Servidor FastAPI + router multi-canal / multi-tenant
# HELIX · AI / AgentKit

import os
import re
import asyncio
import random
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import PlainTextResponse, JSONResponse, Response
from dotenv import load_dotenv

from agent.brain import ejecutar_agente, serializar_tool_calls
from agent.memory import (
    inicializar_db,
    obtener_o_crear_usuario,
    guardar_mensaje,
    obtener_historial,
)
from agent.channels import obtener_canal, CanalBase, MensajeUnificado
from agent.tenants import gestor_tenants
from agent.memory.knowledge import indexar_knowledge_base, buscar_en_knowledge
from agent.memory.long_term import recuperar_contexto_relevante, guardar_resumen_si_necesario
from agent.security import (
    validar_configuracion,
    sanitizar_mensaje,
    ya_procesado,
    marcar_procesado,
    rate_limit_excedido,
)

load_dotenv()

ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
log_level = logging.DEBUG if ENVIRONMENT == "development" else logging.INFO
logging.basicConfig(level=log_level)
logger = logging.getLogger("agentkit")

validar_configuracion()

# Tenant por defecto para la ruta legacy /webhook (single-tenant).
# La ruta /webhook/{canal}/{tenant_id} resuelve el tenant desde la URL.
TENANT_ID = os.getenv("DEFAULT_TENANT_ID", "demo")
# Canal principal de WhatsApp (Twilio o Meta) para la ruta legacy /webhook.
canal = obtener_canal(os.getenv("WHATSAPP_PROVIDER", ""), TENANT_ID)
PORT = int(os.getenv("PORT", 8000))

# Canales que requieren polling (Email) se arrancan al boot y se mantienen vivos.
_canales_polling: list[CanalBase] = []


async def _arrancar_canales_polling() -> None:
    """Arranca los canales basados en polling (Email) de cada tenant que los habilite."""
    for tenant_id in gestor_tenants.listar_tenants():
        cfg = gestor_tenants.obtener_tenant(tenant_id)
        if "email" not in cfg.canales:
            continue
        canal_email = obtener_canal("email", tenant_id)
        # Inyectar el dispatcher para no crear dependencia circular channels→main
        canal_email.procesar = procesar_mensaje
        await canal_email.iniciar()
        _canales_polling.append(canal_email)


async def _indexar_conocimiento() -> None:
    """Auto-indexa la base de conocimiento de cada tenant al arrancar."""
    for tenant_id in gestor_tenants.listar_tenants():
        try:
            n = await indexar_knowledge_base(tenant_id)
            if n:
                logger.info(f"[{tenant_id}] {n} chunks indexados en knowledge base")
        except Exception as e:
            logger.error(f"Error indexando knowledge de {tenant_id}: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await inicializar_db()
    logger.info("Base de datos inicializada")
    await _indexar_conocimiento()
    await canal.iniciar()
    await _arrancar_canales_polling()
    logger.info(f"Servidor AgentKit — HELIX · AI corriendo en puerto {PORT}")
    logger.info(f"Canal legacy activo: {canal.__class__.__name__} (tenant: {TENANT_ID})")
    yield


app = FastAPI(
    title="Sofía — Agente WhatsApp de HELIX · AI",
    version="2.0.0",
    lifespan=lifespan
)


# ─── Helpers de procesamiento ────────────────────────────────────────────────

def partir_en_bloques(respuesta: str, max_len: int = 280) -> list[str]:
    """
    Parte una respuesta larga en bloques cortos estilo chat humano:
    primero por párrafos dobles, y si no hay, por oraciones.
    """
    bloques = [b.strip() for b in respuesta.split("\n\n") if b.strip()]
    if len(bloques) == 1 and len(respuesta) > max_len:
        partes = re.split(r"(?<=[.!?])\s+", respuesta)
        bloques = []
        actual = ""
        for parte in partes:
            if len(actual) + len(parte) < max_len:
                actual = (actual + " " + parte).strip()
            else:
                if actual:
                    bloques.append(actual)
                actual = parte
        if actual:
            bloques.append(actual)
    return bloques or [respuesta]


async def procesar_mensaje(canal_obj: CanalBase, msg: MensajeUnificado) -> None:
    """
    Procesa un mensaje entrante: rate limit, sanitización, generación de
    respuesta con memoria del tenant y envío humanizado. Pensado para correr
    en background (la idempotencia ya se marcó antes de agendar esta tarea).
    """
    try:
        if rate_limit_excedido(msg.usuario_id):
            logger.warning(f"Rate limit excedido: {msg.usuario_id}")
            await canal_obj.enviar_mensaje(
                msg.usuario_id,
                "Enviaste muchos mensajes muy rápido. Por favor esperá un momento e intentá de nuevo 🙏",
                msg.thread_id,
            )
            return

        texto = sanitizar_mensaje(msg.texto)
        if not texto:
            return

        logger.info(f"[{msg.tenant_id}/{msg.canal.value}] Mensaje de {msg.usuario_id}: {texto}")

        # Resolver el usuario interno (lo crea si es la primera vez)
        usuario_pk = await obtener_o_crear_usuario(
            msg.tenant_id, msg.canal.value, msg.usuario_id, msg.usuario_nombre
        )

        historial = await obtener_historial(msg.tenant_id, usuario_pk)

        # Contexto: RAG sobre el knowledge base + memoria de largo plazo del cliente
        contexto_rag = await buscar_en_knowledge(msg.tenant_id, texto)
        contexto_memoria = await recuperar_contexto_relevante(msg.tenant_id, usuario_pk, texto)

        respuesta, tool_calls = await ejecutar_agente(
            texto, historial, msg.tenant_id,
            contexto_rag=contexto_rag, contexto_memoria=contexto_memoria,
            usuario_id=usuario_pk,
        )

        await guardar_mensaje(msg.tenant_id, usuario_pk, msg.canal.value, "user", texto)
        await guardar_mensaje(
            msg.tenant_id, usuario_pk, msg.canal.value, "assistant", respuesta,
            tool_calls_json=serializar_tool_calls(tool_calls),
        )

        # Si la conversación acumuló suficientes mensajes, resumir para la memoria larga
        await guardar_resumen_si_necesario(msg.tenant_id, usuario_pk)

        bloques = partir_en_bloques(respuesta)
        for i, bloque in enumerate(bloques):
            # Delay humano: más largo para el primer mensaje, más corto entre bloques
            if i == 0:
                delay = min(2 + len(bloque) / 80, 8) + random.uniform(0, 1.5)
            else:
                delay = random.uniform(1.5, 3)
            await asyncio.sleep(delay)
            await canal_obj.enviar_mensaje(msg.usuario_id, bloque, msg.thread_id)

        logger.info(f"Respuesta a {msg.usuario_id} ({len(bloques)} bloque/s): {respuesta[:80]}...")

    except Exception as e:
        logger.error(f"Error procesando mensaje de {msg.usuario_id}: {e}")


def responder_verificacion(resultado):
    """
    Traduce el valor de validar_webhook() a una respuesta HTTP adecuada:
    None → no aplica; dict → JSON (Discord PONG); int/str → texto plano
    (Meta hub.challenge, Slack url_verification).
    """
    if resultado is None:
        return None
    if isinstance(resultado, Response):
        return resultado
    if isinstance(resultado, dict):
        return JSONResponse(resultado)
    return PlainTextResponse(str(resultado))


async def manejar_webhook(canal_obj: CanalBase, request: Request,
                          background_tasks: BackgroundTasks) -> dict:
    """
    Lógica común a todas las rutas de webhook: parsea, deduplica y agenda el
    procesamiento en background para responder 200 de inmediato (Slack exige
    < 3s; Meta reenvía si no respondemos en ~20s).
    """
    mensajes = await canal_obj.parsear_webhook(request)
    for msg in mensajes:
        if msg.es_propio or not msg.texto:
            continue
        # Idempotencia: marcar de forma síncrona para deduplicar retries
        if ya_procesado(msg.mensaje_id):
            logger.info(f"Mensaje duplicado ignorado: {msg.mensaje_id}")
            continue
        marcar_procesado(msg.mensaje_id)
        background_tasks.add_task(procesar_mensaje, canal_obj, msg)
    return {"status": "ok"}


# ─── Rutas ───────────────────────────────────────────────────────────────────

@app.get("/")
async def health_check():
    return {"status": "ok", "service": "helix-ai-agentkit", "agente": "Sofía"}


@app.get("/webhook")
async def webhook_verificacion(request: Request):
    """Verificación GET del canal legacy (WhatsApp Meta la requiere)."""
    respuesta = responder_verificacion(await canal.validar_webhook(request))
    return respuesta if respuesta is not None else {"status": "ok"}


@app.post("/webhook")
async def webhook_handler(request: Request, background_tasks: BackgroundTasks):
    """Ruta legacy single-tenant: usa el canal de WhatsApp configurado en .env."""
    try:
        return await manejar_webhook(canal, request, background_tasks)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error en webhook: {e}")
        detail = str(e) if ENVIRONMENT == "development" else "Error interno"
        raise HTTPException(status_code=500, detail=detail)


@app.get("/webhook/{canal_nombre}/{tenant_id}")
async def webhook_verificacion_multi(canal_nombre: str, tenant_id: str, request: Request):
    """Verificación GET por canal+tenant (Meta hub.challenge, Slack url_verification)."""
    canal_obj = obtener_canal(canal_nombre, tenant_id)
    respuesta = responder_verificacion(await canal_obj.validar_webhook(request))
    return respuesta if respuesta is not None else {"status": "ok"}


@app.post("/webhook/{canal_nombre}/{tenant_id}")
async def webhook_handler_multi(canal_nombre: str, tenant_id: str,
                                request: Request, background_tasks: BackgroundTasks):
    """Ruta multi-canal / multi-tenant: POST /webhook/{canal}/{tenant_id}."""
    try:
        canal_obj = obtener_canal(canal_nombre, tenant_id)
        # Slack (url_verification) y Discord (PING) verifican por POST
        verificacion = responder_verificacion(await canal_obj.validar_webhook(request))
        if verificacion is not None:
            return verificacion
        return await manejar_webhook(canal_obj, request, background_tasks)
    except HTTPException:
        raise
    except ValueError as e:
        # Canal no soportado o no implementado
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Error en webhook {canal_nombre}/{tenant_id}: {e}")
        detail = str(e) if ENVIRONMENT == "development" else "Error interno"
        raise HTTPException(status_code=500, detail=detail)
