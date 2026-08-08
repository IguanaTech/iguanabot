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
    # TOPE DE EFECTIVO EN MANO: no es "hace días" como la de arriba, es AHORA MISMO — anda con más
    # plata encima de la que se le fijó. No lo bloquea (sigue trabajando); el aviso existe para que
    # el admin decida si lo manda a depositar.
    for m in alertas.get("mensajeros_sobre_tope", []) or []:
        ref = f"tope:mensajero:{m.get('mensajero_empleado_id')}"
        nombre = m.get("mensajero_nombre") or "Un mensajero"
        avisos.append((ref,
            f"🎒 *LLEVA MUCHO EFECTIVO* — {nombre}\n"
            f"Carga RD$ {_fmt_money(m.get('saldo'))}, RD$ {_fmt_money(m.get('exceso'))} por encima de "
            f"su tope de RD$ {_fmt_money(m.get('tope'))}. No se le bloquea nada: decide si lo mandas "
            f"a depositar o a entregar en central. También puedes bajar la alerta en el CRM → "
            f"Operaciones (con motivo)."
        ))
    # EL BALANCE DE UNA BANCA NO CUADRA. El POS recibió del back un balance que no cierra consigo
    # mismo y se negó a mostrarlo — bien hecho: un número de dinero en el que no se puede confiar es
    # peor que ninguno. NO ES UN ERROR DE LA VENDEDORA, y el mensaje lo dice: ella es la que lo ve
    # primero, y mientras tanto está trabajando sin ver su balance.
    for d in alertas.get("pos_descuadres", []) or []:
        if d.get("reportes") is None:
            print(f"[watcher] descuadre sin 'reportes', no se avisa: {str(d)[:120]}")
            continue
        ref = f"descuadre:estacion:{d.get('estacion_id')}"
        banca = d.get("nombre") or "Una banca"
        est = d.get("estacion_nombre") or "una estación"
        n = d["reportes"]
        avisos.append((ref,
            f"⚖️ *EL BALANCE NO CUADRA* — {banca} ({est})\n"
            f"El punto de venta recibió un balance que no cierra consigo mismo y se negó a "
            f"mostrarlo ({n} vez/veces). No es un error de la vendedora: es una cuenta del sistema. "
            f"Mientras tanto ella está trabajando sin ver su balance. Revisa la banca."
        ))
    for b in alertas.get("bancas_tickets_sin_recoger", []) or []:
        # UN AVISO QUE NO SABE CUÁNTO ES, NO SALE.
        #
        # Esto era `b.get("pendientes") or "?"`, y ese «?» fue el único síntoma visible del peor bug
        # de la semana: el back mandaba las DEUDAS de empleados bajo el rótulo de tickets, y al admin
        # le llegó «TICKETS SIN RECOGER — Dora La Exploradora, ? ticket(s)» cuando era una deuda de
        # RD$48.936. El back ya está arreglado y tiene su prueba de contrato, pero el bot no tenía
        # defensa propia: si mañana le llega otra lista con la forma equivocada, volvería a mandar un
        # mensaje de dinero con un signo de pregunta. Un aviso que no puede decir cuánto es no
        # informa: alarma. Mejor no mandarlo y dejar el rastro en el log.
        if b.get("pendientes") is None:
            print(f"[watcher] item de tickets sin 'pendientes', no se avisa: {str(b)[:120]}")
            continue
        ref = f"estancado:tickets:{b.get('banca_id')}"
        nombre = b.get("nombre") or "Una banca"
        n = b["pendientes"]
        dias = b.get("dias_mas_viejo")
        dias_txt = f", el más viejo {dias} día(s)" if dias is not None else ""
        # EL MENSAJE TIENE QUE DECIR CUÁNTO Y CUÁLES.
        #
        # Antes decía «2 ticket(s) vencidos sin recoger, el más viejo 7 día(s)» y nada más. Dos
        # tickets pueden ser RD$40 o RD$18.020, y la decisión de mandar un mensajero hoy o el lunes
        # no es la misma. Con dinero, un aviso que no dice cuánto obliga a ir a buscarlo al CRM —
        # y entonces el aviso no sirvió de nada, sólo asustó.
        #
        # Se agregan el MONTO y los CÓDIGOS (hasta 5) para que el admin pueda nombrarlos por
        # teléfono en vez de decir «los tickets viejos».
        monto = b.get("monto_total")
        monto_txt = f" por RD$ {_fmt_money(monto)}" if monto not in (None, "", "0") else ""
        codigos = [c for c in (b.get("codigos") or []) if c]
        codigos_txt = ""
        if codigos:
            lista = ", ".join(str(c) for c in codigos[:5])
            resto = int(n) - len(codigos) if str(n).isdigit() else 0
            codigos_txt = f"\nSon: {lista}" + (f" y {resto} más." if resto > 0 else ".")
        avisos.append((ref,
            f"🎫 *TICKETS SIN RECOGER* — {nombre}\n"
            f"{n} ticket(s) vencidos sin recoger{monto_txt}{dias_txt}.{codigos_txt}\n"
            f"Manda a recogerlos, o baja la alerta en el CRM → Operaciones."
        ))
    return avisos


def _alarmas_anulacion_por_vencer(consorcio: Consorcio) -> list[tuple[str, str]]:
    """Pedidos de ANULAR un ticket que van camino a quedar sin atender.

    Es la única alarma con HORA LÍMITE DURA. Cuando la lotería cierra no lo anula nadie —ni el
    admin— y el ticket queda válido, y si salió, gana. Por eso no encaja en ninguna de las otras
    dos familias: no es un pico transitorio que se puede re-avisar cada media hora para siempre, ni
    un estado que dura días y se recuerda una vez al día. Sirve ahora o no sirve.

    Nace de un caso real que está en tribunales: la clienta pidió anular, la empleada se olvidó, el
    número salió ganador. El pedido existía sólo en un WhatsApp y nadie se enteró de que quedó sin
    atender.

    Dos avisos por pedido y no más, por eso la clave lleva el TRAMO:
      · el primero cuando entra en la ventana de aviso;
      · el segundo cuando quedan 15 minutos o menos, con otro tono.
    Un tercero no ayudaría: si a esa altura nadie lo miró, el problema no es el recordatorio.
    """
    avisos = []
    try:
        d = _get(consorcio, "/api/crm/dashboard/operativo")
    except Exception as ex:  # noqa: BLE001
        print(f"[watcher] operativo (anulaciones) {consorcio.nombre}: {ex}")
        return avisos

    for a in (d.get("alertas", {}) or {}).get("anulaciones_por_vencer", []) or []:
        mins = a.get("minutos_restantes")
        if mins is None:
            continue
        sid = a.get("id")
        if mins < 0:
            # Ya se pasó. Se avisa UNA vez, porque no hay nada que hacer y sí algo que saber: ese
            # ticket quedó válido. Callarlo sería justo el silencio que hoy está en tribunales.
            avisos.append((f"anulacion:{sid}:vencido",
                "🎫 *PEDIDO DE ANULACIÓN VENCIDO*\n"
                f"El pedido #{a.get('numero')} se pasó de la hora sin atenderse. "
                "La lotería cerró: ese ticket es VÁLIDO y si salió, gana. "
                "Ya no lo puede anular nadie."
            ))
            continue

        tramo = "urgente" if mins <= 15 else "aviso"
        cabeza = "🚨 *ÚLTIMOS MINUTOS*" if tramo == "urgente" else "🎫 *PEDIDO DE ANULACIÓN*"
        avisos.append((f"anulacion:{sid}:{tramo}",
            f"{cabeza} — quedan {mins} min\n"
            f"La banca pidió anular un ticket (pedido #{a.get('numero')}) y sigue sin resolverse. "
            "Cuando cierre la lotería ya no se puede, ni por el admin: el ticket queda válido. "
            "Contéstame «aprobar solicitud» o resuélvelo en el CRM."
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
        # Los pedidos de anulación van acá y NO con los recordatorios diarios: su ventana se
        # mide en minutos. La clave lleva el tramo (aviso/urgente/vencido), así que salen dos
        # veces por pedido como mucho.
        if config.AVISO_ANULACION:
            avisos += _alarmas_anulacion_por_vencer(full)
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
            destinos = reportes.destinatarios_alarma(full.id, full)
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
