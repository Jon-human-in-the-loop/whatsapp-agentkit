# agent/channels/slack.py — Canal Slack (Events API)
#
# Recibe eventos por webhook y responde con chat.postMessage. Slack firma cada
# request con HMAC-SHA256 (X-Slack-Signature) sobre "v0:{timestamp}:{body}".
# El primer setup envía un challenge de url_verification que hay que devolver.

import os
import time
import hmac
import json
import hashlib
import logging
import httpx
from fastapi import Request, HTTPException
from agent.channels.base import CanalBase, MensajeUnificado, TipoCanal

logger = logging.getLogger("agentkit")

# Ventana máxima de antigüedad de un request para evitar replay attacks
_MAX_ANTIGUEDAD_SEGUNDOS = 60 * 5


class CanalSlack(CanalBase):
    """Canal usando la Events API de Slack."""

    canal = TipoCanal.SLACK

    def __init__(self, tenant_id: str = "demo"):
        super().__init__(tenant_id)
        self.bot_token = os.getenv("SLACK_BOT_TOKEN")
        self.signing_secret = os.getenv("SLACK_SIGNING_SECRET")
        self.api_base = "https://slack.com/api"

    def _verificar_firma(self, body: bytes, timestamp: str, firma: str) -> bool:
        """Valida la firma HMAC-SHA256 de Slack y rechaza requests viejos (replay)."""
        if not self.signing_secret:
            logger.error("SLACK_SIGNING_SECRET no configurado — webhook no se puede verificar")
            return False
        if not timestamp or not firma:
            return False
        try:
            if abs(time.time() - int(timestamp)) > _MAX_ANTIGUEDAD_SEGUNDOS:
                logger.warning("Request de Slack demasiado viejo — posible replay")
                return False
        except ValueError:
            return False
        base = b"v0:" + timestamp.encode() + b":" + body
        esperada = "v0=" + hmac.new(
            self.signing_secret.encode(), base, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(esperada, firma)

    async def _verificar_request(self, request: Request) -> bytes:
        """Verifica la firma y devuelve el body crudo. Lanza 403 si es inválida."""
        timestamp = request.headers.get("x-slack-request-timestamp", "")
        firma = request.headers.get("x-slack-signature", "")
        body = await request.body()
        if not self._verificar_firma(body, timestamp, firma):
            logger.warning("Firma Slack inválida — webhook rechazado")
            raise HTTPException(status_code=403, detail="Firma inválida")
        return body

    async def validar_webhook(self, request: Request) -> dict | int | None:
        """Responde el challenge de url_verification durante el setup del webhook."""
        if request.method != "POST":
            return None
        body = await self._verificar_request(request)
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return None
        if data.get("type") == "url_verification":
            return str(data.get("challenge", ""))
        return None

    async def parsear_webhook(self, request: Request) -> list[MensajeUnificado]:
        """Normaliza un event_callback de Slack a MensajeUnificado."""
        body = await self._verificar_request(request)
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return []

        if data.get("type") != "event_callback":
            return []

        event = data.get("event", {})
        if event.get("type") != "message":
            return []

        texto = event.get("text", "")
        canal_slack = event.get("channel", "")
        ts = event.get("ts", "")
        if not texto or not canal_slack:
            return []

        # Ignorar mensajes del propio bot o de subtipos automáticos
        es_propio = bool(event.get("bot_id")) or event.get("subtype") == "bot_message"

        thread_ts = event.get("thread_ts")

        return [MensajeUnificado(
            canal=self.canal,
            tenant_id=self.tenant_id,
            # usuario_id = canal de Slack (destino de la respuesta y clave de conversación)
            usuario_id=canal_slack,
            usuario_nombre=None,
            texto=texto,
            mensaje_id=f"{canal_slack}:{ts}",
            thread_id=thread_ts,
            es_propio=es_propio,
            metadata={"user": event.get("user", "")},
        )]

    async def enviar_mensaje(self, usuario_id: str, mensaje: str,
                             thread_id: str | None = None) -> bool:
        """Envía un mensaje con chat.postMessage (usuario_id es el canal de Slack)."""
        if not self.bot_token:
            logger.warning("SLACK_BOT_TOKEN no configurado")
            return False
        payload = {"channel": usuario_id, "text": mensaje}
        if thread_id:
            payload["thread_ts"] = thread_id
        headers = {
            "Authorization": f"Bearer {self.bot_token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        timeout = httpx.Timeout(10.0, connect=5.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.post(
                    f"{self.api_base}/chat.postMessage", json=payload, headers=headers
                )
                ok = r.status_code == 200 and r.json().get("ok", False)
                if not ok:
                    logger.error(f"Error Slack: {r.status_code} — {r.text}")
                return ok
        except httpx.HTTPError as e:
            logger.error(f"Error de red enviando a Slack: {e}")
            return False
