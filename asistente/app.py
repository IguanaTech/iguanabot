"""Servicio del asistente (FastAPI). El puente de WhatsApp le POSTea cada mensaje entrante y
recibe la respuesta (texto y/o audio) para mandarla de vuelta al cliente.
"""
import base64
from datetime import date

import psycopg
from fastapi import FastAPI
from pydantic import BaseModel

from . import memoria
from .config import config
from .graph import responder, preparar_memoria
from . import telegram_vinculo
from .identity import BackInalcanzable, consorcio_de, resolver, sincronizar_todos
from . import heartbeat, reportes, retencion, scheduler, voice, watcher

app = FastAPI(title="iguana-asistente")


@app.on_event("startup")
def _startup() -> None:
    # Memoria de conversación durable (PostgresSaver): crea las tablas checkpoint* si faltan, para no
    # pagar el setup() en la primera consulta y para que el hilo de cada usuario sobreviva reinicios.
    try:
        preparar_memoria()
    except Exception as ex:  # noqa: BLE001
        print(f"[startup] memoria avisó: {ex}")
    # Memoria SEMÁNTICA (hechos durables por persona): provisiona la tabla con la dimensión del
    # modelo vigente y calienta el modelo acá, no en la primera consulta — cargar el ONNX tarda y
    # el primer mensaje del día no tiene por qué pagarlo. Si algo falla, la memoria queda apagada
    # y el bot contesta igual.
    if config.MEMORIA_SEMANTICA:
        try:
            memoria.preparar()
        except Exception as ex:  # noqa: BLE001
            print(f"[startup] memoria semántica avisó: {ex}")
    # Vínculo chat de Telegram ↔ teléfono (traductor de identidad del canal, no autoridad).
    try:
        telegram_vinculo.preparar()
    except Exception as ex:  # noqa: BLE001
        print(f"[startup] vínculo de Telegram avisó: {ex}")
    # Sincroniza el directorio desde los backs registrados (best-effort).
    try:
        print(f"[startup] roster: {sincronizar_todos()}")
    except Exception as ex:  # noqa: BLE001
        print(f"[startup] sync roster avisó: {ex}")
    # Programa el envío automático del reporte al cierre (si está activo).
    scheduler.arrancar()
    # Arranca el vigilante de alarmas de dinero (premio sin cobertura + número caliente).
    watcher.arrancar()
    # Retención de memoria (escala #3): borra hilos inactivos + purga bitácora vieja, 1×/día.
    retencion.arrancar()
    # Watchdog: reporta al back si el WhatsApp del bot está conectado → el CRM avisa si se cae.
    heartbeat.arrancar()


class MensajeEntrante(BaseModel):
    telefono: str                 # wa_id del que escribe
    texto: str | None = None      # si mandó texto
    audio_base64: str | None = None  # si mandó nota de voz (ogg/opus)


class MensajeTelegram(BaseModel):
    """Lo que manda el puente de Telegram. NO trae teléfono: trae el chat y quién escribe, y el
    asistente lo traduce (ver telegram_vinculo). El puente es transporte puro — ninguna decisión
    de identidad vive del lado de Node/Telegram."""
    chat_id: int
    user_id: int                       # quién escribe (para verificar el contacto compartido)
    texto: str | None = None
    audio_base64: str | None = None
    contacto_telefono: str | None = None   # si tocó "compartir mi contacto"
    contacto_user_id: int | None = None    # dueño de ese contacto — TIENE que ser user_id


class RespuestaSaliente(BaseModel):
    texto: str
    audio_base64: str | None = None      # nota de voz de respuesta (ogg/opus), si aplica
    documento_base64: str | None = None  # PDF del reporte, si el cliente lo pidió
    documento_nombre: str | None = None
    # Le dice al puente de Telegram que muestre el botón de compartir contacto. Los demás canales
    # lo ignoran (en WhatsApp el teléfono viene con el mensaje).
    pedir_contacto: bool = False


def _bitacora(telefono, consorcio_id, entrada, respuesta) -> None:
    try:
        with psycopg.connect(config.DATABASE_URL) as conn:
            conn.execute(
                "INSERT INTO bitacora (telefono, consorcio_id, entrada, respuesta) VALUES (%s,%s,%s,%s)",
                (telefono, consorcio_id, entrada, respuesta),
            )
            conn.commit()
    except Exception as ex:  # noqa: BLE001 — la bitácora no debe tumbar la respuesta
        print(f"[bitacora] {ex}")


@app.post("/mensaje", response_model=RespuestaSaliente)
def mensaje(m: MensajeEntrante) -> RespuestaSaliente:
    """Canal WhatsApp: el teléfono viene en el mensaje."""
    return _atender(m.telefono, m.texto, m.audio_base64)


@app.post("/mensaje-telegram", response_model=RespuestaSaliente)
def mensaje_telegram(m: MensajeTelegram) -> RespuestaSaliente:
    """Canal Telegram: el teléfono NO viene; se resuelve por el vínculo del chat.

    Un canal más, el MISMO cerebro: apenas se traduce chat→teléfono se cae en `_atender`, que es
    literalmente el mismo camino que WhatsApp (identidad contra el back, alcance, voz, PDF). Si
    esto duplicara el flujo, las dos copias empezarían a diferir y una de las dos tendría el
    agujero."""
    # ¿Está compartiendo su contacto? Es el único momento en que se crea el vínculo.
    if m.contacto_telefono:
        tel = telegram_vinculo.vincular(m.chat_id, m.user_id, m.contacto_user_id, m.contacto_telefono)
        if not tel:
            return RespuestaSaliente(
                texto="Ese contacto no es el tuyo. Usa el botón para compartir TU contacto.",
                pedir_contacto=True)
        # Vinculado: se sigue de largo y se le contesta algo útil, no un "listo" a secas.
        return _atender(tel, m.texto or "hola", None)

    tel = telegram_vinculo.telefono_de(m.chat_id)
    if not tel:
        return RespuestaSaliente(
            texto="Para identificarte necesito tu número. Toca el botón de abajo para compartir tu "
                  "contacto — es de una sola vez.",
            pedir_contacto=True)
    return _atender(tel, m.texto, m.audio_base64)


def _atender(telefono: str, texto_in: str | None, audio_base64: str | None) -> RespuestaSaliente:
    """El camino común de TODOS los canales: identidad → voz → agente → PDF → respuesta."""
    # 1) Identidad. Desconocido = no se le sirve nada; back caído = avisar que es transitorio (para no
    #    hacerle creer a un usuario legítimo que le revocaron el acceso durante un hipo del back).
    try:
        ident = resolver(telefono)
    except BackInalcanzable:
        return RespuestaSaliente(
            texto="No puedo verificar tu identidad ahora mismo (problema de conexión con el sistema). "
                  "Intenta de nuevo en un momento.")
    if not ident:
        if config.AVISAR_DESCONOCIDOS:
            return RespuestaSaliente(texto="No te reconozco en el sistema. Habla con tu administrador.")
        return RespuestaSaliente(texto="")

    consorcio = consorcio_de(ident)
    if not consorcio:
        return RespuestaSaliente(texto="No pude ubicar tu consorcio. Avisa a soporte.")

    # 2) Si vino voz, transcribir. Recordamos si fue voz para contestar en voz.
    era_voz = False
    if audio_base64 and config.VOZ_HABILITADA:
        era_voz = True
        texto = voice.transcribir(base64.b64decode(audio_base64))
    else:
        texto = texto_in or ""
    if not texto.strip():
        return RespuestaSaliente(texto="No entendí el mensaje. ¿Me lo repites?")

    # 3) Correr el agente.
    hubo_error = False
    try:
        respuesta = responder(ident, consorcio, texto)
    except Exception as ex:  # noqa: BLE001
        print(f"[agente] {ex}")
        hubo_error = True
        respuesta = "Se me complicó consultar eso ahora mismo. Intenta de nuevo en un momento."

    _bitacora(ident.telefono, ident.consorcio_id, texto, respuesta)

    # 4) PDF adjunto: (a) si el agente armó el reporte del día (herramienta reporte_del_dia), o
    #    (b) si el cliente pidió la respuesta EN PDF ("dame un pdf de…") → PDF de lo que contestó.
    #    Si el agente falló, NO se arma un PDF del mensaje de error (sería un documento inútil).
    doc_b64 = doc_nombre = None
    pend = reportes.tomar_pendiente(ident.telefono)
    if pend:
        doc_nombre, pdf_bytes = pend
        doc_b64 = base64.b64encode(pdf_bytes).decode()
    elif config.REPORTE_PDF and not hubo_error and voice.pidio_pdf(texto):
        try:
            pdf_bytes = reportes.pdf_de_texto("Consulta al asistente", respuesta)
            if pdf_bytes:
                doc_nombre = f"consulta-{date.today().isoformat()}.pdf"
                doc_b64 = base64.b64encode(pdf_bytes).decode()
        except Exception as ex:  # noqa: BLE001
            print(f"[pdf] {ex}")

    # 5) Contestamos en VOZ cuando el cliente lo pide ("mándame en nota de voz", "dímelo hablado"…),
    #    haya escrito por texto o por voz. On-demand: no todo mensaje se contesta en audio.
    audio_out = None
    if config.VOZ_HABILITADA and voice.pidio_voz(texto):
        try:
            audio_out = base64.b64encode(voice.sintetizar(voice.limpiar_para_voz(respuesta))).decode()
        except Exception as ex:  # noqa: BLE001
            print(f"[tts] {ex}")

    return RespuestaSaliente(
        texto=respuesta, audio_base64=audio_out,
        documento_base64=doc_b64, documento_nombre=doc_nombre,
    )


@app.get("/salud")
def salud() -> dict:
    return {"ok": True}
