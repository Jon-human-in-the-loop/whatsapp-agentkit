# tests/test_channels.py — Tests de normalización de canales a MensajeUnificado
#
# Cada canal traduce su payload nativo a MensajeUnificado. Estos tests validan
# esa normalización y la verificación de seguridad de cada webhook, sin red.

import os
import sys
import json
import hmac
import hashlib
import base64

import pytest
from starlette.requests import Request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.channels.base import TipoCanal


def make_request(body: bytes = b"", headers: dict | None = None,
                 method: str = "POST", query: str = "",
                 path: str = "/webhook") -> Request:
    """Fabrica un starlette.Request mínimo para testear parseo de webhooks."""
    headers = headers or {}
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "headers": raw_headers,
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 12345),
    }

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


# ─── Telegram ────────────────────────────────────────────────────────────────

class TestTelegram:
    def teardown_method(self):
        for var in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_WEBHOOK_SECRET"):
            os.environ.pop(var, None)

    def _canal(self):
        from agent.channels.telegram import CanalTelegram
        return CanalTelegram("demo")

    async def test_normaliza_mensaje(self):
        update = {
            "update_id": 1,
            "message": {
                "message_id": 42,
                "from": {"id": 7, "first_name": "Ana", "is_bot": False},
                "chat": {"id": 12345},
                "text": "Hola, quiero info",
            },
        }
        msgs = await self._canal().parsear_webhook(make_request(json.dumps(update).encode()))
        assert len(msgs) == 1
        m = msgs[0]
        assert m.canal == TipoCanal.TELEGRAM
        assert m.tenant_id == "demo"
        assert m.usuario_id == "12345"
        assert m.usuario_nombre == "Ana"
        assert m.texto == "Hola, quiero info"
        assert m.mensaje_id == "12345:42"
        assert m.es_propio is False

    async def test_ignora_update_sin_texto(self):
        update = {"update_id": 2, "message": {"message_id": 1, "chat": {"id": 9}}}
        msgs = await self._canal().parsear_webhook(make_request(json.dumps(update).encode()))
        assert msgs == []

    async def test_marca_es_propio_si_es_bot(self):
        update = {
            "message": {
                "message_id": 5, "chat": {"id": 1},
                "from": {"id": 2, "first_name": "Bot", "is_bot": True},
                "text": "respuesta",
            }
        }
        msgs = await self._canal().parsear_webhook(make_request(json.dumps(update).encode()))
        assert msgs[0].es_propio is True

    async def test_secret_invalido_rechaza(self):
        os.environ["TELEGRAM_WEBHOOK_SECRET"] = "secreto-correcto-123456"
        from fastapi import HTTPException
        canal = self._canal()
        req = make_request(b"{}", headers={"X-Telegram-Bot-Api-Secret-Token": "mal"})
        with pytest.raises(HTTPException) as exc:
            await canal.parsear_webhook(req)
        assert exc.value.status_code == 403

    async def test_secret_valido_acepta(self):
        os.environ["TELEGRAM_WEBHOOK_SECRET"] = "secreto-correcto-123456"
        canal = self._canal()
        update = {"message": {"message_id": 1, "chat": {"id": 3},
                              "from": {"first_name": "X"}, "text": "hola"}}
        req = make_request(
            json.dumps(update).encode(),
            headers={"X-Telegram-Bot-Api-Secret-Token": "secreto-correcto-123456"},
        )
        msgs = await canal.parsear_webhook(req)
        assert len(msgs) == 1


# ─── Discord ─────────────────────────────────────────────────────────────────

class TestDiscord:
    def setup_method(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives import serialization
        self.sk = Ed25519PrivateKey.generate()
        pub = self.sk.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        os.environ["DISCORD_PUBLIC_KEY"] = pub.hex()

    def teardown_method(self):
        os.environ.pop("DISCORD_PUBLIC_KEY", None)

    def _canal(self):
        from agent.channels.discord import CanalDiscord
        return CanalDiscord("demo")

    def _firmar(self, body: bytes, timestamp: str = "1700000000") -> dict:
        firma = self.sk.sign(timestamp.encode() + body).hex()
        return {"X-Signature-Ed25519": firma, "X-Signature-Timestamp": timestamp}

    async def test_ping_responde_pong(self):
        body = json.dumps({"type": 1}).encode()
        req = make_request(body, headers=self._firmar(body), method="POST")
        resultado = await self._canal().validar_webhook(req)
        assert resultado == {"type": 1}

    async def test_firma_invalida_rechaza(self):
        from fastapi import HTTPException
        body = json.dumps({"type": 1}).encode()
        headers = {"X-Signature-Ed25519": "00" * 64, "X-Signature-Timestamp": "123"}
        with pytest.raises(HTTPException) as exc:
            await self._canal().validar_webhook(make_request(body, headers=headers))
        assert exc.value.status_code == 401

    async def test_normaliza_slash_command(self):
        interaction = {
            "id": "999",
            "type": 2,
            "channel_id": "555",
            "member": {"user": {"id": "77", "username": "ana", "global_name": "Ana"}},
            "data": {
                "name": "chat",
                "options": [{"name": "mensaje", "type": 3, "value": "Hola agente"}],
            },
        }
        body = json.dumps(interaction).encode()
        req = make_request(body, headers=self._firmar(body))
        msgs = await self._canal().parsear_webhook(req)
        assert len(msgs) == 1
        m = msgs[0]
        assert m.canal == TipoCanal.DISCORD
        assert m.usuario_id == "77"
        assert m.usuario_nombre == "Ana"
        assert m.texto == "Hola agente"
        assert m.mensaje_id == "999"
        assert m.metadata.get("channel_id") == "555"

    async def test_ignora_tipo_no_command(self):
        interaction = {"id": "1", "type": 5, "data": {}}  # MODAL_SUBMIT u otro
        body = json.dumps(interaction).encode()
        req = make_request(body, headers=self._firmar(body))
        msgs = await self._canal().parsear_webhook(req)
        assert msgs == []
