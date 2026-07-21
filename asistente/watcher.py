"""Vigilante de alarmas de dinero (proactivo).

Cada cierto rato revisa el riesgo de cada consorcio y, si aparece algo, le avisa a sus admin/encargado
por WhatsApp. Las DOS que Eduardo marcó como críticas:

  1. PREMIO MÁXIMO / banca descubierta — si sale el número más jugado de una banca, no lo paga con su
     caja. Fuente: GET /api/crm/riesgo/cobertura  (feature #3, "bancas descubiertas").
  2. NÚMERO CALIENTE — la misma jugada apareciendo en muchas bancas en poco tiempo (dato filtrado /
     dinero inteligente). Fuente: GET /api/crm/riesgo/velocidad, nivel 'fuerte'.

No recomputa nada: relaya las señales que el back ya calcula. Dedup con cooldown para no spamear.
Solo lectura — avisar no mueve dinero; la decisión de bajar el cupo/bloquear la toma el humano en el CRM.
"""
import time

import httpx

from .config import config
from .registry import Consorcio, consorcio_por_id, listar_consorcios
from . import auth, reportes  # reportes.destinatarios_alarma()
from .scheduler import enviar_por_puente

# Dedup en memoria: clave → epoch del último aviso ENVIADO. Cooldown evita repetir la misma alarma.
# (Para producción multi-instancia, mover a la DB; en un proceso alcanza.)
_ultimo_aviso: dict[str, float] = {}


def _en_cooldown(clave: str) -> bool:
    """Solo CONSULTA si la alarma está en cooldown. NO marca nada: la marca se pone al enviar con
    éxito (_marcar_avisado). Así, si el puente falla, la alarma no queda 'silenciada' sin haber salido."""
    ult = _ultimo_aviso.get(clave, 0)
    return time.time() - ult < config.ALARMAS_COOLDOWN_MIN * 60


def _marcar_avisado(clave: str) -> None:
    """Registra el envío exitoso: arranca el cooldown de esa alarma."""
    _ultimo_aviso[clave] = time.time()


def _get(consorcio: Consorcio, path: str, params: dict | None = None):
    url = consorcio.backend_url + path
    headers = {"Authorization": f"Bearer {auth.token_de(consorcio)}"}
    with httpx.Client(timeout=30) as client:
        r = client.get(url, headers=headers, params=params or {})
        r.raise_for_status()
        return r.json()


def _alarmas_premio(consorcio: Consorcio) -> list[tuple[str, str]]:
    """Bancas descubiertas: si sale su número más jugado, no lo cubren con su caja.
    Devuelve (clave, texto) — la clave sirve para marcar el cooldown SOLO tras enviar."""
    avisos = []
    try:
        d = _get(consorcio, "/api/crm/riesgo/cobertura")
    except Exception as ex:  # noqa: BLE001
        print(f"[watcher] cobertura {consorcio.nombre}: {ex}")
        return avisos
    for f in d.get("cobertura", []):
        clave = f"premio:{consorcio.id}:{f.get('estacion_id')}:{f.get('loteria_id')}:{f.get('numero')}"
        if _en_cooldown(clave):
            continue
        avisos.append((clave,
            f"🔴 *PREMIO SIN COBERTURA* — {f.get('banca_nombre')}\n"
            f"Número {f.get('numero')} ({f.get('loteria_nombre')}): si sale paga RD$ {f.get('exposicion_pago')}, "
            f"y su caja es RD$ {f.get('caja')} (le falta RD$ {f.get('descubierto')}).\n"
            f"Cupo sugerido para cubrirla: RD$ {f.get('tope_sugerido')}. Decide en el CRM → Riesgo."
        ))
    return avisos


def _alarmas_caliente(consorcio: Consorcio) -> list[tuple[str, str]]:
    """Números calientes por dispersión cross-banca (nivel 'fuerte' = anómalo)."""
    avisos = []
    try:
        d = _get(consorcio, "/api/crm/riesgo/velocidad", {"ventana_min": config.VELOCIDAD_VENTANA_MIN})
    except Exception as ex:  # noqa: BLE001
        print(f"[watcher] velocidad {consorcio.nombre}: {ex}")
        return avisos
    for c in d.get("calientes", []):
        if c.get("nivel") != "fuerte":
            continue
        clave = f"caliente:{consorcio.id}:{c.get('loteria_id')}:{c.get('tipo_jugada')}:{c.get('numero')}"
        if _en_cooldown(clave):
            continue
        avisos.append((clave,
            f"🔥 *NÚMERO CALIENTE* — posible dato filtrado\n"
            f"El {c.get('numero')} ({c.get('loteria_nombre')} · {c.get('tipo_jugada')}) apareció en "
            f"{c.get('bancas')} bancas distintas en {config.VELOCIDAD_VENTANA_MIN} min "
            f"(RD$ {c.get('consumido')} jugados). Revisa/bloquea en el CRM → Riesgo."
        ))
    return avisos


def _alarmas_ganador(consorcio: Consorcio) -> list[tuple[str, str]]:
    """Ganador REAL alto: un ticket que YA ganó un premio grande hoy (no la exposición: el premio real)."""
    from datetime import date
    avisos = []
    hoy = date.today().isoformat()
    try:
        d = _get(consorcio, "/api/reportes/ganadores", {"desde": hoy, "hasta": hoy})
    except Exception as ex:  # noqa: BLE001
        print(f"[watcher] ganadores {consorcio.nombre}: {ex}")
        return avisos
    for t in d.get("data", []):
        if t.get("estado") not in ("ganador_pendiente", "ganador_pagado"):
            continue
        try:
            premio = int(t.get("premio") or 0)
        except (ValueError, TypeError):
            continue
        if premio < config.UMBRAL_GANADOR:
            continue
        clave = f"ganador:{consorcio.id}:{t.get('id')}"
        if _en_cooldown(clave):
            continue
        estado = "por pagar" if t.get("estado") == "ganador_pendiente" else "pagado"
        avisos.append((clave,
            f"🏆 *GANADOR ALTO* — premio de RD$ {premio:,}".replace(",", ".") +
            f"\nTicket {t.get('codigo_ticket') or t.get('id')} ({estado}). Ojo con la caja de esa banca."
        ))
    return avisos


def revisar_todos() -> None:
    """Un ciclo: revisa cada consorcio y empuja las alarmas nuevas a SUS admins (no a los de otro)."""
    consorcios = list(listar_consorcios())
    for c in consorcios:
        full = consorcio_por_id(c.id)
        if not full:
            continue
        avisos = _alarmas_premio(full) + _alarmas_caliente(full) + _alarmas_ganador(full)
        if not avisos:
            continue
        # A quién avisar SIN cruzar consorcios: la lista explícita ALARMAS_DESTINATARIOS no trae marca
        # de consorcio, así que solo es segura con UN consorcio (Fase 0). Con 2+ cruzaría alarmas de A
        # hacia los admins de B → se usa el directorio de ESTE consorcio (roles globales admin/contable).
        if config.ALARMAS_DESTINATARIOS and len(consorcios) == 1:
            destinos = config.ALARMAS_DESTINATARIOS
        else:
            destinos = reportes.destinatarios_alarma(full.id)
        if not destinos:
            print(f"[watcher] {full.nombre}: hay alarmas pero no hay destinatarios configurados.")
            continue
        for clave, texto in avisos:
            enviado = False
            for tel in destinos:
                try:
                    enviar_por_puente(tel, texto, None, None)
                    enviado = True
                except Exception as ex:  # noqa: BLE001
                    print(f"[watcher] fallo avisando a {tel}: {ex}")
            if enviado:  # marca el cooldown SOLO si salió a alguien (si no, reintenta el próximo ciclo)
                _marcar_avisado(clave)


def arrancar() -> None:
    """Programa el vigilante en un intervalo si ALARMAS_PUSH está activo."""
    if not config.ALARMAS_PUSH:
        print("[watcher] vigilante de alarmas APAGADO (ALARMAS_PUSH=false).")
        return
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except Exception as ex:  # noqa: BLE001
        print(f"[watcher] APScheduler no disponible ({ex}); no se programa el vigilante")
        return
    sched = BackgroundScheduler(timezone=config.REPORTE_TZ)
    sched.add_job(revisar_todos, "interval", seconds=config.ALARMAS_INTERVALO_SEG, id="watcher_alarmas")
    sched.start()
    print(f"[watcher] vigilante de alarmas cada {config.ALARMAS_INTERVALO_SEG}s.")
