# agent/memory/long_term.py — Memoria de largo plazo (resúmenes vectorizados)
#
# Cuando una conversación acumula suficientes mensajes, se genera un resumen
# con el LLM y se guarda como vector en ChromaDB + fila en la tabla resumenes.
# Al responder, se recuperan los resúmenes más relevantes del usuario.

import os
import logging

import litellm
from sqlalchemy import select, func

from agent.memory.short_term import (
    async_session, ResumenConversacion, obtener_historial, contar_mensajes,
)
from agent.memory.embeddings import embeber_uno
from agent.memory.knowledge import _cliente, _slug

logger = logging.getLogger("agentkit")

UMBRAL_RESUMEN = 20  # mensajes sin resumir que disparan un resumen nuevo
LLM_MODEL = os.getenv("LLM_MODEL", "anthropic/claude-sonnet-4-6")


def _nombre_coleccion(tenant_id: str) -> str:
    return f"lt_{_slug(tenant_id)}"[:512]


def _coleccion(tenant_id: str):
    return _cliente().get_or_create_collection(
        _nombre_coleccion(tenant_id), metadata={"hnsw:space": "cosine"}
    )


async def _mensajes_resumidos(tenant_id: str, usuario_id: int) -> int:
    """Total de mensajes ya cubiertos por resúmenes de este usuario."""
    async with async_session() as session:
        query = select(func.coalesce(func.sum(ResumenConversacion.mensajes_cubiertos), 0)).where(
            ResumenConversacion.tenant_id == tenant_id,
            ResumenConversacion.usuario_id == usuario_id,
        )
        return int((await session.execute(query)).scalar() or 0)


async def guardar_resumen(tenant_id: str, usuario_id: int, resumen: str,
                          mensajes_cubiertos: int) -> str:
    """Persiste un resumen en la tabla resumenes y su vector en ChromaDB."""
    async with async_session() as session:
        fila = ResumenConversacion(
            tenant_id=tenant_id, usuario_id=usuario_id,
            resumen=resumen, mensajes_cubiertos=mensajes_cubiertos,
        )
        session.add(fila)
        await session.commit()
        await session.refresh(fila)
        vector_id = f"resumen-{fila.id}"
        fila.vector_id = vector_id
        await session.commit()

    vector = await embeber_uno(resumen)
    _coleccion(tenant_id).add(
        ids=[vector_id],
        embeddings=[vector],
        documents=[resumen],
        metadatas=[{"usuario_id": usuario_id}],
    )
    return vector_id


async def _generar_resumen(historial: list[dict], tenant_id: str) -> str:
    """Genera un resumen breve de un tramo de conversación con el LLM."""
    conversacion = "\n".join(f"{m['role']}: {m['content']}" for m in historial)
    prompt = (
        "Resumí en 3-5 oraciones qué habló este cliente, qué necesitó, "
        "qué quedó pendiente y cualquier dato relevante del usuario. "
        "Sé conciso y objetivo.\n\n"
        f"Conversación:\n{conversacion}"
    )
    try:
        resp = await litellm.acompletion(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
            timeout=30,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"No se pudo generar resumen: {e}")
        return ""


async def guardar_resumen_si_necesario(tenant_id: str, usuario_id: int) -> str | None:
    """
    Si hay >= UMBRAL_RESUMEN mensajes sin resumir, genera y guarda un resumen.
    Devuelve el resumen generado o None si no hizo falta.
    """
    total = await contar_mensajes(tenant_id, usuario_id)
    resumidos = await _mensajes_resumidos(tenant_id, usuario_id)
    pendientes = total - resumidos
    if pendientes < UMBRAL_RESUMEN:
        return None

    historial = await obtener_historial(tenant_id, usuario_id, limite=pendientes)
    resumen = await _generar_resumen(historial, tenant_id)
    if not resumen:
        return None
    await guardar_resumen(tenant_id, usuario_id, resumen, pendientes)
    logger.info(f"[{tenant_id}] Resumen guardado para usuario {usuario_id} ({pendientes} mensajes)")
    return resumen


async def recuperar_contexto_relevante(tenant_id: str, usuario_id: int,
                                       consulta: str, top_k: int = 3) -> str:
    """Recupera los resúmenes previos más relevantes del usuario para la consulta."""
    if not consulta or not consulta.strip():
        return ""
    coleccion = _coleccion(tenant_id)
    if coleccion.count() == 0:
        return ""

    vector = await embeber_uno(consulta)
    resultado = coleccion.query(
        query_embeddings=[vector],
        n_results=min(top_k, coleccion.count()),
        where={"usuario_id": usuario_id},
    )
    docs = (resultado.get("documents") or [[]])[0]
    if not docs:
        return ""
    return "\n".join(f"- {d}" for d in docs)
