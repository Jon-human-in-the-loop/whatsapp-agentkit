# tests/test_local.py — Simulador de chat en terminal (multi-canal / multi-tenant)
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.brain import generar_respuesta
from agent.memory import (
    inicializar_db,
    obtener_o_crear_usuario,
    guardar_mensaje,
    obtener_historial,
    limpiar_historial,
)
from agent.tenants import gestor_tenants, DEFAULT_TENANT_ID

# Simulamos un usuario que escribe por un canal concreto a un tenant concreto
CANAL_TEST = "whatsapp_twilio"
IDENTIFICADOR_TEST = "test-local-001"


async def main():
    await inicializar_db()

    tenant_id = os.getenv("DEFAULT_TENANT_ID", DEFAULT_TENANT_ID)
    cfg = gestor_tenants.obtener_tenant(tenant_id)
    agente = cfg.prompts.get("nombre") or "Agente"

    usuario_pk = await obtener_o_crear_usuario(
        tenant_id, CANAL_TEST, IDENTIFICADOR_TEST, "Tester"
    )

    print()
    print("=" * 55)
    print(f"   Test Local — tenant: {tenant_id} ({cfg.nombre})")
    print("=" * 55)
    print()
    print("  Escribí mensajes como si fueras un cliente.")
    print("  Comandos especiales:")
    print("    'limpiar'  — borra el historial")
    print("    'salir'    — termina el test")
    print()
    print("-" * 55)
    print()

    while True:
        try:
            mensaje = input("Vos: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\nTest finalizado.")
            break

        if not mensaje:
            continue

        if mensaje.lower() == "salir":
            print("\nTest finalizado.")
            break

        if mensaje.lower() == "limpiar":
            await limpiar_historial(tenant_id, usuario_pk)
            print("[Historial borrado]\n")
            continue

        historial = await obtener_historial(tenant_id, usuario_pk)

        print("\nAgente: ", end="", flush=True)
        respuesta = await generar_respuesta(mensaje, historial, tenant_id)
        print(respuesta)
        print()

        await guardar_mensaje(tenant_id, usuario_pk, CANAL_TEST, "user", mensaje)
        await guardar_mensaje(tenant_id, usuario_pk, CANAL_TEST, "assistant", respuesta)


if __name__ == "__main__":
    asyncio.run(main())
