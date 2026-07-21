"""Envío automático del reporte al CIERRE del día.

A la hora local configurada, por cada consorcio: arma el reporte y se lo manda (texto + PDF) a sus
admins/encargados. El envío sale por el puente (POST BRIDGE_URL/enviar).

⚠ En Fase 0/Baileys esto es 'bot-inicia' → riesgo de baneo si son muchos números. Por eso viene
APAGADO por defecto (REPORTE_EOD_AUTO=false): úsalo para pocos/pruebas. El envío masivo diario va con
la Cloud API oficial + plantillas aprobadas (Fase 1).
"""
import base64

import httpx

from .config import config
from .registry import consorcio_por_id, listar_consorcios
from . import reportes


def enviar_por_puente(telefono: str, texto: str, pdf: bytes | None, nombre: str | None) -> None:
    payload = {"telefono": telefono, "texto": texto}
    if pdf:
        payload["documento_base64"] = base64.b64encode(pdf).decode()
        payload["documento_nombre"] = nombre or "reporte.pdf"
    with httpx.Client(timeout=30) as client:
        client.post(f"{config.BRIDGE_URL}/enviar", json=payload).raise_for_status()


def enviar_reporte_cierre() -> dict:
    """Genera y manda el reporte del día a los admins de cada consorcio. Devuelve un conteo por consorcio."""
    resultado: dict[str, int] = {}
    for c in listar_consorcios():
        full = consorcio_por_id(c.id)  # con token descifrado
        if not full:
            continue
        try:
            rep = reportes.datos_dia(full)
            texto = reportes.texto(rep)
            pdf = reportes.pdf(rep)
            enviados = 0
            for tel in reportes.destinatarios(c.id):
                try:
                    enviar_por_puente(tel, texto, pdf, f"reporte-{rep.fecha}.pdf")
                    enviados += 1
                except Exception as ex:  # noqa: BLE001 — un destinatario que falla no frena a los demás
                    print(f"[cierre] fallo enviando a {tel}: {ex}")
            resultado[full.nombre] = enviados
        except Exception as ex:  # noqa: BLE001
            print(f"[cierre] fallo armando reporte de {full.nombre}: {ex}")
            resultado[full.nombre] = -1
    return resultado


def arrancar() -> None:
    """Programa el cron del cierre si REPORTE_EOD_AUTO está activo."""
    if not config.REPORTE_EOD_AUTO:
        print("[scheduler] envío automático de cierre APAGADO (REPORTE_EOD_AUTO=false).")
        return
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except Exception as ex:  # noqa: BLE001
        print(f"[scheduler] APScheduler no disponible ({ex}); no se programa el cierre")
        return
    hh, mm = (config.REPORTE_EOD_HORA.split(":") + ["0"])[:2]
    sched = BackgroundScheduler(timezone=config.REPORTE_TZ)
    sched.add_job(enviar_reporte_cierre, "cron", hour=int(hh), minute=int(mm), id="reporte_cierre")
    sched.start()
    print(f"[scheduler] reporte de cierre programado {config.REPORTE_EOD_HORA} {config.REPORTE_TZ}.")
