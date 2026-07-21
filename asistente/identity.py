"""Identidad: resolver QUIÉN escribe por su teléfono verificado de WhatsApp.

El directorio (teléfono → consorcio + usuario) se autollena jalando el roster de cada back.
Número desconocido = no se le sirve nada (seguridad: el asistente ve a todos).
"""
import re
from dataclasses import dataclass

import httpx
import psycopg

from . import auth
from .config import config
from .registry import Consorcio, consorcio_por_id, listar_consorcios


@dataclass
class Identidad:
    telefono: str
    consorcio_id: str
    empleado_id: str | None
    nombre: str | None
    rol: str | None
    es_global: bool          # admin/contable ven el consorcio; el resto queda acotado
    banca_ids: list[str]     # bancas que puede ver (solo si NO es global)


def _normalizar(telefono: str) -> str:
    """WhatsApp da el wa_id como '18095551234' o '18095551234@s.whatsapp.net'. Dejamos solo dígitos."""
    return re.sub(r"\D", "", telefono.split("@")[0])


def _resolver_en_back(consorcio: Consorcio, telefono: str) -> dict | None:
    """Pregunta al back del consorcio '¿quién es este teléfono?' (endpoint de gobernanza).
    Devuelve {empleado_id, nombre, rol, es_global, banca_ids} o None si no está autorizado."""
    url = consorcio.backend_url + "/api/crm/asistente/resolver"
    headers = {"Authorization": f"Bearer {auth.token_de(consorcio)}"}
    with httpx.Client(timeout=15) as client:
        resp = client.get(url, headers=headers, params={"telefono": telefono})
        resp.raise_for_status()
        return (resp.json() or {}).get("contacto")


class BackInalcanzable(Exception):
    """Ningún back respondió al resolver la identidad (todos caídos/timeout). Se distingue de
    'desconocido' (un back SÍ respondió pero no reconoció el teléfono) para que el bot pueda decir
    'no puedo verificar ahora' en vez de 'no te reconozco' (que suena a desautorización)."""


def resolver(telefono: str) -> Identidad | None:
    """Resuelve el teléfono contra el BACK (tabla asistente_contactos, configurable desde el CRM).
    Recorre los consorcios registrados; el que lo reconoce define su consorcio, rol y ALCANCE.
    Devuelve None = desconocido/desactivado (un back respondió pero no lo reconoce). Lanza
    BackInalcanzable si NINGÚN back respondió (para distinguir outage de desconocido aguas abajo).

    FAIL-CLOSED (a propósito): ante un error del resolver (back caído, endpoint 4xx/5xx, timeout) NO
    hay fallback que conceda acceso. Antes se caía al directorio local concediendo es_global=True a
    todos — un hueco: un hipo del back = acceso global para cualquiera. Un back caído deja al bot sin
    servir (BackInalcanzable), nunca permisivo."""
    tel = _normalizar(telefono)
    algun_back_respondio = False
    for c in listar_consorcios():
        full = consorcio_por_id(c.id)
        if not full:
            continue
        try:
            r = _resolver_en_back(full, tel)
            algun_back_respondio = True
        except Exception as ex:  # noqa: BLE001 — back caído o endpoint no desplegado → probar el resto
            print(f"[identidad] resolver en {full.nombre} falló: {ex}")
            continue
        if r:
            return Identidad(
                telefono=tel,
                consorcio_id=full.id,
                empleado_id=r.get("empleado_id"),
                nombre=r.get("nombre"),
                rol=r.get("rol"),
                es_global=bool(r.get("es_global")),
                banca_ids=r.get("banca_ids") or [],
            )
    # Ningún back reconoció el teléfono. Si además NINGUNO respondió, es un outage (no un desconocido):
    # se señala para que el mensaje sea distinto. Sigue fail-closed (no se sirve nada en ambos casos).
    if not algun_back_respondio:
        raise BackInalcanzable(tel)
    return None


def consorcio_de(identidad: Identidad) -> Consorcio | None:
    return consorcio_por_id(identidad.consorcio_id)


# ── Auto-sync del roster ──────────────────────────────────────────────────────
# Jala los empleados-con-teléfono de cada back y actualiza el directorio. Corre periódicamente
# (un scheduler simple, o al arrancar) para que gente nueva de un consorcio quede reconocida.
#
# AUTO-SYNC (camino futuro): GET /api/empleados devuelve { id, telefono, nombre_real, rol } por empleado
# (el teléfono puede venir "(809)..." → _normalizar lo deja en dígitos). PERO ese endpoint exige el
# permiso EMPLEADOS_VER, que el usuario de servicio del bot NO tiene por defecto (para no sobre-
# permisionarlo). Para prender el auto-sync, agregar EMPLEADOS_VER a los permisos del bot.
# En Fase 0 (pocos admins) usar el alta MANUAL: `python -m asistente.agregar_persona ...` (más simple,
# sin depender de ese permiso). Por eso sincronizar_roster es best-effort y su 403 no es fatal.
ROSTER_PATH = "/api/empleados"


def sincronizar_roster(consorcio: Consorcio) -> int:
    """Upsert del roster de un consorcio en el directorio. Devuelve cuántos teléfonos cargó."""
    url = consorcio.backend_url + ROSTER_PATH
    headers = {"Authorization": f"Bearer {auth.token_de(consorcio)}"}
    with httpx.Client(timeout=30) as client:
        resp = client.get(url, headers=headers, params={"limit": 1000})
        resp.raise_for_status()
        data = resp.json()
    empleados = data.get("data", data) if isinstance(data, dict) else data

    cargados = 0
    with psycopg.connect(config.DATABASE_URL) as conn:
        for e in empleados:
            tel = _normalizar(str(e.get("telefono") or ""))
            if len(tel) < 10:  # sin teléfono usable, no entra al directorio
                continue
            conn.execute(
                """
                INSERT INTO directorio (telefono, consorcio_id, usuario_id, empleado_id, nombre, rol, sincronizado_en)
                VALUES (%s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (telefono) DO UPDATE SET
                    consorcio_id = EXCLUDED.consorcio_id,
                    usuario_id   = EXCLUDED.usuario_id,
                    empleado_id  = EXCLUDED.empleado_id,
                    nombre       = EXCLUDED.nombre,
                    rol          = EXCLUDED.rol,
                    sincronizado_en = now()
                """,
                (tel, consorcio.id, e.get("usuario_id"), e.get("id"),
                 e.get("nombre_real") or e.get("nombre"), e.get("rol")),
            )
            cargados += 1
        conn.commit()
    return cargados


def sincronizar_todos() -> dict[str, int]:
    """Re-sincroniza el roster de todos los consorcios registrados."""
    out: dict[str, int] = {}
    for c in listar_consorcios():
        full = consorcio_por_id(c.id)  # con token descifrado
        if full:
            try:
                out[full.nombre] = sincronizar_roster(full)
            except Exception as ex:  # noqa: BLE001 — un back caído no debe tumbar el sync de los demás
                out[full.nombre] = -1
                print(f"[roster] fallo sincronizando {full.nombre}: {ex}")
    return out
