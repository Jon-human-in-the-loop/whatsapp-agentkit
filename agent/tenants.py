# agent/tenants.py — Gestión multi-tenant
#
# Carga y cachea la configuración de cada tenant desde config/tenants/{id}/.
# Cada tenant tiene: business.yaml, prompts.yaml y (opcional) tenant.yaml.
# config/settings.yaml define los valores por defecto del sistema.

import os
import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger("agentkit")

TENANTS_DIR = Path(os.getenv("TENANTS_DIR", "config/tenants"))
SETTINGS_PATH = Path(os.getenv("SETTINGS_PATH", "config/settings.yaml"))
DEFAULT_TENANT_ID = os.getenv("DEFAULT_TENANT_ID", "demo")

# Defaults de último recurso si no hay config/settings.yaml
_DEFAULTS_FALLBACK = {
    "canales": ["whatsapp_twilio"],
    "tools": ["knowledge_search", "registrar_lead", "escalar_a_humano"],
    "limites": {
        "rate_limit_mensajes": 10,
        "rate_limit_ventana_segundos": 60,
        "max_tokens_dia": 100000,
    },
}


def _leer_yaml(ruta: Path) -> dict:
    """Lee un YAML y devuelve dict ({} si no existe o está vacío)."""
    try:
        with open(ruta, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}
    except yaml.YAMLError as e:
        logger.error(f"YAML inválido en {ruta}: {e}")
        return {}


@dataclass
class TenantConfig:
    """Configuración resuelta de un tenant (business + prompts + meta)."""
    tenant_id: str
    nombre: str
    config_path: str
    business_info: dict = field(default_factory=dict)
    prompts: dict = field(default_factory=dict)
    canales: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    limites: dict = field(default_factory=dict)

    @property
    def system_prompt(self) -> str:
        return self.prompts.get(
            "system_prompt", "Eres un asistente útil. Responde en español."
        )

    @property
    def error_message(self) -> str:
        return self.prompts.get(
            "error_message",
            "Lo siento, estoy teniendo problemas técnicos. Por favor intentá de nuevo en unos minutos.",
        )

    @property
    def fallback_message(self) -> str:
        return self.prompts.get(
            "fallback_message", "Disculpá, no entendí tu mensaje. ¿Podés contarme un poco más?"
        )


class GestorTenants:
    """Carga, cachea y expone la configuración de cada tenant."""

    def __init__(self, base_dir: Path = TENANTS_DIR, settings_path: Path = SETTINGS_PATH):
        self.base_dir = Path(base_dir)
        self.settings_path = Path(settings_path)
        self._cache: dict[str, TenantConfig] = {}

    def _defaults(self) -> dict:
        """Defaults globales desde settings.yaml, con fallback embebido."""
        settings = _leer_yaml(self.settings_path)
        return settings.get("defaults", _DEFAULTS_FALLBACK)

    def listar_tenants(self) -> list[str]:
        """Lista los tenant_id disponibles (subdirectorios de config/tenants/)."""
        if not self.base_dir.exists():
            return []
        return sorted(p.name for p in self.base_dir.iterdir() if p.is_dir())

    def existe(self, tenant_id: str) -> bool:
        return (self.base_dir / tenant_id).is_dir()

    def cargar_config(self, tenant_id: str) -> dict:
        """Lee los YAML crudos de un tenant: business, prompts y tenant (meta)."""
        carpeta = self.base_dir / tenant_id
        return {
            "business": _leer_yaml(carpeta / "business.yaml"),
            "prompts": _leer_yaml(carpeta / "prompts.yaml"),
            "tenant": _leer_yaml(carpeta / "tenant.yaml"),
        }

    def obtener_tenant(self, tenant_id: str) -> TenantConfig:
        """Devuelve la TenantConfig resuelta (cacheada) de un tenant."""
        if tenant_id in self._cache:
            return self._cache[tenant_id]

        raw = self.cargar_config(tenant_id)
        defaults = self._defaults()
        meta = raw["tenant"]
        business = raw["business"]

        nombre = (
            meta.get("nombre")
            or business.get("negocio", {}).get("nombre")
            or tenant_id
        )
        config = TenantConfig(
            tenant_id=tenant_id,
            nombre=nombre,
            config_path=str(self.base_dir / tenant_id),
            business_info=business,
            prompts=raw["prompts"],
            canales=meta.get("canales", defaults.get("canales", [])),
            tools=meta.get("tools", defaults.get("tools", [])),
            limites=meta.get("limites", defaults.get("limites", {})),
        )
        self._cache[tenant_id] = config
        return config

    def recargar(self, tenant_id: str | None = None) -> None:
        """Invalida el cache (de un tenant o de todos)."""
        if tenant_id is None:
            self._cache.clear()
        else:
            self._cache.pop(tenant_id, None)


# Instancia global compartida
gestor_tenants = GestorTenants()
