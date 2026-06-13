# tests/test_tools.py — Tests de las tools y del bucle agentivo
#
# Offline: EMBEDDING_MODEL=local y ChromaDB temporal; el cliente de Anthropic
# se reemplaza por un stub que simula respuestas con tool_use / end_turn.

import os
import sys
import json
import importlib
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["EMBEDDING_MODEL"] = "local"


@pytest.fixture(autouse=True)
def chroma_temporal(monkeypatch):
    d = tempfile.mkdtemp()
    monkeypatch.setenv("CHROMA_PERSIST_DIR", d)
    from agent.memory import knowledge
    knowledge._clientes.clear()
    yield d
    knowledge._clientes.clear()


# ─── Registro y esquema ──────────────────────────────────────────────────────

class TestRegistro:
    def test_obtener_tools_filtra_desconocidas(self):
        from agent.tools import obtener_tools
        tools = obtener_tools(["knowledge_search", "registrar_lead", "inexistente"])
        nombres = [t.nombre for t in tools]
        assert "knowledge_search" in nombres
        assert "registrar_lead" in nombres
        assert "inexistente" not in nombres

    def test_formato_anthropic(self):
        from agent.tools import obtener_tools
        tool = obtener_tools(["knowledge_search"])[0]
        schema = tool.to_anthropic()
        assert schema["name"] == "knowledge_search"
        assert "description" in schema
        assert schema["input_schema"]["type"] == "object"
        assert "consulta" in schema["input_schema"]["properties"]

    async def test_ejecutar_tool_desconocida(self):
        from agent.tools import ejecutar_tool, ContextoEjecucion
        out = await ejecutar_tool("no_existe", {}, ContextoEjecucion("demo", 1))
        assert "desconocida" in out.lower()


# ─── Tools con base de datos (CRM, escalación) ───────────────────────────────

class TestToolsDB:
    @pytest.fixture(autouse=True)
    def db(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/tools.db")
        from agent.memory import short_term
        importlib.reload(short_term)
        from agent.tools import crm, escalation
        importlib.reload(crm)
        importlib.reload(escalation)
        self.short_term = short_term
        self.crm = crm
        self.escalation = escalation
        yield

    async def test_registrar_y_consultar_lead(self):
        from agent.tools.base import ContextoEjecucion
        await self.short_term.inicializar_db()
        uid = await self.short_term.obtener_o_crear_usuario("demo", "whatsapp_twilio", "+5491100", "Ana")
        ctx = ContextoEjecucion("demo", uid)

        out = await self.crm._registrar_lead(
            ctx, nombre="Ana", email="ana@x.com", interes="Automatización IA",
            calificacion="caliente",
        )
        assert "registrado" in out.lower()

        consulta = await self.crm._consultar_leads(ctx)
        assert "Ana" in consulta
        assert "Automatización IA" in consulta

    async def test_calificacion_invalida_se_normaliza(self):
        from agent.tools.base import ContextoEjecucion
        await self.short_term.inicializar_db()
        uid = await self.short_term.obtener_o_crear_usuario("demo", "telegram", "55", None)
        ctx = ContextoEjecucion("demo", uid)
        await self.crm._registrar_lead(ctx, interes="algo", calificacion="invalida")
        # No debe romper; el lead queda con calificación por defecto
        async with self.short_term.async_session() as s:
            from sqlalchemy import select
            lead = (await s.execute(select(self.short_term.Lead))).scalars().first()
        assert lead.calificacion == "tibio"

    async def test_escalar_marca_usuario(self):
        from agent.tools.base import ContextoEjecucion
        await self.short_term.inicializar_db()
        uid = await self.short_term.obtener_o_crear_usuario("demo", "slack", "C1", None)
        ctx = ContextoEjecucion("demo", uid)
        out = await self.escalation._escalar(ctx, motivo="cliente muy molesto")
        assert "equipo" in out.lower()
        usuario = None
        async with self.short_term.async_session() as s:
            usuario = await s.get(self.short_term.Usuario, uid)
        meta = json.loads(usuario.metadata_json)
        assert meta["escalado"] is True
        assert meta["motivo_escalado"] == "cliente muy molesto"


# ─── Bucle agentivo (cliente Anthropic simulado) ─────────────────────────────

class _Bloque:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Resp:
    def __init__(self, stop_reason, content):
        self.stop_reason = stop_reason
        self.content = content


class _FakeMessages:
    def __init__(self, respuestas):
        self._respuestas = list(respuestas)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._respuestas.pop(0)


class _FakeClient:
    def __init__(self, respuestas):
        self.messages = _FakeMessages(respuestas)


class TestBucleAgentivo:
    async def test_respuesta_directa_sin_tool(self, monkeypatch):
        from agent import brain
        fake = _FakeClient([_Resp("end_turn", [_Bloque(type="text", text="¡Hola! ¿En qué ayudo?")])])
        monkeypatch.setattr(brain, "_cliente", fake)
        texto, log = await brain.ejecutar_agente("hola", [], "demo", usuario_id=1)
        assert texto == "¡Hola! ¿En qué ayudo?"
        assert log == []
        assert len(fake.messages.calls) == 1

    async def test_ejecuta_tool_y_luego_responde(self, monkeypatch):
        from agent import brain
        r1 = _Resp("tool_use", [
            _Bloque(type="tool_use", name="knowledge_search",
                    input={"consulta": "precios"}, id="tu_1"),
        ])
        r2 = _Resp("end_turn", [_Bloque(type="text", text="Los planes parten de 100 USD.")])
        fake = _FakeClient([r1, r2])
        monkeypatch.setattr(brain, "_cliente", fake)

        texto, log = await brain.ejecutar_agente(
            "¿cuánto cuestan?", [], "demo", usuario_id=1
        )
        assert texto == "Los planes parten de 100 USD."
        assert len(log) == 1
        assert log[0]["tool"] == "knowledge_search"
        # Dos llamadas al modelo: una pidió la tool, la otra cerró el turno
        assert len(fake.messages.calls) == 2
        # La segunda llamada debe incluir el tool_result en los mensajes
        ultimos_mensajes = fake.messages.calls[1]["messages"]
        assert any(
            isinstance(m["content"], list) and
            any(isinstance(b, dict) and b.get("type") == "tool_result" for b in m["content"])
            for m in ultimos_mensajes
        )

    async def test_serializar_tool_calls(self):
        from agent.brain import serializar_tool_calls
        assert serializar_tool_calls([]) is None
        out = serializar_tool_calls([{"tool": "x", "input": {}, "output": "ok"}])
        assert json.loads(out)[0]["tool"] == "x"
