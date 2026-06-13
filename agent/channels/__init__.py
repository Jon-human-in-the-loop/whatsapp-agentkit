# agent/channels/__init__.py — Factory de canales de mensajería
# Reemplaza a agent/providers/__init__.py.

"""
Selecciona la implementación de canal según un nombre lógico.
Acepta tanto los nombres nuevos (whatsapp_twilio, telegram, ...) como los
alias legacy (twilio, meta) para no romper configuraciones existentes.
"""

from agent.channels.base import CanalBase, MensajeUnificado, TipoCanal, Adjunto

# Alias legacy → tipo de canal canónico
_MAPA_CANALES = {
    "twilio": "whatsapp_twilio",
    "meta": "whatsapp_meta",
    "whatsapp_twilio": "whatsapp_twilio",
    "whatsapp_meta": "whatsapp_meta",
    "telegram": "telegram",
    "discord": "discord",
    "slack": "slack",
    "email": "email",
}


def obtener_canal(nombre: str, tenant_id: str = "demo") -> CanalBase:
    """Retorna una instancia del canal pedido, asociada al tenant indicado."""
    clave = (nombre or "").lower()
    if not clave:
        raise ValueError(
            "Canal no especificado. Opciones: " + ", ".join(sorted(set(_MAPA_CANALES.values())))
        )
    tipo = _MAPA_CANALES.get(clave)
    if not tipo:
        raise ValueError(
            f"Canal no soportado: {nombre}. "
            f"Opciones: {', '.join(sorted(set(_MAPA_CANALES.values())))}"
        )

    if tipo == "whatsapp_twilio":
        from agent.channels.whatsapp_twilio import CanalWhatsAppTwilio
        return CanalWhatsAppTwilio(tenant_id)
    if tipo == "whatsapp_meta":
        from agent.channels.whatsapp_meta import CanalWhatsAppMeta
        return CanalWhatsAppMeta(tenant_id)
    if tipo == "telegram":
        from agent.channels.telegram import CanalTelegram
        return CanalTelegram(tenant_id)
    if tipo == "discord":
        from agent.channels.discord import CanalDiscord
        return CanalDiscord(tenant_id)
    if tipo == "slack":
        from agent.channels.slack import CanalSlack
        return CanalSlack(tenant_id)
    if tipo == "email":
        from agent.channels.email import CanalEmail
        return CanalEmail(tenant_id)

    raise ValueError(f"Canal '{tipo}' aún no implementado.")


__all__ = ["obtener_canal", "CanalBase", "MensajeUnificado", "TipoCanal", "Adjunto"]
