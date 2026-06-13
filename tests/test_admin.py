# tests/test_admin.py — Tests del Admin API REST
#
# Monta el router admin en una app aislada (sin lifespan), con tenants y DB
# temporales. Offline: EMBEDDING_MODEL=local y ChromaDB temporal.

import os
import sys
import importlib
import tempfile

import pytest
import httpx
import yaml
from httpx import ASGITransport
from fastapi import FastAPI

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["EMBEDDING_MODEL"] = "local"

ADMIN_KEY = "clave-admin-de-prueba"
HEADERS = {"X-Admin-Key": ADMIN_KEY}


@pytest.fixture
async def cliente(tmp_path, monkeypatch):
    # Entorno admin
    monkeypatch.setenv("ADMIN_ENABLED", "true")
    monkeypatch.setenv("ADMIN_API_KEY", ADMIN_KEY)
    monkeypatch.setenv("CHROMA_PERSIST_DIR", tempfile.mkdtemp())
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/admin.db")

    # Tenant temporal "acme"
    tenants_dir = tmp_path / "tenants"
    acme = tenants_dir / "acme"
    acme.mkdir(parents=True)
    (acme / "business.yaml").write_text(
        yaml.safe_dump({"negocio": {"nombre": "Acme SA"}}, allow_unicode=True), encoding="utf-8"
    )
    (acme / "prompts.yaml").write_text(
        yaml.safe_dump({"system_prompt": "Sos el agente de Acme."}, allow_unicode=True),
        encoding="utf-8",
    )
    (acme / "tenant.yaml").write_text(
        yaml.safe_dump({"nombre": "Acme SA", "canales": ["telegram"],
                        "tools": ["knowledge_search"]}, allow_unicode=True),
        encoding="utf-8",
    )

    # Recargar módulos para que tomen la DB temporal
    from agent.memory import knowledge, short_term
    knowledge._clientes.clear()
    importlib.reload(short_term)
    from admin import metrics, api as admin_api
    importlib.reload(metrics)
    importlib.reload(admin_api)

    # Apuntar el gestor de tenants al directorio temporal
    from agent.tenants import gestor_tenants
    gestor_tenants.base_dir = tenants_dir
    gestor_tenants.recargar()

    await short_term.inicializar_db()

    app = FastAPI()
    app.include_router(admin_api.router)

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        ac._short_term = short_term  # para que los tests inserten datos
        yield ac

    await short_term.engine.dispose()


# ─── Auth ────────────────────────────────────────────────────────────────────

class TestAuth:
    async def test_sin_key_rechaza(self, cliente):
        r = await cliente.get("/admin/tenants")
        assert r.status_code == 401

    async def test_key_incorrecta_rechaza(self, cliente):
        r = await cliente.get("/admin/tenants", headers={"X-Admin-Key": "mal"})
        assert r.status_code == 401


# ─── Tenants ─────────────────────────────────────────────────────────────────

class TestTenants:
    async def test_listar(self, cliente):
        r = await cliente.get("/admin/tenants", headers=HEADERS)
        assert r.status_code == 200
        ids = [t["tenant_id"] for t in r.json()["tenants"]]
        assert "acme" in ids

    async def test_crear_y_aparece(self, cliente):
        body = {
            "tenant_id": "nuevo", "nombre": "Nuevo Negocio",
            "system_prompt": "Sos el agente de Nuevo.", "canales": ["slack"],
            "tools": ["knowledge_search", "registrar_lead"],
        }
        r = await cliente.post("/admin/tenants", json=body, headers=HEADERS)
        assert r.status_code == 201
        r2 = await cliente.get("/admin/tenants", headers=HEADERS)
        assert "nuevo" in [t["tenant_id"] for t in r2.json()["tenants"]]

    async def test_crear_duplicado_409(self, cliente):
        body = {"tenant_id": "acme", "nombre": "x", "system_prompt": "x"}
        r = await cliente.post("/admin/tenants", json=body, headers=HEADERS)
        assert r.status_code == 409

    async def test_actualizar(self, cliente):
        r = await cliente.put("/admin/tenants/acme",
                              json={"tools": ["knowledge_search", "escalar_a_humano"]},
                              headers=HEADERS)
        assert r.status_code == 200
        r2 = await cliente.get("/admin/tenants", headers=HEADERS)
        acme = next(t for t in r2.json()["tenants"] if t["tenant_id"] == "acme")
        assert "escalar_a_humano" in acme["tools"]

    async def test_tenant_inexistente_404(self, cliente):
        r = await cliente.get("/admin/tenants/fantasma/metrics", headers=HEADERS)
        assert r.status_code == 404


# ─── Conversaciones, leads y métricas ────────────────────────────────────────

class TestDatos:
    async def _sembrar(self, cliente):
        st = cliente._short_term
        uid = await st.obtener_o_crear_usuario("acme", "telegram", "555", "Ana")
        await st.guardar_mensaje("acme", uid, "telegram", "user", "hola")
        await st.guardar_mensaje("acme", uid, "telegram", "assistant", "buenas",
                                 tool_calls_json='[{"tool": "knowledge_search"}]')
        async with st.async_session() as s:
            s.add(st.Lead(tenant_id="acme", usuario_id=uid, nombre="Ana",
                          interes="planes", calificacion="caliente"))
            await s.commit()
        return uid

    async def test_conversaciones(self, cliente):
        uid = await self._sembrar(cliente)
        r = await cliente.get("/admin/tenants/acme/conversations", headers=HEADERS)
        assert r.status_code == 200
        convs = r.json()["conversaciones"]
        assert len(convs) == 1
        assert convs[0]["usuario_id"] == uid
        assert convs[0]["mensajes"] == 2

    async def test_ver_conversacion(self, cliente):
        uid = await self._sembrar(cliente)
        r = await cliente.get(f"/admin/tenants/acme/conversations/{uid}", headers=HEADERS)
        assert r.status_code == 200
        roles = [m["role"] for m in r.json()["mensajes"]]
        assert roles == ["user", "assistant"]

    async def test_leads(self, cliente):
        await self._sembrar(cliente)
        r = await cliente.get("/admin/tenants/acme/leads", headers=HEADERS)
        assert r.status_code == 200
        leads = r.json()["leads"]
        assert len(leads) == 1
        assert leads[0]["calificacion"] == "caliente"

    async def test_metrics(self, cliente):
        await self._sembrar(cliente)
        r = await cliente.get("/admin/tenants/acme/metrics", headers=HEADERS)
        assert r.status_code == 200
        data = r.json()
        assert data["total_mensajes"] == 2
        assert data["usuarios_unicos"] == 1
        assert data["total_leads"] == 1
        assert data["tools_usadas"].get("knowledge_search") == 1

    async def test_knowledge_status(self, cliente):
        r = await cliente.get("/admin/tenants/acme/knowledge/status", headers=HEADERS)
        assert r.status_code == 200
        assert "chunks" in r.json()
