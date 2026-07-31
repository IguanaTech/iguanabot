"""Config del asistente — lee del entorno (.env vía docker compose)."""
import os


def _req(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Falta la variable de entorno {name}")
    return v


class Config:
    # LLM — proveedor conmutable (diseño agnóstico: cambiar de modelo = una línea).
    #   anthropic → Claude Haiku 4.5 (recomendado para producción: mejor tool-calling + soberanía).
    #   deepseek  → DeepSeek-V3 vía su API OpenAI-compatible (barato, ideal para PROBAR ya).
    LLM_PROVIDER = os.getenv("LLM_PROVIDER", "anthropic").lower()

    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
    LLM_MODEL = os.getenv("LLM_MODEL", "claude-haiku-4-5")
    LLM_MODEL_ESCALADA = os.getenv("LLM_MODEL_ESCALADA", "claude-sonnet-5")
    # Escala #1: ventana deslizante de mensajes que ve el LLM (poda del historial). El hilo COMPLETO
    # sigue en el checkpoint; esto solo acota lo que se le manda a Claude (costo/latencia/contexto).
    # 24 ≈ 12 turnos — suficiente para el contexto reciente sin reenviar toda la conversación.
    CONVERSACION_MAX_MSGS = int(os.getenv("CONVERSACION_MAX_MSGS", "24"))
    # Escala #2: TTL de la caché de identidad (teléfono→consorcio). Evita re-preguntarle a TODOS los
    # backs en cada mensaje. Corto para que un alta/cambio de rol en el CRM se refleje pronto.
    IDENTIDAD_CACHE_TTL_SEG = int(os.getenv("IDENTIDAD_CACHE_TTL_SEG", "300"))
    # Escala #3: retención de memoria. Sin esto la memoria crece para siempre (un checkpoint por
    # usuario que nunca se limpia + la bitácora write-only). Borra hilos INACTIVOS (último checkpoint
    # más viejo que N días) y purga bitácora vieja. Corre 1×/día.
    CHECKPOINT_RETENCION_DIAS = int(os.getenv("CHECKPOINT_RETENCION_DIAS", "30"))
    BITACORA_RETENCION_DIAS = int(os.getenv("BITACORA_RETENCION_DIAS", "90"))

    # Memoria SEMÁNTICA (hechos durables por persona, recuperados por parecido). El modelo de
    # embeddings corre LOCAL —igual que Whisper y Piper— para no sumar otro proveedor ni otra clave
    # sólo por vectorizar frases; Anthropic no ofrece embeddings. MiniLM multilingüe: 384 dims,
    # 220 MB, entiende español. Cambiarlo por otro de fastembed re-provisiona la tabla con la
    # dimensión nueva (memoria.preparar() la pregunta al modelo, no la asume).
    EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL",
                                "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
    MEMORIA_SEMANTICA = os.getenv("MEMORIA_SEMANTICA", "true").lower() == "true"
    # Retención de los recuerdos. Más largo que el hilo de conversación (30 días) porque son
    # justamente las cosas que deben sobrevivir al hilo; pero no eternas: una preferencia de hace
    # un año probablemente ya no aplica y nadie se acuerda de borrarla.
    MEMORIA_RETENCION_DIAS = int(os.getenv("MEMORIA_RETENCION_DIAS", "365"))

    DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
    DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")  # V3, soporta tool-calling

    # DB propia del asistente
    DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://asistente:asistente@pgvector:5432/asistente")
    SECRET_KEY = os.getenv("ASISTENTE_SECRET_KEY", "")

    # Puente de WhatsApp
    BRIDGE_URL = os.getenv("BRIDGE_URL", "http://bridge:3100")
    # Puente de TELEGRAM (canal paralelo, mismo cerebro). Vacío = canal apagado. El token lo da
    # @BotFather. Telegram es la API OFICIAL para bots: no hay verificación de empresa, no hay
    # plantillas aprobadas para los mensajes que inicia el bot, y no hay riesgo de que restrinjan
    # la cuenta — que es lo que le pasó al canal de WhatsApp (error 463 con el cliente no oficial).
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_BRIDGE_URL = os.getenv("TELEGRAM_BRIDGE_URL", "http://telegram:3200")

    # Voz
    VOZ_HABILITADA = os.getenv("VOZ_HABILITADA", "true").lower() == "true"
    WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small")
    TTS_MOTOR = os.getenv("TTS_MOTOR", "piper")  # piper (self-host) | polly | elevenlabs (neural, cuenta)
    # Voz FEMENINA es_MX de alta calidad (self-host, offline). Para una voz neural casi humana,
    # cambiar TTS_MOTOR a polly (Mia) o elevenlabs — el código ya está en voice.py.
    PIPER_MODEL = os.getenv("PIPER_MODEL", "/voces/es_MX-claude-high.onnx")

    # Resumen de lo que el bot APROBÓ SIN PREGUNTAR (back mig 221). Va aparte del reporte de cierre
    # a propósito: aquél habla del negocio y hoy está apagado; éste habla de lo que se hizo en tu
    # nombre sin consultarte, y no debería depender de que el otro esté prendido.
    # SÓLO SE MANDA SI HUBO ALGO: un mensaje diario que a veces dice "no pasó nada" hace que dejen
    # de abrirlo, y el día que trae algo importante tampoco se abre.
    RESUMEN_DELEGADO_AUTO = os.getenv("RESUMEN_DELEGADO_AUTO", "true").lower() == "true"
    RESUMEN_DELEGADO_HORA = os.getenv("RESUMEN_DELEGADO_HORA", "21:00")

    # Reportes
    REPORTE_PDF = os.getenv("REPORTE_PDF", "true").lower() == "true"      # adjuntar PDF además del texto
    REPORTE_TZ = os.getenv("REPORTE_TZ", "America/Santo_Domingo")
    # Envío automático (proactivo) al cierre. En Fase 0/Baileys, dejar en false para pocos/pruebas;
    # el envío masivo bot-inicia va con la Cloud API oficial + plantillas (Fase 1) para no arriesgar baneo.
    REPORTE_EOD_AUTO = os.getenv("REPORTE_EOD_AUTO", "false").lower() == "true"
    # Disparo del reporte de cierre: NO a hora fija (una lotería puede cerrar más tarde). El bot
    # SONDEA el estado del día operativo del back y manda el reporte cuando la ÚLTIMA lotería del día
    # ya publicó números (premios) + pasó el margen del consorcio → back marca el día como cerrado.
    # Se sondea todo el día (barato) y se dispara apenas cierra; no hay franja horaria de arranque.
    REPORTE_EOD_POLL_MIN = int(os.getenv("REPORTE_EOD_POLL_MIN", "10"))    # cada cuánto sondea (min)
    # Red de seguridad: si el operador NUNCA cargó la última lotería, el día no cierra y el reporte no
    # saldría. A esta hora local, si aún no salió, se manda igual marcado como PRELIMINAR (el día no
    # cerró formalmente). Poner "" para desactivar la red y esperar SIEMPRE el cierre real.
    REPORTE_EOD_TOPE_HORA = os.getenv("REPORTE_EOD_TOPE_HORA", "23:30")

    # Alarmas de dinero (vigilante proactivo)
    # Avisar a los admin/encargado cuando: (a) una banca queda DESCUBIERTA de un premio (no lo puede
    # pagar con su caja) y (b) un NÚMERO CALIENTE (misma jugada en muchas bancas = dato filtrado).
    # A pocos admins de un consorcio es de bajo volumen (más seguro en Baileys que el masivo del cierre),
    # pero sigue siendo bot-inicia → a escala grande, migrar a Cloud API. Por eso es prendible/apagable.
    ALARMAS_PUSH = os.getenv("ALARMAS_PUSH", "true").lower() == "true"
    ALARMAS_INTERVALO_SEG = int(os.getenv("ALARMAS_INTERVALO_SEG", "90"))
    ALARMAS_COOLDOWN_MIN = int(os.getenv("ALARMAS_COOLDOWN_MIN", "30"))  # no repetir la misma alarma
    VELOCIDAD_VENTANA_MIN = int(os.getenv("VELOCIDAD_VENTANA_MIN", "60"))
    # Ganador REAL alto: un ticket que YA ganó un premio >= a esto → avisar (RD$ enteros).
    UMBRAL_GANADOR = int(os.getenv("UMBRAL_GANADOR", "20000"))
    # A quién se le empujan las alarmas de dinero: TELÉFONOS (no LID — el LID no sirve para enviar).
    # Lista separada por comas. Fase 0: los admins que quieren recibirlas. (Luego: leerlos del back.)
    ALARMAS_DESTINATARIOS = [t.strip() for t in os.getenv("ALARMAS_DESTINATARIOS", "").split(",") if t.strip()]

    # Recordatorio DIARIO de dinero-vivo estancado (mensajero con efectivo hace días / banca con
    # tickets sin recoger). Lee /dashboard/operativo (el back ya quita las gestionadas en el CRM) y
    # recuerda UNA vez al día HASTA que se resuelva. Dedup persistido en la DB (sobrevive reinicios).
    # Independiente de ALARMAS_PUSH (es el P0 que pidió Eduardo). Se apaga con RECORDATORIO_DINERO=false.
    RECORDATORIO_DINERO = os.getenv("RECORDATORIO_DINERO", "true").lower() == "true"
    RECORDATORIO_DINERO_HORA = int(os.getenv("RECORDATORIO_DINERO_HORA", "9"))  # primer tick tras esta hora local

    # Seguridad
    AVISAR_DESCONOCIDOS = os.getenv("AVISAR_DESCONOCIDOS", "true").lower() == "true"


config = Config()
