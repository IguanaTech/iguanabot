"""Puente de TELEGRAM — transporte puro, hermano del puente de WhatsApp.

Un cerebro, dos puentes. Este proceso no sabe nada de consorcios, roles ni permisos: recibe lo que
llega de Telegram, se lo pasa al asistente y devuelve lo que el asistente conteste. TODA decisión
de identidad vive del lado del asistente (ver `asistente/telegram_vinculo.py`) — si este archivo
empezara a decidir quién puede hablar, habría dos modelos de permisos en paralelo y uno de los dos
terminaría equivocándose.

POR QUÉ LONG POLLING Y NO WEBHOOK: un webhook obliga a exponer un endpoint HTTPS público en el
VPS. `getUpdates` es una conexión SALIENTE, así que no se abre ni un puerto nuevo — menos
superficie que la que ya hay, y sin certificados ni dominio que mantener.

Paridad real con WhatsApp: texto, notas de voz (Telegram las manda en OGG/Opus, que es justo lo
que Whisper ya consume) y PDF. No es un canal recortado "para probar".
"""
import base64
import os
import threading
import time

import httpx
from fastapi import FastAPI
from pydantic import BaseModel

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ASISTENTE_URL = os.getenv("ASISTENTE_URL", "http://asistente:8000")
API = f"https://api.telegram.org/bot{TOKEN}"
ARCHIVOS = f"https://api.telegram.org/file/bot{TOKEN}"

# El teclado de una sola tecla que pide el contacto. `request_contact` hace que el número lo
# ponga TELEGRAM desde la cuenta: la persona no lo teclea, así que no puede escribir uno ajeno.
TECLADO_CONTACTO = {
    "keyboard": [[{"text": "📱 Compartir mi contacto", "request_contact": True}]],
    "resize_keyboard": True,
    "one_time_keyboard": True,
}

app = FastAPI(title="puente-telegram")
_estado = {"ultimo_update": 0, "conectado": False, "error": None}


# ── Salida hacia Telegram ────────────────────────────────────────────────────

def _api(metodo: str, payload: dict, archivos: dict | None = None) -> dict | None:
    try:
        with httpx.Client(timeout=60) as c:
            r = (c.post(f"{API}/{metodo}", data=payload, files=archivos) if archivos
                 else c.post(f"{API}/{metodo}", json=payload))
            j = r.json()
        if not j.get("ok"):
            print(f"[telegram] {metodo} rechazado: {j.get('description')}")
            return None
        return j.get("result")
    except Exception as ex:  # noqa: BLE001
        print(f"[telegram] {metodo} falló: {ex}")
        return None


def responder_al_chat(chat_id: int, r: dict) -> None:
    """Entrega la respuesta del asistente: texto, y si vinieron, voz y documento.

    El orden importa: primero el texto. Si la voz o el PDF fallan (formato, tamaño, red), la
    persona igual recibió la respuesta — al revés se quedaría esperando algo que nunca llega.
    """
    texto = (r.get("texto") or "").strip()
    if texto:
        payload = {"chat_id": chat_id, "text": texto}
        if r.get("pedir_contacto"):
            payload["reply_markup"] = TECLADO_CONTACTO
        _api("sendMessage", payload)

    if r.get("audio_base64"):
        try:
            _api("sendVoice", {"chat_id": str(chat_id)},
                 archivos={"voice": ("respuesta.ogg", base64.b64decode(r["audio_base64"]),
                                     "audio/ogg")})
        except Exception as ex:  # noqa: BLE001
            print(f"[telegram] no se pudo mandar la nota de voz: {ex}")

    if r.get("documento_base64"):
        try:
            _api("sendDocument", {"chat_id": str(chat_id)},
                 archivos={"document": (r.get("documento_nombre") or "reporte.pdf",
                                        base64.b64decode(r["documento_base64"]),
                                        "application/pdf")})
        except Exception as ex:  # noqa: BLE001
            print(f"[telegram] no se pudo mandar el documento: {ex}")


# ── Entrada desde Telegram ───────────────────────────────────────────────────

def _bajar_voz(file_id: str) -> str | None:
    """Descarga la nota de voz y la devuelve en base64 (OGG/Opus, tal cual la manda Telegram)."""
    meta = _api("getFile", {"file_id": file_id})
    if not meta or not meta.get("file_path"):
        return None
    try:
        with httpx.Client(timeout=60) as c:
            r = c.get(f"{ARCHIVOS}/{meta['file_path']}")
            r.raise_for_status()
        return base64.b64encode(r.content).decode()
    except Exception as ex:  # noqa: BLE001
        print(f"[telegram] no se pudo bajar el audio: {ex}")
        return None


def _atender(msg: dict) -> None:
    chat_id = msg.get("chat", {}).get("id")
    user_id = msg.get("from", {}).get("id")
    if chat_id is None or user_id is None:
        return

    payload = {"chat_id": chat_id, "user_id": user_id}

    contacto = msg.get("contact")
    if contacto:
        # Se manda el user_id del contacto SIN filtrar: la verificación de que sea el contacto
        # propio la hace el asistente. Acá no se decide nada de identidad, a propósito.
        payload["contacto_telefono"] = contacto.get("phone_number")
        payload["contacto_user_id"] = contacto.get("user_id")
    elif msg.get("voice") or msg.get("audio"):
        media = msg.get("voice") or msg.get("audio")
        payload["audio_base64"] = _bajar_voz(media.get("file_id"))
        if not payload["audio_base64"]:
            _api("sendMessage", {"chat_id": chat_id,
                                 "text": "No pude bajar tu nota de voz. ¿Me la escribes?"})
            return
    else:
        texto = (msg.get("text") or msg.get("caption") or "").strip()
        if not texto:
            return
        # /start es el saludo de Telegram, no una pregunta para el agente.
        payload["texto"] = "hola" if texto.startswith("/start") else texto

    try:
        with httpx.Client(timeout=180) as c:
            r = c.post(f"{ASISTENTE_URL}/mensaje-telegram", json=payload)
            r.raise_for_status()
            respuesta = r.json()
    except Exception as ex:  # noqa: BLE001
        print(f"[telegram] el asistente no respondió: {ex}")
        _api("sendMessage", {"chat_id": chat_id,
                             "text": "Se me complicó procesar eso. Intenta de nuevo."})
        return

    responder_al_chat(chat_id, respuesta)


def _bucle() -> None:
    """Long polling. Cada update se atiende en su propio hilo: una consulta pesada (el agente puede
    tardar) no debe frenar la cola — si no, un mensaje lento bloquea a todos los demás usuarios."""
    if not TOKEN:
        print("[telegram] sin TELEGRAM_BOT_TOKEN: el canal queda apagado.")
        return
    yo = _api("getMe", {})
    if not yo:
        _estado["error"] = "getMe falló (¿token inválido?)"
        print("[telegram] no pude autenticarme con Telegram; revisá el token.")
        return
    _estado["conectado"] = True
    print(f"[telegram] conectado como @{yo.get('username')}.")

    offset = 0
    while True:
        try:
            # timeout=50 es el long poll de Telegram: la petición queda abierta hasta que haya algo
            # o pasen 50s. El timeout del cliente va MÁS ARRIBA (60) o cortaría cada espera normal
            # y se reconectaría en vano.
            with httpx.Client(timeout=60) as c:
                r = c.get(f"{API}/getUpdates",
                          params={"offset": offset, "timeout": 50,
                                  "allowed_updates": '["message"]'})
                j = r.json()
            if not j.get("ok"):
                print(f"[telegram] getUpdates rechazado: {j.get('description')}")
                time.sleep(5)
                continue
            for up in j.get("result", []):
                offset = up["update_id"] + 1
                _estado["ultimo_update"] = up["update_id"]
                msg = up.get("message")
                if msg:
                    threading.Thread(target=_atender, args=(msg,), daemon=True).start()
        except Exception as ex:  # noqa: BLE001
            # Un corte de red no debe matar el puente: se espera y se reintenta. Sin esto, un hipo
            # de conexión dejaría el canal mudo hasta que alguien reinicie el contenedor.
            print(f"[telegram] bucle: {ex}")
            time.sleep(5)


# ── API local (la usa el asistente para los mensajes que INICIA el bot) ──────

class Saliente(BaseModel):
    chat_id: int
    texto: str
    documento_base64: str | None = None
    documento_nombre: str | None = None


@app.post("/enviar")
def enviar(s: Saliente) -> dict:
    responder_al_chat(s.chat_id, {
        "texto": s.texto,
        "documento_base64": s.documento_base64,
        "documento_nombre": s.documento_nombre,
    })
    return {"ok": True}


@app.get("/salud")
def salud() -> dict:
    return {"ok": _estado["conectado"], **_estado}


@app.on_event("startup")
def _arrancar() -> None:
    threading.Thread(target=_bucle, daemon=True).start()
