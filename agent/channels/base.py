# agent/channels/base.py — Abstracción genérica de canal de mensajería
# Reemplaza a agent/providers/base.py (que era específico de WhatsApp).

"""
Define la interfaz común que TODOS los canales deben implementar
(WhatsApp, Telegram, Discord, Slack, Email...). El resto del sistema
solo conoce `MensajeUnificado` y `CanalBase`, nunca el detalle de cada
plataforma. Así se agrega un canal nuevo sin tocar el router ni el cerebro.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum

from fastapi import Request


class TipoCanal(str, Enum):
    """Identificador de cada canal soportado por la plataforma."""
    WHATSAPP_TWILIO = "whatsapp_twilio"
    WHATSAPP_META = "whatsapp_meta"
    TELEGRAM = "telegram"
    DISCORD = "discord"
    SLACK = "slack"
    EMAIL = "email"


@dataclass
class Adjunto:
    """Archivo adjunto recibido en un mensaje (imagen, audio, documento, video)."""
    tipo: str               # "imagen" | "audio" | "documento" | "video"
    url: str
    nombre: str | None = None


@dataclass
class MensajeUnificado:
    """
    Mensaje normalizado — mismo formato sin importar el canal de origen.
    Cada canal traduce su payload nativo a esta estructura común.
    """
    canal: TipoCanal
    tenant_id: str
    usuario_id: str             # identificador nativo del canal (teléfono, chat_id, etc.)
    usuario_nombre: str | None
    texto: str
    mensaje_id: str
    thread_id: str | None       # para canales con hilos (Slack threads, email threads)
    es_propio: bool = False     # True si lo envió el propio agente (se ignora)
    adjuntos: list[Adjunto] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


class CanalBase(ABC):
    """
    Interfaz que cada canal de mensajería debe implementar.

    Una instancia de canal está asociada a un tenant concreto: sus
    credenciales y su `tenant_id` se resuelven al construirla. El método
    `parsear_webhook` estampa ese `tenant_id` en cada `MensajeUnificado`.
    """

    canal: TipoCanal

    def __init__(self, tenant_id: str = "demo"):
        self.tenant_id = tenant_id

    @abstractmethod
    async def parsear_webhook(self, request: Request) -> list[MensajeUnificado]:
        """Extrae y normaliza los mensajes del payload del webhook a MensajeUnificado."""
        ...

    @abstractmethod
    async def enviar_mensaje(self, usuario_id: str, mensaje: str,
                             thread_id: str | None = None) -> bool:
        """Envía un mensaje de texto al usuario. Retorna True si fue exitoso."""
        ...

    async def validar_webhook(self, request: Request) -> dict | int | None:
        """Verificación GET del webhook (solo Meta la requiere). None = no aplica."""
        return None

    async def iniciar(self) -> None:
        """Setup del canal al arrancar el servidor (ej: registrar webhook en Telegram)."""
        pass
