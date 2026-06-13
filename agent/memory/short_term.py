# agent/memory/short_term.py — Memoria de corto plazo (historial relacional)
# Esquema multi-tenant con SQLAlchemy async (SQLite local / PostgreSQL prod).

"""
Define el modelo de datos relacional de la plataforma y las operaciones de
historial reciente por (tenant, usuario). Las otras capas de memoria
(long_term.py = vectores, knowledge.py = RAG) viven en módulos aparte.
"""

import os
import json
from datetime import datetime

from sqlalchemy import (
    String, Text, DateTime, Integer, Boolean, ForeignKey,
    UniqueConstraint, select,
)
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./agentkit.db")
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def _ahora() -> datetime:
    return datetime.utcnow()


class Base(DeclarativeBase):
    pass


# ─── Modelos ──────────────────────────────────────────────────────────────

class Tenant(Base):
    """Una empresa/cliente de la plataforma. Cada tenant tiene su config y datos."""
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # slug, ej: "demo"
    nombre: Mapped[str] = mapped_column(String(200), default="")
    activo: Mapped[bool] = mapped_column(Boolean, default=True)
    config_path: Mapped[str] = mapped_column(String(300), default="")
    creado: Mapped[datetime] = mapped_column(DateTime, default=_ahora)


class Usuario(Base):
    """Un usuario final, único por (tenant, canal, identificador nativo)."""
    __tablename__ = "usuarios"
    __table_args__ = (
        UniqueConstraint("tenant_id", "canal", "identificador", name="uq_usuario_canal"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), index=True)
    canal: Mapped[str] = mapped_column(String(40))           # "whatsapp_twilio", "telegram", ...
    identificador: Mapped[str] = mapped_column(String(120), index=True)  # teléfono, chat_id, etc.
    nombre: Mapped[str | None] = mapped_column(String(200), nullable=True)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    primera_vez: Mapped[datetime] = mapped_column(DateTime, default=_ahora)
    ultima_interaccion: Mapped[datetime] = mapped_column(DateTime, default=_ahora)


class Mensaje(Base):
    """Mensaje individual de una conversación. role: user | assistant | tool."""
    __tablename__ = "mensajes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    usuario_id: Mapped[int] = mapped_column(Integer, ForeignKey("usuarios.id"), index=True)
    canal: Mapped[str] = mapped_column(String(40))
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    tool_calls_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=_ahora, index=True)


class ResumenConversacion(Base):
    """Resumen vectorizado de un tramo de conversación (memoria de largo plazo)."""
    __tablename__ = "resumenes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    usuario_id: Mapped[int] = mapped_column(Integer, index=True)
    resumen: Mapped[str] = mapped_column(Text)
    vector_id: Mapped[str] = mapped_column(String(120), default="")
    mensajes_cubiertos: Mapped[int] = mapped_column(Integer, default=0)
    creado: Mapped[datetime] = mapped_column(DateTime, default=_ahora)


class Lead(Base):
    """Lead capturado durante una conversación."""
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    usuario_id: Mapped[int] = mapped_column(Integer, index=True)
    nombre: Mapped[str | None] = mapped_column(String(200), nullable=True)
    email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    telefono: Mapped[str | None] = mapped_column(String(50), nullable=True)
    interes: Mapped[str] = mapped_column(Text, default="")
    calificacion: Mapped[str] = mapped_column(String(20), default="frio")  # frio|tibio|caliente
    estado: Mapped[str] = mapped_column(String(20), default="nuevo")       # nuevo|contactado|convertido
    notas: Mapped[str] = mapped_column(Text, default="")
    creado: Mapped[datetime] = mapped_column(DateTime, default=_ahora)


# ─── Inicialización ─────────────────────────────────────────────────────────

async def inicializar_db() -> None:
    """Crea las tablas si no existen y siembra los tenants definidos en config/."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await sembrar_tenants()


async def sembrar_tenants() -> None:
    """Crea una fila Tenant por cada tenant configurado en config/tenants/."""
    from agent.tenants import gestor_tenants  # import diferido para evitar ciclo
    for tid in gestor_tenants.listar_tenants():
        async with async_session() as session:
            existente = await session.get(Tenant, tid)
            if existente is not None:
                continue
            cfg = gestor_tenants.obtener_tenant(tid)
            session.add(Tenant(id=tid, nombre=cfg.nombre, config_path=cfg.config_path))
            await session.commit()


# ─── Usuarios ────────────────────────────────────────────────────────────────

async def obtener_o_crear_usuario(tenant_id: str, canal: str, identificador: str,
                                  nombre: str | None = None) -> int:
    """Devuelve el id interno del usuario, creándolo si es la primera vez."""
    async with async_session() as session:
        query = select(Usuario).where(
            Usuario.tenant_id == tenant_id,
            Usuario.canal == canal,
            Usuario.identificador == identificador,
        )
        usuario = (await session.execute(query)).scalar_one_or_none()
        if usuario is None:
            usuario = Usuario(
                tenant_id=tenant_id, canal=canal,
                identificador=identificador, nombre=nombre,
            )
            session.add(usuario)
            await session.commit()
            await session.refresh(usuario)
        else:
            usuario.ultima_interaccion = _ahora()
            if nombre and not usuario.nombre:
                usuario.nombre = nombre
            await session.commit()
        return usuario.id


# ─── Historial ───────────────────────────────────────────────────────────────

async def guardar_mensaje(tenant_id: str, usuario_id: int, canal: str,
                          role: str, content: str,
                          tool_calls_json: str | None = None) -> None:
    """Guarda un mensaje en el historial de conversación."""
    async with async_session() as session:
        session.add(Mensaje(
            tenant_id=tenant_id, usuario_id=usuario_id, canal=canal,
            role=role, content=content, tool_calls_json=tool_calls_json,
        ))
        await session.commit()


async def obtener_historial(tenant_id: str, usuario_id: int, limite: int = 20) -> list[dict]:
    """
    Recupera los últimos N mensajes user/assistant de una conversación,
    en orden cronológico, listos para enviar al LLM.
    """
    async with async_session() as session:
        query = (
            select(Mensaje)
            .where(Mensaje.tenant_id == tenant_id, Mensaje.usuario_id == usuario_id)
            .order_by(Mensaje.timestamp.desc(), Mensaje.id.desc())
            .limit(limite)
        )
        mensajes = list((await session.execute(query)).scalars().all())
        mensajes.reverse()  # cronológico (estaban del más reciente al más viejo)
        return [
            {"role": m.role, "content": m.content}
            for m in mensajes
            if m.role in ("user", "assistant")
        ]


async def contar_mensajes(tenant_id: str, usuario_id: int) -> int:
    """Cuenta cuántos mensajes tiene un usuario (para disparar resúmenes)."""
    from sqlalchemy import func
    async with async_session() as session:
        query = select(func.count(Mensaje.id)).where(
            Mensaje.tenant_id == tenant_id, Mensaje.usuario_id == usuario_id
        )
        return int((await session.execute(query)).scalar() or 0)


async def limpiar_historial(tenant_id: str, usuario_id: int) -> None:
    """Borra todo el historial de una conversación."""
    async with async_session() as session:
        query = select(Mensaje).where(
            Mensaje.tenant_id == tenant_id, Mensaje.usuario_id == usuario_id
        )
        for mensaje in (await session.execute(query)).scalars().all():
            await session.delete(mensaje)
        await session.commit()
