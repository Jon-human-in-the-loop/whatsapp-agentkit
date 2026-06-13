# agent/providers/twilio.py — Shim de compatibilidad
#
# La lógica del canal de WhatsApp/Twilio se movió a
# agent/channels/whatsapp_twilio.py durante la refactorización multi-canal.
# Mantenemos este alias para no romper imports existentes (ej. tests/test_security.py).

from agent.channels.whatsapp_twilio import CanalWhatsAppTwilio as ProveedorTwilio

__all__ = ["ProveedorTwilio"]
