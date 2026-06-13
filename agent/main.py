# agent/main.py — Servidor FastAPI + Webhook de WhatsApp — HELIX · AI
import os
import asyncio
import random
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
from dotenv import load_dotenv

from agent.brain import generar_respuesta
from agent.memory import inicializar_db, guardar_mensaje, obtener_historial
from agent.channels import obtener_canal
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

# Tenant por defecto mientras el sistema es single-tenant.
# La arquitectura multi-tenant (Fase 2) resuelve el tenant desde la URL.
TENANT_ID = os.getenv("DEFAULT_TENANT_ID", "demo")
# Canal principal de WhatsApp (Twilio o Meta), asociado al tenant por defecto.
canal = obtener_canal(os.getenv("WHATSAPP_PROVIDER", ""), TENANT_ID)
PORT = int(os.getenv("PORT", 8000))


@asynccontextmanager
async def lifespan(app: FastAPI):
    await inicializar_db()
    logger.info("Base de datos inicializada")
    await canal.iniciar()
    logger.info(f"Servidor AgentKit — HELIX · AI corriendo en puerto {PORT}")
    logger.info(f"Canal activo: {canal.__class__.__name__} (tenant: {TENANT_ID})")
    yield


app = FastAPI(
    title="Sofía — Agente WhatsApp de HELIX · AI",
    version="1.0.0",
    lifespan=lifespan
)


@app.get("/")
async def health_check():
    return {"status": "ok", "service": "helix-ai-agentkit", "agente": "Sofía"}


@app.get("/webhook")
async def webhook_verificacion(request: Request):
    resultado = await canal.validar_webhook(request)
    if resultado is not None:
        return PlainTextResponse(str(resultado))
    return {"status": "ok"}


@app.post("/webhook")
async def webhook_handler(request: Request):
    try:
        mensajes = await canal.parsear_webhook(request)

        for msg in mensajes:
            if msg.es_propio or not msg.texto:
                continue

            if ya_procesado(msg.mensaje_id):
                logger.info(f"Mensaje duplicado ignorado: {msg.mensaje_id}")
                continue
            marcar_procesado(msg.mensaje_id)

            if rate_limit_excedido(msg.usuario_id):
                logger.warning(f"Rate limit excedido: {msg.usuario_id}")
                await canal.enviar_mensaje(
                    msg.usuario_id,
                    "Enviaste muchos mensajes muy rápido. Por favor esperá un momento e intentá de nuevo 🙏",
                    msg.thread_id,
                )
                continue

            msg.texto = sanitizar_mensaje(msg.texto)
            if not msg.texto:
                continue

            logger.info(f"Mensaje de {msg.usuario_id}: {msg.texto}")

            historial = await obtener_historial(msg.usuario_id)
            respuesta = await generar_respuesta(msg.texto, historial)

            await guardar_mensaje(msg.usuario_id, "user", msg.texto)
            await guardar_mensaje(msg.usuario_id, "assistant", respuesta)

            # Partir en bloques si hay párrafos dobles o la respuesta es larga
            bloques = [b.strip() for b in respuesta.split("\n\n") if b.strip()]
            if len(bloques) == 1 and len(respuesta) > 280:
                # Partir por oraciones si no hay saltos de párrafo
                import re
                partes = re.split(r'(?<=[.!?])\s+', respuesta)
                bloques = []
                actual = ""
                for parte in partes:
                    if len(actual) + len(parte) < 280:
                        actual = (actual + " " + parte).strip()
                    else:
                        if actual:
                            bloques.append(actual)
                        actual = parte
                if actual:
                    bloques.append(actual)

            for i, bloque in enumerate(bloques):
                # Delay humano: más largo para el primer mensaje, más corto entre bloques
                if i == 0:
                    delay = min(2 + len(bloque) / 80, 8) + random.uniform(0, 1.5)
                else:
                    delay = random.uniform(1.5, 3)
                await asyncio.sleep(delay)
                await canal.enviar_mensaje(msg.usuario_id, bloque, msg.thread_id)

            logger.info(f"Respuesta a {msg.usuario_id} ({len(bloques)} bloque/s): {respuesta[:80]}...")

        return {"status": "ok"}

    except Exception as e:
        logger.error(f"Error en webhook: {e}")
        detail = str(e) if ENVIRONMENT == "development" else "Error interno"
        raise HTTPException(status_code=500, detail=detail)
