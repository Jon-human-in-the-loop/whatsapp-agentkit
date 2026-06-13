# admin/api.py — Endpoints REST de administración
#
# Autenticados con el header X-Admin-Key == ADMIN_API_KEY.
# Se deshabilitan si ADMIN_ENABLED != "true".

import os
import logging
from pathlib import Path

import yaml
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func

from agent.tenants import gestor_tenants
from agent.memory.short_term import async_session, Mensaje, Usuario, Lead, Tenant
from agent.memory.knowledge import indexar_knowledge_base, estado_indice
from admin.metrics import metricas_tenant

logger = logging.getLogger("agentkit")

router = APIRouter(prefix="/admin", tags=["admin"])


# ─── Autenticación ───────────────────────────────────────────────────────────

def verificar_admin(x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    """Valida la API key de administración."""
    if os.getenv("ADMIN_ENABLED", "true").lower() != "true":
        raise HTTPException(status_code=404, detail="Admin API deshabilitada")
    clave = os.getenv("ADMIN_API_KEY")
    if not clave:
        raise HTTPException(status_code=503, detail="ADMIN_API_KEY no configurada")
    if x_admin_key != clave:
        raise HTTPException(status_code=401, detail="No autorizado")


def _verificar_tenant(tenant_id: str):
    if not gestor_tenants.existe(tenant_id):
        raise HTTPException(status_code=404, detail=f"Tenant '{tenant_id}' no existe")


# ─── Modelos de request ──────────────────────────────────────────────────────

class CrearTenant(BaseModel):
    tenant_id: str
    nombre: str
    system_prompt: str
    descripcion: str = ""
    canales: list[str] = ["whatsapp_twilio"]
    tools: list[str] = ["knowledge_search", "registrar_lead", "escalar_a_humano"]


class ActualizarTenant(BaseModel):
    nombre: str | None = None
    canales: list[str] | None = None
    tools: list[str] | None = None
    system_prompt: str | None = None


# ─── Tenants ─────────────────────────────────────────────────────────────────

@router.get("/tenants", dependencies=[Depends(verificar_admin)])
async def listar_tenants():
    resultado = []
    for tid in gestor_tenants.listar_tenants():
        cfg = gestor_tenants.obtener_tenant(tid)
        resultado.append({
            "tenant_id": tid, "nombre": cfg.nombre,
            "canales": cfg.canales, "tools": cfg.tools,
        })
    return {"tenants": resultado}


@router.post("/tenants", dependencies=[Depends(verificar_admin)], status_code=201)
async def crear_tenant(body: CrearTenant):
    carpeta = Path(gestor_tenants.base_dir) / body.tenant_id
    if carpeta.exists():
        raise HTTPException(status_code=409, detail="El tenant ya existe")
    carpeta.mkdir(parents=True)

    (carpeta / "business.yaml").write_text(
        yaml.safe_dump(
            {"negocio": {"nombre": body.nombre, "descripcion": body.descripcion}},
            allow_unicode=True, sort_keys=False,
        ), encoding="utf-8",
    )
    (carpeta / "prompts.yaml").write_text(
        yaml.safe_dump({"system_prompt": body.system_prompt}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    (carpeta / "tenant.yaml").write_text(
        yaml.safe_dump(
            {"nombre": body.nombre, "canales": body.canales, "tools": body.tools},
            allow_unicode=True, sort_keys=False,
        ), encoding="utf-8",
    )

    gestor_tenants.recargar(body.tenant_id)
    async with async_session() as session:
        if not await session.get(Tenant, body.tenant_id):
            session.add(Tenant(id=body.tenant_id, nombre=body.nombre, config_path=str(carpeta)))
            await session.commit()
    logger.info(f"Tenant creado vía admin: {body.tenant_id}")
    return {"status": "creado", "tenant_id": body.tenant_id}


@router.put("/tenants/{tenant_id}", dependencies=[Depends(verificar_admin)])
async def actualizar_tenant(tenant_id: str, body: ActualizarTenant):
    _verificar_tenant(tenant_id)
    carpeta = Path(gestor_tenants.base_dir) / tenant_id

    # Actualizar metadata (tenant.yaml) con los campos provistos
    ruta_meta = carpeta / "tenant.yaml"
    meta = {}
    if ruta_meta.exists():
        meta = yaml.safe_load(ruta_meta.read_text(encoding="utf-8")) or {}
    for campo in ("nombre", "canales", "tools"):
        valor = getattr(body, campo)
        if valor is not None:
            meta[campo] = valor
    ruta_meta.write_text(yaml.safe_dump(meta, allow_unicode=True, sort_keys=False), encoding="utf-8")

    # Actualizar system_prompt (prompts.yaml) solo si se envió explícitamente
    if body.system_prompt is not None:
        ruta_prompts = carpeta / "prompts.yaml"
        prompts = {}
        if ruta_prompts.exists():
            prompts = yaml.safe_load(ruta_prompts.read_text(encoding="utf-8")) or {}
        prompts["system_prompt"] = body.system_prompt
        ruta_prompts.write_text(
            yaml.safe_dump(prompts, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )

    gestor_tenants.recargar(tenant_id)
    return {"status": "actualizado", "tenant_id": tenant_id}


# ─── Conversaciones ──────────────────────────────────────────────────────────

@router.get("/tenants/{tenant_id}/conversations", dependencies=[Depends(verificar_admin)])
async def listar_conversaciones(tenant_id: str, canal: str | None = None,
                                limit: int = Query(50, ge=1, le=500)):
    _verificar_tenant(tenant_id)
    async with async_session() as session:
        query = select(Usuario).where(Usuario.tenant_id == tenant_id)
        if canal:
            query = query.where(Usuario.canal == canal)
        query = query.order_by(Usuario.ultima_interaccion.desc()).limit(limit)
        usuarios = list((await session.execute(query)).scalars().all())

        conversaciones = []
        for u in usuarios:
            n = int((await session.execute(
                select(func.count(Mensaje.id)).where(
                    Mensaje.tenant_id == tenant_id, Mensaje.usuario_id == u.id
                )
            )).scalar() or 0)
            conversaciones.append({
                "usuario_id": u.id, "canal": u.canal, "identificador": u.identificador,
                "nombre": u.nombre, "mensajes": n,
                "ultima_interaccion": u.ultima_interaccion.isoformat(),
            })
    return {"conversaciones": conversaciones}


@router.get("/tenants/{tenant_id}/conversations/{usuario_id}",
            dependencies=[Depends(verificar_admin)])
async def ver_conversacion(tenant_id: str, usuario_id: int,
                           limit: int = Query(100, ge=1, le=1000)):
    _verificar_tenant(tenant_id)
    async with async_session() as session:
        query = (
            select(Mensaje)
            .where(Mensaje.tenant_id == tenant_id, Mensaje.usuario_id == usuario_id)
            .order_by(Mensaje.timestamp.asc(), Mensaje.id.asc())
            .limit(limit)
        )
        mensajes = list((await session.execute(query)).scalars().all())
    return {
        "usuario_id": usuario_id,
        "mensajes": [
            {"role": m.role, "content": m.content, "canal": m.canal,
             "timestamp": m.timestamp.isoformat(),
             "tool_calls": m.tool_calls_json}
            for m in mensajes
        ],
    }


# ─── Leads ───────────────────────────────────────────────────────────────────

@router.get("/tenants/{tenant_id}/leads", dependencies=[Depends(verificar_admin)])
async def listar_leads(tenant_id: str, estado: str | None = None,
                       limit: int = Query(100, ge=1, le=1000)):
    _verificar_tenant(tenant_id)
    async with async_session() as session:
        query = select(Lead).where(Lead.tenant_id == tenant_id)
        if estado:
            query = query.where(Lead.estado == estado)
        query = query.order_by(Lead.creado.desc()).limit(limit)
        leads = list((await session.execute(query)).scalars().all())
    return {
        "leads": [
            {"id": l.id, "usuario_id": l.usuario_id, "nombre": l.nombre,
             "email": l.email, "telefono": l.telefono, "interes": l.interes,
             "calificacion": l.calificacion, "estado": l.estado,
             "creado": l.creado.isoformat()}
            for l in leads
        ]
    }


# ─── Métricas ────────────────────────────────────────────────────────────────

@router.get("/tenants/{tenant_id}/metrics", dependencies=[Depends(verificar_admin)])
async def metricas(tenant_id: str):
    _verificar_tenant(tenant_id)
    return await metricas_tenant(tenant_id)


# ─── Knowledge base ──────────────────────────────────────────────────────────

@router.post("/tenants/{tenant_id}/knowledge/reindex", dependencies=[Depends(verificar_admin)])
async def reindexar_knowledge(tenant_id: str):
    _verificar_tenant(tenant_id)
    chunks = await indexar_knowledge_base(tenant_id)
    return {"status": "reindexado", "tenant_id": tenant_id, "chunks": chunks}


@router.get("/tenants/{tenant_id}/knowledge/status", dependencies=[Depends(verificar_admin)])
async def estado_knowledge(tenant_id: str):
    _verificar_tenant(tenant_id)
    return estado_indice(tenant_id)
