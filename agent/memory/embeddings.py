# agent/memory/embeddings.py — Generación de embeddings para memoria vectorial
#
# Usa el modelo configurado en EMBEDDING_MODEL vía LiteLLM (OpenAI, etc.).
# Si no hay red/clave o se pide explícitamente "local", cae a un embedding
# determinista por hashing de tokens: degrada la calidad semántica pero deja
# el sistema (y los tests) funcionando sin dependencias externas.

import os
import re
import math
import hashlib
import logging

import litellm

logger = logging.getLogger("agentkit")

EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
DIM_LOCAL = 256  # dimensión del embedding local de fallback


def _usar_local() -> bool:
    return EMBEDDING_MODEL.strip().lower() in ("local", "hash", "local-hash", "")


def _embed_local(texto: str, dim: int = DIM_LOCAL) -> list[float]:
    """Embedding determinista por bolsa de palabras hasheada y normalizada."""
    vec = [0.0] * dim
    for token in re.findall(r"\w+", texto.lower()):
        h = int(hashlib.md5(token.encode()).hexdigest(), 16)
        vec[h % dim] += 1.0
    norma = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norma for v in vec]


async def embeber(textos: list[str]) -> list[list[float]]:
    """Devuelve los embeddings de una lista de textos."""
    if not textos:
        return []
    if _usar_local():
        return [_embed_local(t) for t in textos]
    try:
        resp = await litellm.aembedding(model=EMBEDDING_MODEL, input=textos)
        return [item["embedding"] for item in resp.data]
    except Exception as e:
        logger.warning(f"Embeddings remotos fallaron ({e}); usando fallback local")
        return [_embed_local(t) for t in textos]


async def embeber_uno(texto: str) -> list[float]:
    """Embedding de un solo texto."""
    return (await embeber([texto]))[0]
