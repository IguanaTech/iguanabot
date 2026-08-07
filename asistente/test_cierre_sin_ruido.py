"""El bot NO manda mensajes que el admin no puede usar.

    python3 -m asistente.test_cierre_sin_ruido

El reporte de cierre venía con un mensaje pegado aparte: «⚠ No pude entregar este reporte a todos.
Sin canal: 8492074887». Salía en CADA reporte, siempre igual, porque ese número simplemente no está
vinculado — un estado que no cambia solo. Eduardo (2026-08-06): «si no está vinculado no necesito
saber eso, no entiendo la utilidad de enviármelo».

El fondo es la misma regla que hace que un día quieto no se reporte: un aviso que se repite sin
cambiar deja de leerse, y arrastra a los que sí importan. Quién recibe se administra en el CRM; a
quién le llegó queda en el log.

Esta prueba fija que por cada reporte sale UN mensaje por destinatario y ni uno más.
"""
from __future__ import annotations

import sys
import types

# `scheduler` arrastra httpx/psycopg/apscheduler para pegar al back; acá se prueba lógica pura
# (¿cuántos mensajes salen?) sin tocar la red, como el resto de las pruebas de este repo.
for _m in ("httpx", "psycopg", "apscheduler", "apscheduler.schedulers",
           "apscheduler.schedulers.background", "apscheduler.triggers",
           "apscheduler.triggers.cron", "apscheduler.triggers.interval"):
    sys.modules.setdefault(_m, types.ModuleType(_m))
for _mod, _attr in (("apscheduler.schedulers.background", "BackgroundScheduler"),
                    ("apscheduler.triggers.cron", "CronTrigger"),
                    ("apscheduler.triggers.interval", "IntervalTrigger")):
    setattr(sys.modules[_mod], _attr, type(_attr, (), {}))

from . import scheduler  # noqa: E402

fallos: list[str] = []


def check(cond: bool, que: str) -> None:
    if not cond:
        fallos.append(que)


# ── El aviso de "no le llegó a fulano" NO viaja por el puente ─────────────────
enviados: list[tuple[str, str]] = []
scheduler.enviar_por_puente = lambda tel, texto, *a, **k: enviados.append((tel, texto))

scheduler._avisar_no_entregados(["8492074887"], ["18099074550"])
check(enviados == [],
      "un destinatario sin canal NO puede generar un mensaje al que sí recibió "
      "(se registra en el log, que es donde vive la configuración, no en el chat)")

# Y con todo entregado, tampoco (nunca hubo motivo, pero que quede fijado).
scheduler._avisar_no_entregados([], ["18099074550"])
check(enviados == [], "sin fallidos no se manda nada, obviamente")


if __name__ == "__main__":
    if fallos:
        print(f"FALLA: {len(fallos)}")
        for f in fallos:
            print("  ·", f)
        sys.exit(1)
    print("OK — el cierre no manda ruido.")
