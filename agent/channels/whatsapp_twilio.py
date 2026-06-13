# agent/channels/whatsapp_twilio.py — Canal WhatsApp vía Twilio
# Migrado desde agent/providers/twilio.py — ahora implementa CanalBase.

import os
import hmac
import hashlib
import logging
import base64
import httpx
from fastapi import Request, HTTPException
from agent.channels.base import CanalBase, MensajeUnificado, TipoCanal

logger = logging.getLogger("agentkit")


class CanalWhatsAppTwilio(CanalBase):
    """Canal de WhatsApp usando Twilio."""

    canal = TipoCanal.WHATSAPP_TWILIO

    def __init__(self, tenant_id: str = "demo"):
        super().__init__(tenant_id)
        self.account_sid = os.getenv("TWILIO_ACCOUNT_SID")
        self.auth_token = os.getenv("TWILIO_AUTH_TOKEN")
        self.phone_number = os.getenv("TWILIO_PHONE_NUMBER")
        # Si TRUE, valida X-Twilio-Signature; usar FALSE solo para tests locales
        self.validar_firma = os.getenv("TWILIO_VALIDATE_SIGNATURE", "true").lower() == "true"

    def _verificar_firma(self, url: str, params: dict, firma_header: str) -> bool:
        """
        Valida la firma HMAC-SHA1 que Twilio envía en X-Twilio-Signature.
        Twilio firma: URL completa + parámetros del form ordenados alfabéticamente.
        Sin esta validación cualquiera podría enviar mensajes falsos al webhook.
        """
        if not self.auth_token:
            logger.error("TWILIO_AUTH_TOKEN no configurado — webhook no se puede verificar")
            return False
        if not firma_header:
            return False
        cadena = url
        for clave in sorted(params.keys()):
            cadena += clave + params[clave]
        firma_esperada = base64.b64encode(
            hmac.new(self.auth_token.encode(), cadena.encode(), hashlib.sha1).digest()
        ).decode()
        return hmac.compare_digest(firma_header, firma_esperada)

    async def parsear_webhook(self, request: Request) -> list[MensajeUnificado]:
        """Parsea el payload form-encoded de Twilio tras verificar firma."""
        form = await request.form()
        params = {k: v for k, v in form.items()}

        if self.validar_firma:
            firma_header = request.headers.get("x-twilio-signature", "")
            url = str(request.url)
            if not self._verificar_firma(url, params, firma_header):
                logger.warning("Firma Twilio inválida — webhook rechazado")
                raise HTTPException(status_code=403, detail="Firma inválida")

        texto = params.get("Body", "")
        telefono = params.get("From", "").replace("whatsapp:", "")
        mensaje_id = params.get("MessageSid", "")
        nombre = params.get("ProfileName") or None
        if not texto:
            return []
        return [MensajeUnificado(
            canal=self.canal,
            tenant_id=self.tenant_id,
            usuario_id=telefono,
            usuario_nombre=nombre,
            texto=texto,
            mensaje_id=mensaje_id,
            thread_id=None,
            es_propio=False,
        )]

    async def enviar_mensaje(self, usuario_id: str, mensaje: str,
                             thread_id: str | None = None) -> bool:
        """Envía mensaje via Twilio API. WhatsApp no usa hilos, thread_id se ignora."""
        if not all([self.account_sid, self.auth_token, self.phone_number]):
            logger.warning("Variables de Twilio no configuradas")
            return False
        url = f"https://api.twilio.com/2010-04-01/Accounts/{self.account_sid}/Messages.json"
        auth = base64.b64encode(f"{self.account_sid}:{self.auth_token}".encode()).decode()
        headers = {"Authorization": f"Basic {auth}"}
        data = {
            "From": f"whatsapp:{self.phone_number}",
            "To": f"whatsapp:{usuario_id}",
            "Body": mensaje,
        }
        # Timeout explícito: si Twilio cuelga no queremos bloquear el webhook
        timeout = httpx.Timeout(10.0, connect=5.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.post(url, data=data, headers=headers)
                if r.status_code != 201:
                    logger.error(f"Error Twilio: {r.status_code} — {r.text}")
                return r.status_code == 201
        except httpx.HTTPError as e:
            logger.error(f"Error de red enviando a Twilio: {e}")
            return False
