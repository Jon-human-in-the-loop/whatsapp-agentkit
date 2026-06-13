# agent/channels/telegram.py — Canal Telegram (Bot API)
#
# Telegram Bot API es REST simple. Recibimos Updates por webhook y enviamos
# con sendMessage. La seguridad del webhook se hace con un secret token que
# Telegram reenvía en el header X-Telegram-Bot-Api-Secret-Token.

import os
import hmac
import json
import logging
import httpx
from fastapi import Request, HTTPException
from agent.channels.base import CanalBase, MensajeUnificado, TipoCanal

logger = logging.getLogger("agentkit")


class CanalTelegram(CanalBase):
    """Canal de mensajería usando la Bot API de Telegram."""

    canal = TipoCanal.TELEGRAM

    def __init__(self, tenant_id: str = "demo"):
        super().__init__(tenant_id)
        self.bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        # Secret token compartido con Telegram al registrar el webhook
        self.webhook_secret = os.getenv("TELEGRAM_WEBHOOK_SECRET")
        # URL pública del webhook (si está, lo registramos al arrancar)
        self.webhook_url = os.getenv("TELEGRAM_WEBHOOK_URL")
        self.api_base = f"https://api.telegram.org/bot{self.bot_token}" if self.bot_token else None

    def _verificar_secret(self, request: Request) -> bool:
        """Valida el header X-Telegram-Bot-Api-Secret-Token si hay secret configurado."""
        if not self.webhook_secret:
            # Sin secret configurado no podemos verificar; lo advertimos pero dejamos pasar
            logger.warning("TELEGRAM_WEBHOOK_SECRET no configurado — webhook sin verificar")
            return True
        recibido = request.headers.get("x-telegram-bot-api-secret-token", "")
        return hmac.compare_digest(recibido, self.webhook_secret)

    async def iniciar(self) -> None:
        """Registra el webhook en Telegram si se configuró TELEGRAM_WEBHOOK_URL."""
        if not self.api_base or not self.webhook_url:
            return
        payload = {"url": self.webhook_url}
        if self.webhook_secret:
            payload["secret_token"] = self.webhook_secret
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
                r = await client.post(f"{self.api_base}/setWebhook", json=payload)
                if r.status_code == 200 and r.json().get("ok"):
                    logger.info("Webhook de Telegram registrado correctamente")
                else:
                    logger.error(f"No se pudo registrar webhook Telegram: {r.text}")
        except httpx.HTTPError as e:
            logger.error(f"Error registrando webhook Telegram: {e}")

    async def parsear_webhook(self, request: Request) -> list[MensajeUnificado]:
        """Normaliza un Update de Telegram a MensajeUnificado."""
        if not self._verificar_secret(request):
            logger.warning("Secret token de Telegram inválido — webhook rechazado")
            raise HTTPException(status_code=403, detail="Secret inválido")

        body = await request.body()
        try:
            update = json.loads(body)
        except json.JSONDecodeError:
            return []

        # Telegram puede mandar message, edited_message, channel_post, etc.
        mensaje = update.get("message") or update.get("edited_message")
        if not mensaje:
            return []

        texto = mensaje.get("text", "")
        if not texto:
            return []

        chat = mensaje.get("chat", {})
        remitente = mensaje.get("from", {})
        chat_id = str(chat.get("id", ""))
        nombre = remitente.get("first_name") or remitente.get("username")
        # message_thread_id existe en supergrupos con temas (forums)
        thread_id = mensaje.get("message_thread_id")

        return [MensajeUnificado(
            canal=self.canal,
            tenant_id=self.tenant_id,
            usuario_id=chat_id,
            usuario_nombre=nombre,
            texto=texto,
            mensaje_id=f"{chat_id}:{mensaje.get('message_id', '')}",
            thread_id=str(thread_id) if thread_id is not None else None,
            es_propio=bool(remitente.get("is_bot", False)),
        )]

    async def enviar_mensaje(self, usuario_id: str, mensaje: str,
                             thread_id: str | None = None) -> bool:
        """Envía un mensaje con sendMessage de la Bot API."""
        if not self.api_base:
            logger.warning("TELEGRAM_BOT_TOKEN no configurado")
            return False
        payload = {"chat_id": usuario_id, "text": mensaje}
        if thread_id:
            payload["message_thread_id"] = int(thread_id)
        timeout = httpx.Timeout(10.0, connect=5.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.post(f"{self.api_base}/sendMessage", json=payload)
                if r.status_code != 200:
                    logger.error(f"Error Telegram: {r.status_code} — {r.text}")
                return r.status_code == 200
        except httpx.HTTPError as e:
            logger.error(f"Error de red enviando a Telegram: {e}")
            return False
