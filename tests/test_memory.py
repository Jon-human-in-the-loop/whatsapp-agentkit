# tests/test_memory.py — Tests de memoria: chunking, embeddings, RAG y largo plazo
#
# Corren 100% offline: EMBEDDING_MODEL=local fuerza el embedding determinista
# por hashing, y ChromaDB persiste en un directorio temporal por test.

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Forzar embeddings locales antes de importar los módulos de memoria
os.environ["EMBEDDING_MODEL"] = "local"


@pytest.fixture(autouse=True)
def chroma_temporal(monkeypatch):
    """Aísla ChromaDB en un directorio temporal y limpia el cache de clientes."""
    d = tempfile.mkdtemp()
    monkeypatch.setenv("CHROMA_PERSIST_DIR", d)
    from agent.memory import knowledge
    knowledge._clientes.clear()
    yield d
    knowledge._clientes.clear()


# ─── Embeddings ──────────────────────────────────────────────────────────────

class TestEmbeddings:
    async def test_determinista(self):
        from agent.memory.embeddings import embeber_uno
        a = await embeber_uno("hola mundo")
        b = await embeber_uno("hola mundo")
        assert a == b

    async def test_dimension_fija(self):
        from agent.memory.embeddings import embeber_uno, DIM_LOCAL
        v = await embeber_uno("texto cualquiera")
        assert len(v) == DIM_LOCAL

    async def test_textos_distintos_difieren(self):
        from agent.memory.embeddings import embeber_uno
        assert await embeber_uno("precios y planes") != await embeber_uno("horario de atención")


# ─── Chunking ────────────────────────────────────────────────────────────────

class TestChunking:
    def test_chunk_respeta_tamano_y_overlap(self):
        from agent.memory.knowledge import chunk_texto
        texto = " ".join(str(i) for i in range(1200))
        chunks = chunk_texto(texto, tamano=500, overlap=50)
        assert len(chunks) >= 2
        assert all(len(c.split()) <= 500 for c in chunks)

    def test_texto_vacio_sin_chunks(self):
        from agent.memory.knowledge import chunk_texto
        assert chunk_texto("") == []

    def test_texto_corto_un_chunk(self):
        from agent.memory.knowledge import chunk_texto
        assert chunk_texto("hola mundo") == ["hola mundo"]


# ─── RAG sobre knowledge base ────────────────────────────────────────────────

class TestKnowledgeRAG:
    def _crear_knowledge(self, tmp_path, tenant="t_demo"):
        carpeta = tmp_path / "knowledge" / tenant
        carpeta.mkdir(parents=True)
        (carpeta / "precios.txt").write_text(
            "Los planes cuestan 100 dólares por mes e incluyen soporte premium.",
            encoding="utf-8",
        )
        (carpeta / "horario.md").write_text(
            "Atendemos de lunes a viernes de 9 a 18 horas.", encoding="utf-8",
        )
        return tenant

    async def test_indexa_y_busca(self, tmp_path, monkeypatch):
        from agent.memory import knowledge
        monkeypatch.chdir(tmp_path)
        tenant = self._crear_knowledge(tmp_path)
        n = await knowledge.indexar_knowledge_base(tenant)
        assert n >= 2
        resultado = await knowledge.buscar_en_knowledge(tenant, "cuánto cuestan los planes")
        assert "100 dólares" in resultado

    async def test_busqueda_sin_indice_vacia(self):
        from agent.memory import knowledge
        assert await knowledge.buscar_en_knowledge("inexistente", "algo") == ""

    async def test_estado_indice(self, tmp_path, monkeypatch):
        from agent.memory import knowledge
        monkeypatch.chdir(tmp_path)
        tenant = self._crear_knowledge(tmp_path, tenant="t_estado")
        await knowledge.indexar_knowledge_base(tenant)
        estado = knowledge.estado_indice(tenant)
        assert estado["chunks"] >= 2
        assert "precios.txt" in estado["archivos"]


# ─── Memoria de largo plazo ──────────────────────────────────────────────────

class TestLargoPlazo:
    @pytest.fixture(autouse=True)
    def db_temporal(self, tmp_path, monkeypatch):
        """DB SQLite temporal e import fresco de short_term con esa URL."""
        monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/lt.db")
        import importlib
        from agent.memory import short_term
        importlib.reload(short_term)
        yield

    async def test_guardar_y_recuperar_resumen(self, tmp_path, monkeypatch):
        from agent.memory import short_term, long_term
        # long_term referencia async_session de short_term; recargar para usar la DB temporal
        import importlib
        importlib.reload(long_term)
        await short_term.inicializar_db()

        await long_term.guardar_resumen(
            "demo", 1, "El cliente preguntó por precios de los planes premium.", 20
        )
        await long_term.guardar_resumen(
            "demo", 1, "El cliente quería saber el horario de atención.", 20
        )
        ctx = await long_term.recuperar_contexto_relevante("demo", 1, "precio del plan")
        assert "precios" in ctx.lower()
        # El filtro por usuario_id aísla: otro usuario no ve estos resúmenes
        assert await long_term.recuperar_contexto_relevante("demo", 999, "precio") == ""
