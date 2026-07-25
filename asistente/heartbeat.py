"""Watchdog del bot: reporta al back cada pocos minutos si el puente de WhatsApp está CONECTADO.

El objetivo es AVISAR en el CRM cuando el bot se desconecta (device_removed, proceso caído, red) por
un canal que NO depende del WhatsApp caído. El back guarda el latido en Redis con TTL: si el bot deja
de latir, la clave expira sola y el CRM lo ve como desconectado. Barato y sin estado propio.
"""
from datetime import datetime, timedelta, timezone

import httpx

from .config import config
from .registry import listar_consorcios, consorcio_por_id
from . import auth

_sched = None


def _wa_conectado() -> bool:
    """¿El bot puede REALMENTE hablar?

    Antes miraba sólo `wa` (socket abierto). Con la cuenta restringida (error 463)
    el socket sigue conectado pero WhatsApp rechaza todo, así que el CRM pintaba
    el bot en verde estando mudo. Ahora exige además `entregando`: que no haya
    envíos recientes sin acuse. (auditoría 2026-07-25)
    """
    try:
        with httpx.Client(timeout=8) as c:
            s = c.get(f"{config.BRIDGE_URL}/salud").json()
        if not s.get("wa"):
            return False
        # `entregando` lo expone el puente nuevo; si falta (puente viejo), no
        # degradamos el estado: se cae al comportamiento anterior.
        return bool(s.get("entregando", True))
    except Exception:  # noqa: BLE001
        return False


def latir() -> None:
    wa = _wa_conectado()
    cons = listar_consorcios()
    if not cons:
        return
    # El estado del bot es GLOBAL; basta reportar con el token de un consorcio.
    full = consorcio_por_id(cons[0].id)
    if not full:
        return
    try:
        with httpx.Client(timeout=10) as c:
            c.post(
                f"{full.backend_url}/api/crm/bot-salud",
                headers={"Authorization": f"Bearer {auth.token_de(full)}"},
                json={"wa": wa},
            )
    except Exception as ex:  # noqa: BLE001
        print(f"[heartbeat] reporte falló: {ex}", flush=True)


def arrancar() -> None:
    global _sched
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except Exception as ex:  # noqa: BLE001
        print(f"[heartbeat] APScheduler no disponible ({ex}); watchdog OFF", flush=True)
        return
    _sched = BackgroundScheduler(timezone=config.REPORTE_TZ)
    _sched.add_job(
        latir, "interval", minutes=2, id="bot_heartbeat",
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=30),
    )
    _sched.start()
    print("[heartbeat] watchdog del bot cada 2 min (reporta wa al back).", flush=True)
