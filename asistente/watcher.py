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
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx
import psycopg

from .config import config
from .registry import Consorcio, consorcio_por_id, listar_consorcios
from . import auth, reportes  # reportes.destinatarios_alarma()
from .scheduler import enviar_por_puente

# Dedup en memoria: clave → epoch del último aviso ENVIADO. Cooldown evita repetir la misma alarma.
# (Para producción multi-instancia, mover a la DB; en un proceso alcanza.)
_ultimo_aviso: dict[str, float] = {}


def _fmt_money(v) -> str:
    """RD$ enteros con separador de miles al estilo local (1.234.567)."""
    try:
        return f"{int(v or 0):,}".replace(",", ".")
    except (ValueError, TypeError):
        return str(v)


def _hoy_tz() -> date:
    return datetime.now(ZoneInfo(config.REPORTE_TZ)).date()


def _hora_tz() -> int:
    return datetime.now(ZoneInfo(config.REPORTE_TZ)).hour


# ── Dedup DIARIO persistido (sobrevive reinicios; el cooldown en memoria no) ──
# Tabla propia del bot: un recordatorio por (consorcio, ref-de-alerta, día). Se crea sola.
_tabla_lista = False


def _asegurar_tabla_recordatorios() -> None:
    global _tabla_lista
    if _tabla_lista:
        return
    with psycopg.connect(config.DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS recordatorio_diario ("
                "  consorcio_id text NOT NULL,"
                "  ref text NOT NULL,"
                "  dia date NOT NULL,"
                "  creado_en timestamptz NOT NULL DEFAULT now(),"
                "  PRIMARY KEY (consorcio_id, ref, dia))"
            )
        conn.commit()
    _tabla_lista = True


def _ya_recordado_hoy(consorcio_id: str, ref: str, dia: date) -> bool:
    _asegurar_tabla_recordatorios()
    with psycopg.connect(config.DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM recordatorio_diario WHERE consorcio_id=%s AND ref=%s AND dia=%s",
                (consorcio_id, ref, dia),
            )
            return cur.fetchone() is not None


def _marcar_recordado(consorcio_id: str, ref: str, dia: date) -> None:
    _asegurar_tabla_recordatorios()
    with psycopg.connect(config.DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO recordatorio_diario (consorcio_id, ref, dia) VALUES (%s,%s,%s) "
                "ON CONFLICT DO NOTHING",
                (consorcio_id, ref, dia),
            )
        conn.commit()


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


def _alarmas_banca_descubierta(consorcio: Consorcio) -> list[tuple[str, str]]:
    """Banca que NO puede cubrir sus premios REALES: su disponible quedó negativo. La reserva de
    premios ya congela el monto de los ganadores pendientes; si la caja no alcanza, la banca no
    puede pagar (money-real y reactivo).

    Reemplaza a la vieja alerta PREDICTIVA de 'premio sin cobertura', que disparaba 'si sale X no
    puedes pagar' para CADA número descubierto de CADA banca = la operación NORMAL de toda banca
    (ninguna caja cubre su número grande, para eso está la central) → ruido inmanejable a escala.
    Fuente: /dashboard/operativo → alertas.bancas_disponible_negativo (banca_id, nombre, hueco).

    Devuelve (ref, texto). El dedup NO es el cooldown de 30 min (spameaba varias veces al día); va
    con la cadencia DIARIA persistida (una vez al día HASTA que se resuelva), como el recordatorio de
    dinero estancado — la maneja revisar_todos."""
    avisos = []
    try:
        d = _get(consorcio, "/api/crm/dashboard/operativo")
    except Exception as ex:  # noqa: BLE001
        print(f"[watcher] operativo(descubierta) {consorcio.nombre}: {ex}")
        return avisos
    for b in (d.get("alertas", {}) or {}).get("bancas_disponible_negativo", []) or []:
        ref = f"descubierta:{b.get('banca_id')}"
        avisos.append((ref,
            f"🔴 *BANCA SIN FONDOS PARA PREMIOS* — {b.get('nombre')}\n"
            f"Su caja no cubre los premios que YA debe: le falta RD$ {_fmt_money(b.get('hueco'))}. "
            f"Fondéala (entrega) o resuélvelo en el CRM antes de que el ganador se presente a cobrar."
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


# La vieja alerta de "GANADOR ALTO" (premio >= UMBRAL) disparaba por CUALQUIER premio grande, aunque
# la banca lo pudiera pagar sin problema → no es lo que importa. La versión money-aware que pidió
# Eduardo ("alertar si sacan y NO tiene el dinero") es _alarmas_banca_descubierta (arriba): una banca
# cuyo disponible quedó negativo = no puede cubrir sus premios reales. Por eso se quitó ésta.


def _alarmas_dinero_estancado(consorcio: Consorcio) -> list[tuple[str, str]]:
    """Recordatorio de DINERO VIVO estancado (una vez al día HASTA que se resuelva). Lee el tablero
    operativo del back (que ya quita las alertas gestionadas/reconocidas/pospuestas en el CRM) y
    relaya las dos alertas de dinero-vivo que faltaban en el canal de WhatsApp:
      - mensajero_saldo_estancado: mensajero con efectivo en mano hace días sin llevarlo a la central.
      - bancas_tickets_sin_recoger: banca con papelería vencida sin recoger hace días.
    Devuelve (ref_estable, texto). El dedup NO es el cooldown de 30 min: es DIARIO y persistido."""
    avisos = []
    try:
        d = _get(consorcio, "/api/crm/dashboard/operativo")
    except Exception as ex:  # noqa: BLE001
        print(f"[watcher] operativo {consorcio.nombre}: {ex}")
        return avisos
    alertas = d.get("alertas", {}) or {}
    for m in alertas.get("mensajero_saldo_estancado", []) or []:
        ref = f"estancado:mensajero:{m.get('mensajero_empleado_id')}"
        nombre = m.get("mensajero_nombre") or "Un mensajero"
        dias = m.get("dias_con_dinero")
        dias_txt = f"{dias} día(s)" if dias is not None else "varios días"
        avisos.append((ref,
            f"💵 *DINERO SIN ENTREGAR* — {nombre}\n"
            f"Lleva {dias_txt} con RD$ {_fmt_money(m.get('saldo'))} en mano sin llevarlo a la central. "
            f"Coordina la entrega/traspaso, o baja la alerta en el CRM → Operaciones (con motivo)."
        ))
    for b in alertas.get("bancas_tickets_sin_recoger", []) or []:
        ref = f"estancado:tickets:{b.get('banca_id')}"
        nombre = b.get("nombre") or "Una banca"
        n = b.get("pendientes") or "?"
        dias = b.get("dias_mas_viejo")
        dias_txt = f", el más viejo {dias} día(s)" if dias is not None else ""
        avisos.append((ref,
            f"🎫 *TICKETS SIN RECOGER* — {nombre}\n"
            f"{n} ticket(s) vencidos sin recoger{dias_txt}. "
            f"Manda a recogerlos, o baja la alerta en el CRM → Operaciones."
        ))
    return avisos


def _enviar_a_todos(destinos: list[str], texto: str) -> bool:
    """Empuja `texto` a cada teléfono; devuelve True si salió al menos a uno."""
    enviado = False
    for tel in destinos:
        try:
            enviar_por_puente(tel, texto, None, None)
            enviado = True
        except Exception as ex:  # noqa: BLE001
            print(f"[watcher] fallo avisando a {tel}: {ex}")
    return enviado


def revisar_todos() -> None:
    """Un ciclo: revisa cada consorcio y empuja las alarmas nuevas a SUS admins (no a los de otro)."""
    consorcios = list(listar_consorcios())
    hoy = _hoy_tz()
    # El recordatorio diario solo dispara en horario razonable (no a medianoche). El primer tick del
    # día tras esta hora lo manda; el dedup persistido evita repetir el resto del día.
    en_horario = config.RECORDATORIO_DINERO and _hora_tz() >= config.RECORDATORIO_DINERO_HORA
    for c in consorcios:
        full = consorcio_por_id(c.id)
        if not full:
            continue
        # Alarmas TRANSITORIAS (cooldown de 30 min, en memoria) — solo si ALARMAS_PUSH. Solo el número
        # caliente (dato filtrado): es un pico transitorio y urgente, re-avisar cada 30 min está bien.
        avisos = _alarmas_caliente(full) if config.ALARMAS_PUSH else []
        # Recordatorios de estados PERSISTENTES de dinero (UNA vez al día, dedup persistido) — solo si
        # RECORDATORIO_DINERO. Dinero estancado del mensajero + banca sin fondos para premios: son
        # estados que duran días; avisar cada 30 min spameaba (Eduardo 2026-07-21) → una sola vez al
        # día HASTA que se resuelva (bajar la alerta en el CRM lo apaga).
        recordatorios = (
            [(ref, txt) for (ref, txt) in (_alarmas_dinero_estancado(full) + _alarmas_banca_descubierta(full))
             if not _ya_recordado_hoy(full.id, ref, hoy)]
            if en_horario else []
        )
        if not avisos and not recordatorios:
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
            if _enviar_a_todos(destinos, texto):  # marca el cooldown SOLO si salió a alguien
                _marcar_avisado(clave)
        for ref, texto in recordatorios:
            if _enviar_a_todos(destinos, texto):  # marca el día SOLO si salió (si no, reintenta)
                _marcar_recordado(full.id, ref, hoy)


def arrancar() -> None:
    """Programa el vigilante en un intervalo. Corre si hay alarmas de lotería (ALARMAS_PUSH) O el
    recordatorio diario de dinero (RECORDATORIO_DINERO) — cada uno se activa por separado."""
    if not config.ALARMAS_PUSH and not config.RECORDATORIO_DINERO:
        print("[watcher] vigilante APAGADO (ALARMAS_PUSH=false y RECORDATORIO_DINERO=false).")
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
