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

from . import telegram_vinculo
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
    """Manda un mensaje que INICIA el bot (reporte de cierre, alarmas) por el canal de esa persona.

    ELIGE EL CANAL, no manda por los dos: quien está en Telegram lo recibe por Telegram; el resto,
    por WhatsApp. Mandar por ambos sería duplicarle el reporte a la misma persona, y peor: con el
    WhatsApp restringido, la mitad de los envíos fallaría y ensuciaría el registro de entregas.

    Telegram va PRIMERO a propósito: es el canal que hoy funciona sin restricciones. Si la persona
    no está vinculada, `chat_de` devuelve None y se cae a WhatsApp exactamente como antes."""
    payload = {"texto": texto}
    if pdf:
        payload["documento_base64"] = base64.b64encode(pdf).decode()
        payload["documento_nombre"] = nombre or "reporte.pdf"

    chat_id = telegram_vinculo.chat_de(telefono) if config.TELEGRAM_BOT_TOKEN else None
    if chat_id is not None:
        with httpx.Client(timeout=30) as client:
            client.post(f"{config.TELEGRAM_BRIDGE_URL}/enviar",
                        json={**payload, "chat_id": chat_id}).raise_for_status()
        return

    with httpx.Client(timeout=30) as client:
        client.post(f"{config.BRIDGE_URL}/enviar",
                    json={**payload, "telefono": telefono}).raise_for_status()


def _avisar_no_entregados(fallidos: list[str], entregados: list[str]) -> None:
    """Le cuenta a QUIEN SÍ recibió que a alguien no le llegó.

    Un envío que falla imprimía una línea en el log del contenedor y ahí moría. Nadie lee el log del
    contenedor — y desde el lado de la persona que no recibió nada, «no me llegó» y «no hubo nada que
    contar» se ven exactamente igual. O sea: el reporte podía dejar de llegarle a alguien por semanas
    sin que nadie se enterara.

    El aviso va a los que SÍ recibieron, que son los únicos alcanzables por definición. Es una línea
    corta pegada al mensaje que ya esperaban, no una notificación nueva: si nadie recibió nada, no
    hay a quién avisarle y el log sigue siendo el único registro (no se inventa un canal que no hay).
    """
    if not fallidos or not entregados:
        return
    quienes = ", ".join(fallidos)
    aviso = (
        "⚠ *No pude entregar este reporte a todos*\n"
        f"Sin canal: {quienes}.\n"
        "Suele ser que esa persona no está vinculada a Telegram y WhatsApp no está en servicio. "
        "Se vincula escribiéndole al bot de Telegram."
    )
    for tel in entregados:
        try:
            enviar_por_puente(tel, aviso, None, None)
        except Exception as ex:  # noqa: BLE001 — el aviso del aviso no puede encadenar fallos
            print(f"[cierre] no se pudo avisar del no-entregado a {tel}: {ex}")


def _enviar_senal_de_vida(full, c_id: str, rep, dia: date) -> int:
    """Una línea cuando el día cerró sin nada que contar. Ver el porqué en `_enviar_a_consorcio`."""
    venta = reportes.venta_del_dia(rep)
    cuerpo = (
        f"🌙 *{full.nombre}* — día cerrado, {dia}\n"
        f"Venta: {venta}. Nada pendiente.\n"
        "_Si no te llega este mensaje algún día, el bot no está corriendo._"
    )
    entregados, fallidos = [], []
    for tel in reportes.destinatarios(c_id, full):
        try:
            enviar_por_puente(tel, cuerpo, None, None)
            entregados.append(tel)
        except Exception as ex:  # noqa: BLE001
            fallidos.append(tel)
            print(f"[cierre] señal de vida a {tel} falló: {ex}")
    _avisar_no_entregados(fallidos, entregados)
    return len(entregados)


def _enviar_a_consorcio(full, c_id: str, dia: date, preliminar: bool) -> int:
    """Arma y manda el reporte del `dia` a los destinatarios del consorcio. Devuelve cuántos salieron.
    `preliminar=True` antepone un aviso (el día aún no cerró formalmente)."""
    rep = reportes.datos_dia(full, dia)

    # Si el día no tuvo movimiento, no se manda nada. Ver `reportes.hubo_movimiento`: un reporte
    # que a veces dice "no pasó nada" hace que dejen de abrirlo, y entonces el que sí importa
    # tampoco se abre.
    if not reportes.hubo_movimiento(rep):
        # Sin movimiento no va el reporte — pero SÍ una línea de vida.
        #
        # La regla de callarse cuando no hay nada es buena: un mensaje diario que a veces dice «no
        # pasó nada» entrena a no abrirlo. El problema es otro: desde el teléfono, «no llegó nada» y
        # «no hubo nada» se ven IGUAL, y uno de los dos significa que el bot está caído.
        #
        # Ya nos pasó: el envío automático del cierre llevaba tiempo apagado por configuración
        # (REPORTE_EOD_AUTO=false) y nadie se enteró, justamente porque el silencio se leía como
        # «no hubo novedades».
        #
        # Una línea, con el número que importa. No es un reporte: es la prueba de que el bot está
        # vivo y que el silencio de hoy es real.
        print(f"[cierre] {full.nombre} {dia}: sin movimiento, va la señal de vida.")
        return _enviar_senal_de_vida(full, c_id, rep, dia)

    texto = reportes.texto(rep)
    if preliminar:
        texto = ("⚠ *Reporte preliminar* — el día aún no cerró formalmente en el sistema "
                 "(la última lotería podría no haber publicado sus números todavía).\n\n") + texto
    pdf = reportes.pdf(rep)
    entregados, fallidos = [], []
    for tel in reportes.destinatarios(c_id, full):
        try:
            enviar_por_puente(tel, texto, pdf, f"reporte-{rep.fecha}.pdf")
            entregados.append(tel)
        except Exception as ex:  # noqa: BLE001 — un destinatario que falla no frena a los demás
            fallidos.append(tel)
            print(f"[cierre] fallo enviando a {tel}: {ex}")
    _avisar_no_entregados(fallidos, entregados)
    return len(entregados)


def resumen_delegado(dia: date | None = None) -> dict:
    """Manda, al cierre, lo que el bot aprobó SIN preguntar (back mig 221).

    Va por su propio camino y no colgado del reporte de cierre a propósito: ese reporte hoy está
    apagado (`REPORTE_EOD_AUTO=false`), así que colgarlo de ahí sería construir algo que nunca
    llega. Y son cosas distintas — uno es cómo fue el negocio, éste es qué se hizo en tu nombre
    sin consultarte.

    MISMA REGLA: si el bot no aprobó nada por su cuenta, NO se manda. El silencio significa que
    nadie actuó en tu nombre, que es justo lo que uno quiere saber sin tener que leer.
    """
    resultado = {}
    for c in listar_consorcios():
        full = consorcio_por_id(c.id)
        if not full:
            continue
        # NO se usa `_safe` acá: ése se traga el error y devuelve {}, y un {} se leería como
        # "no se aprobó nada". En un resumen de rendición de cuentas eso es la peor falla posible:
        # el día que el endpoint se rompa, el silencio diría "nadie actuó en tu nombre" cuando en
        # realidad nadie miró. Un fallo de LECTURA no es lo mismo que NADA QUE CONTAR.
        try:
            params = {"dia": dia.isoformat()} if dia else None
            r = reportes._get(full, "/api/crm/asistente/aprobaciones", params) or {}
        except Exception as ex:  # noqa: BLE001
            print(f"[delegado] NO PUDE LEER las aprobaciones de {c.nombre}: {ex}")
            resultado[c.nombre] = f"ERROR de lectura: {ex}"
            continue

        filas = [x for x in (r.get("data") or []) if not x.get("revertido_en")]
        if not filas:
            resultado[c.nombre] = "sin aprobaciones automáticas"
            continue

        res = r.get("resumen") or {}
        lineas = [f"🤖 *Aprobado sin preguntarte hoy* — {len(filas)} operación(es)"]
        if res.get("monto_total") and int(res["monto_total"]) > 0:
            lineas.append(f"Total: RD${int(res['monto_total']):,}".replace(",", "."))
        lineas.append("")
        for x in filas[:20]:
            hora = str(x.get("creado_en") or "")[11:16]
            monto = f" · RD${int(x['monto']):,}".replace(",", ".") if x.get("monto") else ""
            quien = x.get("empleado_nombre") or "—"
            banca = f" · {x['banca_nombre']}" if x.get("banca_nombre") else ""
            lineas.append(f"· {hora} {x.get('accion')}{monto} — {quien}{banca}")
        if len(filas) > 20:
            lineas.append(f"…y {len(filas) - 20} más.")
        if res.get("revertidas"):
            lineas.append(f"\n({res['revertidas']} ya revertida(s))")
        lineas.append("\nSi algo no cuadra, se revierte desde el CRM en "
                      "Asistente IA → Aprobación delegada.")
        texto = "\n".join(lineas)

        enviados = 0
        for tel in reportes.destinatarios(c.id):
            try:
                enviar_por_puente(tel, texto, None, None)
                enviados += 1
            except Exception as ex:  # noqa: BLE001
                print(f"[delegado] fallo enviando a {tel}: {ex}")
        resultado[c.nombre] = f"{len(filas)} aprobaciones → {enviados} envío(s)"
    return resultado


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
    """Programa el sondeo del cierre y el resumen de lo aprobado sin preguntar.

    Los dos van por separado y con interruptores distintos: el reporte de cierre habla del NEGOCIO
    y hoy está apagado; el resumen de lo delegado habla de lo que se hizo EN TU NOMBRE sin
    consultarte, y eso no debería depender de que el otro esté prendido."""
    global _sched
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except Exception as ex:  # noqa: BLE001
        print(f"[scheduler] APScheduler no disponible ({ex}); no se programa nada", flush=True)
        return
    _sched = BackgroundScheduler(timezone=config.REPORTE_TZ)

    if config.RESUMEN_DELEGADO_AUTO:
        hh, mm = (config.RESUMEN_DELEGADO_HORA.split(":") + ["0"])[:2]
        _sched.add_job(resumen_delegado, "cron", hour=int(hh), minute=int(mm),
                       id="resumen_delegado")
        print(f"[scheduler] resumen de lo aprobado sin preguntar: {config.RESUMEN_DELEGADO_HORA} "
              f"(sólo si hubo algo).", flush=True)

    if not config.REPORTE_EOD_AUTO:
        print("[scheduler] envío automático de cierre APAGADO (REPORTE_EOD_AUTO=false).", flush=True)
        _sched.start()
        return
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
