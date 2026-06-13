# agent/tools/base.py — Definición de herramientas compatibles con Anthropic tool use
#
# Cada tool se describe con un input_schema (JSON Schema) en el formato que
# espera la API de Anthropic. La función recibe un ContextoEjecucion (tenant,
# usuario) más los argumentos que el modelo decidió, y devuelve un string.

from dataclasses import dataclass
from typing import Awaitable, Callable


@dataclass
class ContextoEjecucion:
    """Contexto que se inyecta a cada tool al ejecutarla (no lo provee el LLM)."""
    tenant_id: str
    usuario_id: int | None = None


@dataclass
class ToolDefinition:
    """Una herramienta que el agente puede invocar."""
    nombre: str
    descripcion: str
    input_schema: dict
    funcion: Callable[..., Awaitable[str]]  # async (contexto, **input) -> str

    def to_anthropic(self) -> dict:
        """Formato de tool que espera la API de Anthropic."""
        return {
            "name": self.nombre,
            "description": self.descripcion,
            "input_schema": self.input_schema,
        }
