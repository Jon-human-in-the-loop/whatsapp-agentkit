# admin/metrics.py — Métricas por tenant para el panel de administración

import json
import logging
from collections import Counter
from datetime import datetime, timedelta

from sqlalchemy import select, func, distinct

from agent.memory.short_term import async_session, Mensaje, Usuario, Lead

logger = logging.getLogger("agentkit")


async def metricas_tenant(tenant_id: str) -> dict:
    """Calcula métricas agregadas de un tenant: mensajes, usuarios, leads, tools."""
    desde_24h = datetime.utcnow() - timedelta(days=1)
    async with async_session() as session:
        total_mensajes = int((await session.execute(
            select(func.count(Mensaje.id)).where(Mensaje.tenant_id == tenant_id)
        )).scalar() or 0)

        mensajes_24h = int((await session.execute(
            select(func.count(Mensaje.id)).where(
                Mensaje.tenant_id == tenant_id, Mensaje.timestamp >= desde_24h
            )
        )).scalar() or 0)

        usuarios_unicos = int((await session.execute(
            select(func.count(distinct(Usuario.id))).where(Usuario.tenant_id == tenant_id)
        )).scalar() or 0)

        total_leads = int((await session.execute(
            select(func.count(Lead.id)).where(Lead.tenant_id == tenant_id)
        )).scalar() or 0)

        # Tools usadas: agregamos desde tool_calls_json de los mensajes del agente
        filas = (await session.execute(
            select(Mensaje.tool_calls_json).where(
                Mensaje.tenant_id == tenant_id, Mensaje.tool_calls_json.isnot(None)
            )
        )).scalars().all()

    tools_usadas: Counter = Counter()
    for cruda in filas:
        try:
            for tc in json.loads(cruda):
                nombre = tc.get("tool")
                if nombre:
                    tools_usadas[nombre] += 1
        except (json.JSONDecodeError, TypeError, AttributeError):
            continue

    return {
        "tenant_id": tenant_id,
        "total_mensajes": total_mensajes,
        "mensajes_ultimas_24h": mensajes_24h,
        "usuarios_unicos": usuarios_unicos,
        "total_leads": total_leads,
        "tools_usadas": dict(tools_usadas),
    }
