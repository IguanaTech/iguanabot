"""Servicio del asistente (FastAPI). El puente de WhatsApp le POSTea cada mensaje entrante y
recibe la respuesta (texto y/o audio) para mandarla de vuelta al cliente.
"""
import base64
from datetime import date

import psycopg
from fastapi import FastAPI
from pydantic import BaseModel

from .config import config
from .graph import responder, preparar_memoria
from .identity import BackInalcanzable, consorcio_de, resolver, sincronizar_todos
from . import reportes, scheduler, voice, watcher

app = FastAPI(title="iguana-asistente")


@app.on_event("startup")
def _startup() -> None:
    # Memoria de conversación durable (PostgresSaver): crea las tablas checkpoint* si faltan, para no
    # pagar el setup() en la primera consulta y para que el hilo de cada usuario sobreviva reinicios.
    try:
        preparar_memoria()
    except Exception as ex:  # noqa: BLE001
        print(f"[startup] memoria avisó: {ex}")
    # Sincroniza el directorio desde los backs registrados (best-effort).
    try:
        print(f"[startup] roster: {sincronizar_todos()}")
    except Exception as ex:  # noqa: BLE001
        print(f"[startup] sync roster avisó: {ex}")
    # Programa el envío automático del reporte al cierre (si está activo).
    scheduler.arrancar()
    # Arranca el vigilante de alarmas de dinero (premio sin cobertura + número caliente).
    watcher.arrancar()


class MensajeEntrante(BaseModel):
    telefono: str                 # wa_id del que escribe
    texto: str | None = None      # si mandó texto
    audio_base64: str | None = None  # si mandó nota de voz (ogg/opus)


class RespuestaSaliente(BaseModel):
    texto: str
    audio_base64: str | None = None      # nota de voz de respuesta (ogg/opus), si aplica
    documento_base64: str | None = None  # PDF del reporte, si el cliente lo pidió
    documento_nombre: str | None = None


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
    # 1) Identidad. Desconocido = no se le sirve nada; back caído = avisar que es transitorio (para no
    #    hacerle creer a un usuario legítimo que le revocaron el acceso durante un hipo del back).
    try:
        ident = resolver(m.telefono)
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
    if m.audio_base64 and config.VOZ_HABILITADA:
        era_voz = True
        texto = voice.transcribir(base64.b64decode(m.audio_base64))
    else:
        texto = m.texto or ""
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
