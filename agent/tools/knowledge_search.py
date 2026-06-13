# agent/tools/knowledge_search.py — Tool: buscar en la base de conocimiento (RAG)

from agent.tools.base import ToolDefinition, ContextoEjecucion
from agent.memory.knowledge import buscar_en_knowledge


async def _ejecutar(contexto: ContextoEjecucion, consulta: str) -> str:
    resultado = await buscar_en_knowledge(contexto.tenant_id, consulta)
    return resultado or "No encontré información sobre eso en la base de conocimiento."


TOOL = ToolDefinition(
    nombre="knowledge_search",
    descripcion=(
        "Busca información en la base de conocimiento del negocio (catálogo, precios, "
        "FAQ, políticas, servicios). Usala cuando el cliente pregunte algo específico "
        "del negocio que no sabés con certeza."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "consulta": {
                "type": "string",
                "description": "La pregunta o tema a buscar en la base de conocimiento.",
            }
        },
        "required": ["consulta"],
    },
    funcion=_ejecutar,
)
