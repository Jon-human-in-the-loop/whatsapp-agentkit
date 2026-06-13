# agent/channels/discord.py — Canal Discord (Interactions API)
#
# Usa el modelo de Interactions (webhooks HTTP), no el Gateway por websocket,
# para encajar con la arquitectura webhook de FastAPI. Discord firma cada
# request con Ed25519 (headers X-Signature-Ed25519 / X-Signature-Timestamp).
# El texto del usuario llega como opción de un slash command.

import os
import json
import logging
import httpx
from fastapi import Request, HTTPException
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.exceptions import InvalidSignature

from agent.channels.base import CanalBase, MensajeUnificado, TipoCanal

logger = logging.getLogger("agentkit")

# Tipos de interacción de Discord
_PING = 1
_APPLICATION_COMMAND = 2


class CanalDiscord(CanalBase):
    """Canal usando la Interactions API de Discord."""

    canal = TipoCanal.DISCORD

    def __init__(self, tenant_id: str = "demo"):
        super().__init__(tenant_id)
        self.bot_token = os.getenv("DISCORD_BOT_TOKEN")
        self.public_key = os.getenv("DISCORD_PUBLIC_KEY")
        self.api_base = "https://discord.com/api/v10"

    def _verificar_firma(self, body: bytes, firma_hex: str, timestamp: str) -> bool:
        """Valida la firma Ed25519 que Discord envía en cada request."""
        if not self.public_key:
            logger.error("DISCORD_PUBLIC_KEY no configurado — webhook no se puede verificar")
            return False
        if not firma_hex or not timestamp:
            return False
        try:
            verificador = Ed25519PublicKey.from_public_bytes(bytes.fromhex(self.public_key))
            verificador.verify(bytes.fromhex(firma_hex), timestamp.encode() + body)
            return True
        except (InvalidSignature, ValueError):
            return False

    async def _verificar_request(self, request: Request) -> bytes:
        """Verifica la firma y devuelve el body crudo. Lanza 401 si es inválida."""
        firma = request.headers.get("x-signature-ed25519", "")
        timestamp = request.headers.get("x-signature-timestamp", "")
        body = await request.body()
        if not self._verificar_firma(body, firma, timestamp):
            logger.warning("Firma Discord inválida — webhook rechazado")
            raise HTTPException(status_code=401, detail="Firma inválida")
        return body

    async def validar_webhook(self, request: Request) -> dict | int | None:
        """
        Discord valida el endpoint enviando un PING firmado: hay que responder
        PONG ({"type": 1}). Las firmas inválidas deben devolver 401.
        """
        if request.method != "POST":
            return None
        body = await self._verificar_request(request)
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return None
        if data.get("type") == _PING:
            return {"type": _PING}  # PONG
        return None

    async def parsear_webhook(self, request: Request) -> list[MensajeUnificado]:
        """Normaliza una interacción de slash command a MensajeUnificado."""
        body = await self._verificar_request(request)
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return []

        if data.get("type") != _APPLICATION_COMMAND:
            return []

        # El usuario puede venir en member.user (servidor) o user (DM)
        usuario = (data.get("member") or {}).get("user") or data.get("user") or {}
        user_id = str(usuario.get("id", ""))
        nombre = usuario.get("global_name") or usuario.get("username")

        # El texto llega como opción string del comando (ej: /chat mensaje:"hola")
        opciones = data.get("data", {}).get("options", [])
        texto = ""
        for op in opciones:
            if op.get("type") == 3 and op.get("value"):  # type 3 = STRING
                texto = op["value"]
                break

        if not texto or not user_id:
            return []

        return [MensajeUnificado(
            canal=self.canal,
            tenant_id=self.tenant_id,
            usuario_id=user_id,
            usuario_nombre=nombre,
            texto=texto,
            mensaje_id=str(data.get("id", "")),
            thread_id=None,
            es_propio=bool(usuario.get("bot", False)),
            metadata={"channel_id": data.get("channel_id", "")},
        )]

    async def _crear_dm(self, client: httpx.AsyncClient, user_id: str) -> str | None:
        """Crea (o recupera) el canal de DM con un usuario y devuelve su id."""
        r = await client.post(
            f"{self.api_base}/users/@me/channels",
            headers={"Authorization": f"Bot {self.bot_token}"},
            json={"recipient_id": user_id},
        )
        if r.status_code in (200, 201):
            return r.json().get("id")
        logger.error(f"No se pudo crear DM Discord: {r.status_code} — {r.text}")
        return None

    async def enviar_mensaje(self, usuario_id: str, mensaje: str,
                             thread_id: str | None = None) -> bool:
        """Envía un DM al usuario vía la REST API del bot."""
        if not self.bot_token:
            logger.warning("DISCORD_BOT_TOKEN no configurado")
            return False
        timeout = httpx.Timeout(10.0, connect=5.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                channel_id = await self._crear_dm(client, usuario_id)
                if not channel_id:
                    return False
                r = await client.post(
                    f"{self.api_base}/channels/{channel_id}/messages",
                    headers={"Authorization": f"Bot {self.bot_token}"},
                    json={"content": mensaje},
                )
                if r.status_code not in (200, 201):
                    logger.error(f"Error Discord: {r.status_code} — {r.text}")
                return r.status_code in (200, 201)
        except httpx.HTTPError as e:
            logger.error(f"Error de red enviando a Discord: {e}")
            return False
