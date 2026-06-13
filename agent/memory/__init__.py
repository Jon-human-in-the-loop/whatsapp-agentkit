# agent/memory/__init__.py — Paquete de memoria del agente
#
# Capas:
#   short_term.py  → historial relacional reciente (SQLAlchemy)
#   long_term.py   → resúmenes vectorizados por usuario (ChromaDB) — Fase 4
#   knowledge.py   → RAG sobre knowledge/{tenant}/ (ChromaDB) — Fase 4
#
# Re-exporta la API de corto plazo para que el resto del sistema importe
# desde `agent.memory` sin conocer la estructura interna.

from agent.memory.short_term import (
    Base,
    engine,
    async_session,
    Tenant,
    Usuario,
    Mensaje,
    ResumenConversacion,
    Lead,
    inicializar_db,
    sembrar_tenants,
    obtener_o_crear_usuario,
    guardar_mensaje,
    obtener_historial,
    contar_mensajes,
    limpiar_historial,
)

__all__ = [
    "Base", "engine", "async_session",
    "Tenant", "Usuario", "Mensaje", "ResumenConversacion", "Lead",
    "inicializar_db", "sembrar_tenants", "obtener_o_crear_usuario",
    "guardar_mensaje", "obtener_historial", "contar_mensajes", "limpiar_historial",
]
