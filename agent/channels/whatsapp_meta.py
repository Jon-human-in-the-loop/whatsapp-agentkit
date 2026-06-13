# agent/channels/whatsapp_meta.py — Canal WhatsApp vía Meta Cloud API
# Implementa CanalBase. La API oficial de WhatsApp de Meta.

import os
import json
import hmac
import hashlib
import logging
import httpx
from fastapi import Request, HTTPException
from agent.channels.base import CanalBase, MensajeUnificado, TipoCanal

logger = logging.getLogger("agentkit")


class CanalWhatsAppMeta(CanalBase):
    """Canal de WhatsApp usando la API oficial de Meta (Cloud API)."""

    canal = TipoCanal.WHATSAPP_META

    def __init__(self, tenant_id: str = "demo"):
        super().__init__(tenant_id)
        self.access_token = os.getenv("META_ACCESS_TOKEN")
        self.phone_number_id = os.getenv("META_PHONE_NUMBER_ID")
        self.verify_token = os.getenv("META_VERIFY_TOKEN", "agentkit-verify")
        # App Secret para validar firma del webhook (X-Hub-Signature-256)
        self.app_secret = os.getenv("META_APP_SECRET")
        self.api_version = "v21.0"

    async def validar_webhook(self, request: Request) -> dict | int | None:
        """Meta requiere verificación GET con hub.verify_token."""
        params = request.query_params
        mode = params.get("hub.mode")
        token = params.get("hub.verify_token")
        challenge = params.get("hub.challenge")
        if mode == "subscribe" and token == self.verify_token:
            # Meta espera el challenge como respuesta en texto plano
            return int(challenge)
        return None

    def _verificar_firma(self, body: bytes, firma_header: str) -> bool:
        """
        Valida la firma HMAC-SHA256 que Meta envía en X-Hub-Signature-256.
        Sin esta validación cualquiera podría enviar mensajes falsos al webhook.
        """
        if not self.app_secret:
            logger.error("META_APP_SECRET no configurado — webhook no se puede verificar")
            return False
        if not firma_header or not firma_header.startswith("sha256="):
            return False
        firma_recibida = firma_header.split("=", 1)[1]
        firma_esperada = hmac.new(
            self.app_secret.encode(), body, hashlib.sha256
        ).hexdigest()
        # compare_digest evita timing attacks
        return hmac.compare_digest(firma_recibida, firma_esperada)

    async def parsear_webhook(self, request: Request) -> list[MensajeUnificado]:
        """Parsea el payload anidado de Meta Cloud API tras verificar firma."""
        body = await request.body()
        firma_header = request.headers.get("x-hub-signature-256", "")
        if not self._verificar_firma(body, firma_header):
            logger.warning("Firma Meta inválida — webhook rechazado")
            raise HTTPException(status_code=403, detail="Firma inválida")

        payload = json.loads(body)
        mensajes = []
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                # Mapa de wa_id → nombre de perfil para enriquecer el mensaje
                nombres = {
                    c.get("wa_id"): c.get("profile", {}).get("name")
                    for c in value.get("contacts", [])
                }
                for msg in value.get("messages", []):
                    if msg.get("type") != "text":
                        continue
                    remitente = msg.get("from", "")
                    mensajes.append(MensajeUnificado(
                        canal=self.canal,
                        tenant_id=self.tenant_id,
                        usuario_id=remitente,
                        usuario_nombre=nombres.get(remitente),
                        texto=msg.get("text", {}).get("body", ""),
                        mensaje_id=msg.get("id", ""),
                        thread_id=None,
                        es_propio=False,  # Meta solo envía mensajes entrantes
                    ))
        return mensajes

    async def enviar_mensaje(self, usuario_id: str, mensaje: str,
                             thread_id: str | None = None) -> bool:
        """Envía mensaje via Meta WhatsApp Cloud API. thread_id se ignora."""
        if not self.access_token or not self.phone_number_id:
            logger.warning("META_ACCESS_TOKEN o META_PHONE_NUMBER_ID no configurados")
            return False
        url = f"https://graph.facebook.com/{self.api_version}/{self.phone_number_id}/messages"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
        payload = {
            "messaging_product": "whatsapp",
            "to": usuario_id,
            "type": "text",
            "text": {"body": mensaje},
        }
        # Timeout explícito: si Meta cuelga no queremos bloquear el webhook
        timeout = httpx.Timeout(10.0, connect=5.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.post(url, json=payload, headers=headers)
                if r.status_code != 200:
                    logger.error(f"Error Meta API: {r.status_code} — {r.text}")
                return r.status_code == 200
        except httpx.HTTPError as e:
            logger.error(f"Error de red enviando a Meta: {e}")
            return False
