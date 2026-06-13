# agent/providers/__init__.py — Shim de compatibilidad
#
# Reemplazado por agent/channels/. Mantenemos obtener_proveedor() delegando
# a la nueva factory para no romper código o configuraciones legacy.

import os
from agent.channels import obtener_canal, CanalBase


def obtener_proveedor() -> CanalBase:
    """Compat: retorna el canal de WhatsApp configurado en WHATSAPP_PROVIDER."""
    proveedor = os.getenv("WHATSAPP_PROVIDER", "")
    tenant_id = os.getenv("DEFAULT_TENANT_ID", "demo")
    return obtener_canal(proveedor, tenant_id)
