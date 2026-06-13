# agent/channels/email.py — Canal Email (IMAP polling + SMTP)
#
# A diferencia de los demás canales, Email no usa webhook: hace polling IMAP
# de los correos no leídos y responde por SMTP. El loop de polling se arranca
# en iniciar() y despacha cada correo nuevo al pipeline vía self.procesar.

import os
import ssl
import time
import email
import asyncio
import logging
import smtplib
import imaplib
from email.message import EmailMessage
from email.header import decode_header, make_header
from email.utils import parseaddr
from typing import Awaitable, Callable

from fastapi import Request
from agent.channels.base import CanalBase, MensajeUnificado, TipoCanal

logger = logging.getLogger("agentkit")


class CanalEmail(CanalBase):
    """Canal de Email: lee por IMAP y responde por SMTP."""

    canal = TipoCanal.EMAIL

    def __init__(self, tenant_id: str = "demo"):
        super().__init__(tenant_id)
        # IMAP entrante
        self.imap_host = os.getenv("EMAIL_IMAP_HOST")
        self.imap_port = int(os.getenv("EMAIL_IMAP_PORT", "993"))
        self.imap_user = os.getenv("EMAIL_IMAP_USER")
        self.imap_password = os.getenv("EMAIL_IMAP_PASSWORD")
        self.intervalo = int(os.getenv("EMAIL_CHECK_INTERVAL_SECONDS", "30"))
        # SMTP saliente
        self.smtp_host = os.getenv("EMAIL_SMTP_HOST")
        self.smtp_port = int(os.getenv("EMAIL_SMTP_PORT", "587"))
        self.smtp_user = os.getenv("EMAIL_SMTP_USER")
        self.smtp_password = os.getenv("EMAIL_SMTP_PASSWORD")
        self.from_addr = self.smtp_user or self.imap_user

        # Callback de procesamiento (lo inyecta main.py para no crear ciclo)
        self.procesar: Callable[["CanalEmail", MensajeUnificado], Awaitable[None]] | None = None
        self._task: asyncio.Task | None = None

    # ─── Parsing (puro, testeable sin red) ──────────────────────────────────

    @staticmethod
    def _decodificar_header(valor: str) -> str:
        """Decodifica un header MIME (RFC 2047) a texto legible."""
        if not valor:
            return ""
        try:
            return str(make_header(decode_header(valor)))
        except (ValueError, UnicodeDecodeError):
            return valor

    @staticmethod
    def _extraer_texto(em: email.message.Message) -> str:
        """Extrae el cuerpo text/plain de un email (multipart o simple)."""
        if em.is_multipart():
            for parte in em.walk():
                if parte.get_content_type() == "text/plain" and \
                        "attachment" not in str(parte.get("Content-Disposition", "")):
                    payload = parte.get_payload(decode=True)
                    if payload:
                        charset = parte.get_content_charset() or "utf-8"
                        return payload.decode(charset, errors="replace")
            return ""
        payload = em.get_payload(decode=True)
        if payload:
            charset = em.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
        return ""

    def _email_a_mensaje(self, raw: bytes) -> MensajeUnificado | None:
        """Convierte un email crudo (RFC822) en MensajeUnificado."""
        em = email.message_from_bytes(raw)
        nombre_raw, direccion = parseaddr(em.get("From", ""))
        if not direccion:
            return None
        nombre = self._decodificar_header(nombre_raw)
        texto = self._extraer_texto(em).strip()
        if not texto:
            return None
        message_id = em.get("Message-ID", "").strip()
        # Para mantener el hilo del email al responder
        thread_ref = (em.get("References") or em.get("In-Reply-To") or message_id).strip()
        asunto = self._decodificar_header(em.get("Subject", ""))

        return MensajeUnificado(
            canal=self.canal,
            tenant_id=self.tenant_id,
            usuario_id=direccion,
            usuario_nombre=nombre or None,
            texto=texto,
            mensaje_id=message_id or f"{direccion}:{asunto}",
            thread_id=thread_ref or None,
            es_propio=(direccion.lower() == (self.from_addr or "").lower()),
            metadata={"asunto": asunto, "message_id": message_id},
        )

    # ─── IMAP (sync, corre en thread) ────────────────────────────────────────

    def _leer_no_leidos(self) -> list[MensajeUnificado]:
        """Lee los correos no leídos por IMAP y los marca como leídos."""
        mensajes: list[MensajeUnificado] = []
        try:
            conn = imaplib.IMAP4_SSL(self.imap_host, self.imap_port)
            conn.login(self.imap_user, self.imap_password)
            conn.select("INBOX")
            typ, data = conn.search(None, "UNSEEN")
            if typ == "OK":
                for num in data[0].split():
                    typ, msg_data = conn.fetch(num, "(RFC822)")
                    if typ != "OK" or not msg_data or not msg_data[0]:
                        continue
                    raw = msg_data[0][1]
                    conn.store(num, "+FLAGS", "\\Seen")
                    msg = self._email_a_mensaje(raw)
                    if msg:
                        mensajes.append(msg)
            conn.logout()
        except (imaplib.IMAP4.error, OSError) as e:
            logger.error(f"Error leyendo IMAP: {e}")
        return mensajes

    # ─── SMTP (sync, corre en thread) ────────────────────────────────────────

    def _enviar_smtp(self, destino: str, mensaje: str, thread_id: str | None) -> bool:
        """Envía un email por SMTP (STARTTLS)."""
        msg = EmailMessage()
        msg["From"] = self.from_addr
        msg["To"] = destino
        msg["Subject"] = "Re: tu consulta"
        if thread_id:
            msg["In-Reply-To"] = thread_id
            msg["References"] = thread_id
        msg.set_content(mensaje)
        try:
            contexto = ssl.create_default_context()
            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=15) as servidor:
                servidor.starttls(context=contexto)
                if self.smtp_user and self.smtp_password:
                    servidor.login(self.smtp_user, self.smtp_password)
                servidor.send_message(msg)
            return True
        except (smtplib.SMTPException, OSError) as e:
            logger.error(f"Error enviando SMTP: {e}")
            return False

    # ─── Interfaz CanalBase ──────────────────────────────────────────────────

    async def parsear_webhook(self, request: Request) -> list[MensajeUnificado]:
        """Email no usa webhook; la entrada llega por polling IMAP."""
        return []

    async def enviar_mensaje(self, usuario_id: str, mensaje: str,
                             thread_id: str | None = None) -> bool:
        """Envía la respuesta por SMTP (usuario_id es la dirección de email)."""
        if not self.smtp_host or not self.from_addr:
            logger.warning("SMTP no configurado — no se puede enviar email")
            return False
        return await asyncio.to_thread(self._enviar_smtp, usuario_id, mensaje, thread_id)

    async def iniciar(self) -> None:
        """Arranca el loop de polling IMAP si está configurado."""
        if not all([self.imap_host, self.imap_user, self.imap_password]):
            logger.info("IMAP no configurado para Email — polling deshabilitado")
            return
        self._task = asyncio.create_task(self._loop_polling())
        logger.info(f"Polling de Email iniciado (cada {self.intervalo}s)")

    async def _loop_polling(self) -> None:
        """Revisa correos nuevos periódicamente y los despacha al pipeline."""
        from agent.security import ya_procesado, marcar_procesado
        while True:
            try:
                mensajes = await asyncio.to_thread(self._leer_no_leidos)
                for msg in mensajes:
                    if msg.es_propio or ya_procesado(msg.mensaje_id):
                        continue
                    marcar_procesado(msg.mensaje_id)
                    if self.procesar:
                        await self.procesar(self, msg)
            except Exception as e:
                logger.error(f"Error en loop de polling Email: {e}")
            await asyncio.sleep(self.intervalo)
