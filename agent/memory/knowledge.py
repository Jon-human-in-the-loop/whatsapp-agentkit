# agent/memory/knowledge.py — RAG sobre la base de conocimiento del tenant
#
# Indexa los archivos de knowledge/{tenant}/ en ChromaDB y permite búsqueda
# semántica. La función buscar_en_knowledge se expone como TOOL al agente.

import os
import re
import json
import pathlib
import logging

import chromadb

from agent.memory.embeddings import embeber, embeber_uno

logger = logging.getLogger("agentkit")

KNOWLEDGE_BASE = pathlib.Path("knowledge")
CHROMA_PERSIST_DIR = os.getenv("CHROMA_PERSIST_DIR", "./chroma_db")

# Chunking
CHUNK_PALABRAS = 500
CHUNK_OVERLAP = 50
MAX_BYTES_ARCHIVO = 5 * 1024 * 1024
EXTENSIONES_SOPORTADAS = {".txt", ".md", ".csv", ".json", ".pdf", ".docx"}

# Cache de clientes Chroma por ruta de persistencia
_clientes: dict[str, "chromadb.ClientAPI"] = {}


def _cliente() -> "chromadb.ClientAPI":
    ruta = os.getenv("CHROMA_PERSIST_DIR", CHROMA_PERSIST_DIR)
    if ruta not in _clientes:
        pathlib.Path(ruta).mkdir(parents=True, exist_ok=True)
        _clientes[ruta] = chromadb.PersistentClient(path=ruta)
    return _clientes[ruta]


def _slug(valor: str) -> str:
    """Normaliza un valor a un fragmento válido de nombre de colección Chroma."""
    limpio = re.sub(r"[^a-zA-Z0-9_-]", "_", valor)
    return limpio.strip("_-") or "x"


def _nombre_coleccion(tenant_id: str) -> str:
    nombre = f"kb_{_slug(tenant_id)}"
    # Chroma exige 3-512 chars; kb_ garantiza el mínimo
    return nombre[:512]


def _coleccion(tenant_id: str):
    return _cliente().get_or_create_collection(
        _nombre_coleccion(tenant_id), metadata={"hnsw:space": "cosine"}
    )


def _knowledge_dir(tenant_id: str) -> pathlib.Path:
    return KNOWLEDGE_BASE / tenant_id


# ─── Lectura de archivos ─────────────────────────────────────────────────────

def _leer_archivo(ruta: pathlib.Path) -> str:
    """Extrae texto de un archivo soportado (txt/md/csv/json/pdf/docx)."""
    ext = ruta.suffix.lower()
    try:
        if ext in (".txt", ".md", ".csv"):
            return ruta.read_text(encoding="utf-8", errors="replace")
        if ext == ".json":
            return json.dumps(json.loads(ruta.read_text(encoding="utf-8")),
                              ensure_ascii=False, indent=2)
        if ext == ".pdf":
            return _leer_pdf(ruta)
        if ext == ".docx":
            return _leer_docx(ruta)
    except Exception as e:
        logger.warning(f"No se pudo leer {ruta.name}: {e}")
    return ""


def _leer_pdf(ruta: pathlib.Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        logger.warning("pypdf no instalado — se omite PDF: %s", ruta.name)
        return ""
    lector = PdfReader(str(ruta))
    return "\n".join((p.extract_text() or "") for p in lector.pages)


def _leer_docx(ruta: pathlib.Path) -> str:
    try:
        import docx
    except ImportError:
        logger.warning("python-docx no instalado — se omite DOCX: %s", ruta.name)
        return ""
    documento = docx.Document(str(ruta))
    return "\n".join(p.text for p in documento.paragraphs)


def chunk_texto(texto: str, tamano: int = CHUNK_PALABRAS,
                overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Parte el texto en chunks de ~`tamano` palabras con solapamiento."""
    palabras = texto.split()
    if not palabras:
        return []
    chunks = []
    paso = max(1, tamano - overlap)
    for inicio in range(0, len(palabras), paso):
        chunk = " ".join(palabras[inicio:inicio + tamano])
        if chunk.strip():
            chunks.append(chunk)
        if inicio + tamano >= len(palabras):
            break
    return chunks


# ─── Indexación y búsqueda ───────────────────────────────────────────────────

async def indexar_knowledge_base(tenant_id: str) -> int:
    """
    Indexa todos los archivos de knowledge/{tenant}/ en ChromaDB.
    Reemplaza el índice anterior del tenant. Retorna el número de chunks.
    """
    carpeta = _knowledge_dir(tenant_id)
    if not carpeta.exists():
        return 0

    # Recrear la colección desde cero para reflejar el estado actual de los archivos
    nombre = _nombre_coleccion(tenant_id)
    try:
        _cliente().delete_collection(nombre)
    except Exception:
        pass
    coleccion = _cliente().get_or_create_collection(nombre, metadata={"hnsw:space": "cosine"})

    documentos, ids, metadatos = [], [], []
    for ruta in sorted(carpeta.iterdir()):
        if not ruta.is_file() or ruta.name.startswith(".") or \
                ruta.suffix.lower() not in EXTENSIONES_SOPORTADAS:
            continue
        if ruta.stat().st_size > MAX_BYTES_ARCHIVO:
            logger.warning(f"Archivo demasiado grande, omitido: {ruta.name}")
            continue
        texto = _leer_archivo(ruta)
        for i, chunk in enumerate(chunk_texto(texto)):
            documentos.append(chunk)
            ids.append(f"{ruta.name}::{i}")
            metadatos.append({"archivo": ruta.name, "chunk": i})

    if not documentos:
        return 0

    vectores = await embeber(documentos)
    coleccion.add(ids=ids, embeddings=vectores, documents=documentos, metadatas=metadatos)
    logger.info(f"[{tenant_id}] Knowledge base indexada: {len(documentos)} chunks")
    return len(documentos)


async def buscar_en_knowledge(tenant_id: str, consulta: str, top_k: int = 3) -> str:
    """Búsqueda semántica en el knowledge base del tenant. Se expone como TOOL."""
    if not consulta or not consulta.strip():
        return ""
    coleccion = _coleccion(tenant_id)
    if coleccion.count() == 0:
        return ""

    vector = await embeber_uno(consulta)
    resultado = coleccion.query(
        query_embeddings=[vector],
        n_results=min(top_k, coleccion.count()),
    )
    docs = (resultado.get("documents") or [[]])[0]
    metas = (resultado.get("metadatas") or [[]])[0]
    if not docs:
        return ""

    partes = []
    for doc, meta in zip(docs, metas):
        archivo = (meta or {}).get("archivo", "knowledge")
        partes.append(f"[{archivo}] {doc[:600]}")
    return "\n---\n".join(partes)


def estado_indice(tenant_id: str) -> dict:
    """Devuelve el estado del índice del tenant (chunks y archivos)."""
    carpeta = _knowledge_dir(tenant_id)
    archivos = []
    if carpeta.exists():
        archivos = [
            r.name for r in carpeta.iterdir()
            if r.is_file() and not r.name.startswith(".")
            and r.suffix.lower() in EXTENSIONES_SOPORTADAS
        ]
    try:
        chunks = _coleccion(tenant_id).count()
    except Exception:
        chunks = 0
    return {"chunks": chunks, "archivos": archivos}
