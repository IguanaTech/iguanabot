"""Envío automático del reporte al CIERRE del día.

El disparo NO es a hora fija: una lotería puede cerrar más tarde. El bot SONDEA el estado del día
operativo del back (GET /api/crm/dashboard/dia-operativo) cada pocos minutos y, por cada consorcio,
manda el reporte cuando el back marca el día como CERRADO — es decir, cuando la ÚLTIMA lotería del día
ya publicó números (los premios) y pasó el margen del consorcio. Un envío por día operativo (dedup
por (consorcio, día) en `recordatorio_diario`).

Red de seguridad: si el operador nunca cargó la última lotería, el día no cierra; a una hora tope el
reporte sale igual, marcado como PRELIMINAR, para que el admin no se quede sin nada.

El envío sale por el puente (POST BRIDGE_URL/enviar).

⚠ En Fase 0/Baileys esto es 'bot-inicia' → riesgo de baneo si son muchos números. Por eso viene
APAGADO por defecto (REPORTE_EOD_AUTO=false): úsalo para pocos/pruebas. El envío masivo diario va con
la Cloud API oficial + plantillas aprobadas (Fase 1).
"""
import base64
from datetime import date, datetime, timedelta, timezone

import httpx

from .config import config
from .registry import consorcio_por_id, listar_consorcios
from . import reportes
# NB: el dedup diario (_ya_recordado_hoy/_marcar_recordado) vive en watcher, pero watcher importa
# enviar_por_puente de ESTE módulo → import diferido dentro de revisar_cierres() para no ciclar.

_REF_CIERRE = "cierre-eod"


def _tz():
    """Zona local para las decisiones de hora (gate de sondeo + hora tope). RD es UTC-4 fijo (sin
    horario de verano); si REPORTE_TZ no resuelve, caemos a ese offset."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(config.REPORTE_TZ)
    except Exception:  # noqa: BLE001
        return timezone(timedelta(hours=-4))


def _hhmm(valor: str):
    """'HH:MM' → (hh, mm); '' o inválido → None (sin hora tope)."""
    if not valor or ":" not in valor:
        return None
    try:
        hh, mm = valor.split(":")[:2]
        return int(hh), int(mm)
    except Exception:  # noqa: BLE001
        return None


def enviar_por_puente(telefono: str, texto: str, pdf: bytes | None, nombre: str | None) -> None:
    payload = {"telefono": telefono, "texto": texto}
    if pdf:
        payload["documento_base64"] = base64.b64encode(pdf).decode()
        payload["documento_nombre"] = nombre or "reporte.pdf"
    with httpx.Client(timeout=30) as client:
        client.post(f"{config.BRIDGE_URL}/enviar", json=payload).raise_for_status()


def _enviar_a_consorcio(full, c_id: str, dia: date, preliminar: bool) -> int:
    """Arma y manda el reporte del `dia` a los destinatarios del consorcio. Devuelve cuántos salieron.
    `preliminar=True` antepone un aviso (el día aún no cerró formalmente)."""
    rep = reportes.datos_dia(full, dia)
    texto = reportes.texto(rep)
    if preliminar:
        texto = ("⚠ *Reporte preliminar* — el día aún no cerró formalmente en el sistema "
                 "(la última lotería podría no haber publicado sus números todavía).\n\n") + texto
    pdf = reportes.pdf(rep)
    enviados = 0
    for tel in reportes.destinatarios(c_id):
        try:
            enviar_por_puente(tel, texto, pdf, f"reporte-{rep.fecha}.pdf")
            enviados += 1
        except Exception as ex:  # noqa: BLE001 — un destinatario que falla no frena a los demás
            print(f"[cierre] fallo enviando a {tel}: {ex}")
    return enviados


def revisar_cierres() -> dict:
    """Sondeo: por cada consorcio, si su día operativo ya cerró (o llegó la hora tope) y no se mandó
    hoy, manda el reporte. Devuelve un conteo por consorcio con lo que pasó."""
    from .watcher import _ya_recordado_hoy, _marcar_recordado  # diferido: evita el ciclo con watcher

    # Sin gate de hora: el reporte se ata al cierre REAL del día (última lotería publicada + margen),
    # no a una franja horaria. Se sondea todo el día; el dedup evita reenvíos y el sondeo es barato.
    ahora = datetime.now(_tz())
    tope = _hhmm(config.REPORTE_EOD_TOPE_HORA)
    en_tope = tope is not None and (ahora.hour, ahora.minute) >= tope

    resultado: dict[str, str] = {}
    for c in listar_consorcios():
        full = consorcio_por_id(c.id)  # con token descifrado
        if not full:
            continue
        estado = reportes.estado_dia_operativo(full)
        if estado is None:
            resultado[c.nombre] = "back-inalcanzable"  # se reintenta en el próximo sondeo
            continue
        try:
            dia = date.fromisoformat(estado.get("fecha"))
        except Exception:  # noqa: BLE001
            dia = ahora.date()
        if _ya_recordado_hoy(c.id, _REF_CIERRE, dia):
            resultado[c.nombre] = "ya-enviado"
            continue

        cerrado = bool(estado.get("cerrado"))
        if not cerrado and not en_tope:
            resultado[c.nombre] = "espera"  # aún falta que cierre la última lotería
            continue

        try:
            enviados = _enviar_a_consorcio(full, c.id, dia, preliminar=not cerrado)
        except Exception as ex:  # noqa: BLE001
            print(f"[cierre] fallo armando reporte de {c.nombre}: {ex}")
            resultado[c.nombre] = "error"
            continue
        # Marcamos SOLO si algo salió (si el puente falló con todos, reintenta en el próximo sondeo).
        if enviados > 0:
            _marcar_recordado(c.id, _REF_CIERRE, dia)
        resultado[c.nombre] = ("preliminar" if not cerrado else "cerrado") + f":{enviados}"
    # Observabilidad: dejamos rastro de lo que hizo cada sondeo que tuvo algo que decidir. flush=True
    # para que se vea aunque el stdout esté bufferizado bajo uvicorn.
    if resultado:
        print(f"[cierre] sondeo {ahora.strftime('%H:%M')} RD => {resultado}", flush=True)
    return resultado


def enviar_reporte_cierre() -> dict:
    """Envío MANUAL/forzado: manda el reporte del día de HOY a los admins de cada consorcio, sin
    esperar el cierre ni respetar el dedup. Útil para pruebas o para un disparo a mano."""
    resultado: dict[str, int] = {}
    hoy = datetime.now(_tz()).date()
    for c in listar_consorcios():
        full = consorcio_por_id(c.id)
        if not full:
            continue
        try:
            resultado[full.nombre] = _enviar_a_consorcio(full, c.id, hoy, preliminar=False)
        except Exception as ex:  # noqa: BLE001
            print(f"[cierre] fallo armando reporte de {full.nombre}: {ex}")
            resultado[full.nombre] = -1
    return resultado


# Referencia global al scheduler: BackgroundScheduler corre en su propio hilo, pero si el objeto se
# recolecta (GC) al salir de arrancar(), el hilo puede morir. La guardamos para que viva todo el proceso.
_sched = None


def arrancar() -> None:
    """Programa el sondeo del cierre si REPORTE_EOD_AUTO está activo."""
    global _sched
    if not config.REPORTE_EOD_AUTO:
        print("[scheduler] envío automático de cierre APAGADO (REPORTE_EOD_AUTO=false).", flush=True)
        return
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except Exception as ex:  # noqa: BLE001
        print(f"[scheduler] APScheduler no disponible ({ex}); no se programa el cierre", flush=True)
        return
    _sched = BackgroundScheduler(timezone=config.REPORTE_TZ)
    # Sondea el estado del día operativo cada REPORTE_EOD_POLL_MIN minutos; el primer chequeo corre
    # enseguida (por si el día ya cerró cuando el bot arranca de tarde).
    _sched.add_job(
        revisar_cierres, "interval",
        minutes=max(1, config.REPORTE_EOD_POLL_MIN),
        id="reporte_cierre",
        next_run_time=datetime.now(_tz()) + timedelta(seconds=20),
    )
    _sched.start()
    tope = config.REPORTE_EOD_TOPE_HORA or "(sin red)"
    print(f"[scheduler] cierre por evento: sondeo del día operativo c/{config.REPORTE_EOD_POLL_MIN}min, "
          f"dispara al cerrar; tope {tope} {config.REPORTE_TZ}.", flush=True)
