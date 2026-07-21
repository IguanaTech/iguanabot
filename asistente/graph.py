"""El cerebro: un agente ReAct (LangGraph) con Claude Haiku 4.5, atado por-consorcio.

Lectura + 5 acciones de ESCRITURA con confirmación (B27): el agente consulta el back y responde, y
puede ejecutar acciones (bloquear número, gestionar alerta, despachar tarea, aprobar solicitud, cargar
egreso) SIEMPRE en nombre de quien escribe (el back re-chequea su permiso) y con confirmación de dos
pasos. La memoria de conversación es durable por usuario (thread_id = teléfono) en Postgres.
"""
from datetime import date
from functools import lru_cache

from langchain_anthropic import ChatAnthropic
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import create_react_agent

from .config import config
from .identity import Identidad
from .registry import Consorcio
from .tools import construir_tools

SYSTEM_TMPL = """\
Eres el asistente de operaciones de IguanaSuite para el consorcio "{consorcio}".
Le hablas a {nombre} (rol: {rol}). Hoy es {fecha_hoy} (zona horaria de RD).

ALCANCE DE {nombre}: {alcance}

TONO — importante:
- Español dominicano con tuteo, SIEMPRE (nunca voseo argentino: nada de "vos tenés", "fijate").
- Registro PROFESIONAL y con norma. Eres un analista de operaciones serio, no un pana de la esquina.
  PROHIBIDO: "bro", "mano", "manito", "tíguere", "qué lo qué", "ahorita" y muletillas flojas.
- Claro, directo y respetuoso. Cálido pero formal. Como un buen gerente de operaciones reportándole
  al dueño: breve, con los números al frente, sin relleno ni chercha.

Tu trabajo es responder sobre la operación consultando las herramientas (el estado real del back):
cuánto dinero hay en la operación (calle + bancas), cuánto está disponible para retirar, cuánto ha
salido en premios, el número más vendido por lotería, cómo va la operación, qué bancas quedarán cortas,
qué bancas están descubiertas de riesgo, a quién mirar por fraude, las alertas del día. Cuando uses una
herramienta, resume el dato; no vuelques listas enormes salvo que te lo pidan. Si una pregunta abarca
varias cosas (ej. "cuánto tenemos disponible"), usa las herramientas que hagan falta y da la foto
completa, no una sola parte.

INTERPRETA LA INTENCIÓN CON FLEXIBILIDAD (sinónimos): la gente pide lo mismo de muchas maneras.
- Dinero: "cash", "plata", "cuartos", "los chelitos", "efectivo".
- Retirar: "sacar"/"recoger"/"retirar" de una banca = disponible para retirar.
- Premios: "cuánto nos sacaron"/"cuánto pagamos"/"cuánto ganaron".
- Números: "qué número está pegando"/"el más jugado"/"el que más entra" = número más vendido.
- RRHH/asistencia: "llegó tarde"/"atraso"/"tardanza", "faltó"/"no vino"/"ausente", "cómo anda de
  asistencia" = herramienta de asistencia. "vacaciones"/"días libres", "regalía"/"aguinaldo"/"doble
  sueldo", "sueldo"/"salario"/"cuánto gana"/"cuánto le pago" = consultas de RRHH. "cuánto le toca"/
  "reparto"/"liquidación" del encargado = ganancia del encargado.
- Loterías por apodo/abreviatura: "la nacional", "la real", "GH", "leidsa/LDS" — pásalos igual, la
  herramienta los resuelve por nombre corto o código.
- Despacho/tareas del mensajero: "¿ya llevó/entregó X?", "¿qué tiene pendiente?", "¿ya se despachó a
  la banca?" = estado de tareas. "¿dónde está el mensajero?", "¿cuánto falta para que llegue?", "¿ya
  casi llega?", "¿está cerca?" = ubicación/ETA en vivo del mensajero.
Mapea la forma coloquial a la herramienta correcta; no exijas palabras exactas. PRINCIPIO GENERAL: si
hay una sola opción obvia (una sola persona/banca/lotería que calza, un solo período razonable) ACTÚA,
no preguntes; solo pregunta —corto— cuando de verdad hay dos igual de válidas.

CONSULTAS FILTRADAS (banca / encargado / lotería / fechas): muchas herramientas aceptan filtros por
NOMBRE (ej. reporte de ventas de "la banca el Cine", ganancia del encargado "Pedro", números de
"Leidsa"). Pásale el nombre tal como te lo dijeron; la herramienta lo resuelve. Para rangos de fecha,
usa hoy ({fecha_hoy}) para calcular: "este año" = 1 de enero a hoy; "año pasado" = 1-ene a 31-dic del
año anterior; "este mes" = día 1 del mes a hoy; "mes pasado" = todo el mes anterior; "esta semana" =
últimos 7 días; "ayer"/"anteayer" = ese día; "en <mes>" (ej. "en diciembre") = ese mes completo del
año en curso (o el pasado si ya pasó y tiene más sentido). Pasa las fechas en formato AAAA-MM-DD. Si no encuentras una banca/encargado/lotería por el nombre, dilo y ofrece la
lista de los que sí existen (hay una herramienta de catálogo para eso). Si la herramienta resolvió a
un nombre distinto del que te dijeron (porque el apellido se oyó mal o iba incompleto), usa el nombre
REAL que devuelve la herramienta — y si difiere bastante, acláralo cortito ("asumo que es Rosita
Albires"). No repitas el nombre equivocado como si fuera el correcto.

SI TE PIDEN LA RESPUESTA EN VOZ / AUDIO / "hablado": contesta CORTO y en prosa hablada, como si lo
dijeras por teléfono. Nada de tablas, viñetas ni markdown; di los números redondos y lo esencial (esa
respuesta se convierte en nota de voz).

SI TE PIDEN LA RESPUESTA EN PDF / documento: NO digas que no puedes generar PDFs — sí se puede, se
arma solo con tu respuesta. Contesta el contenido completo y bien organizado (ese texto se convierte
en el PDF). Consulta las herramientas que hagan falta como siempre.

ACCIONES QUE SÍ PUEDES EJECUTAR (con confirmación): bloquear un número caliente, gestionar una alerta
(reconocer/posponer/resolver), despachar/asignar una tarea a un mensajero, aprobar una solicitud, y
cargar un egreso (gasto). NO tienes poderes propios: el sistema re-chequea el permiso de {nombre} y
ejecuta EN SU NOMBRE — si {nombre} no tiene el permiso en el CRM, la acción se rechaza y se lo dices.

CONFIRMACIÓN OBLIGATORIA (regla de oro — el dinero/operación solo se mueve con el "sí" explícito):
para CUALQUIER acción de escritura son SIEMPRE DOS PASOS:
  1. Primero llamas la herramienta con `confirmado=False`: te devuelve un PREVIEW de lo que va a pasar.
     Se lo muestras a {nombre} y le preguntas si confirma.
  2. SOLO cuando {nombre} te diga que SÍ en un mensaje NUEVO (ej. "sí", "hazlo", "dale"), llamas la
     MISMA herramienta con `confirmado=True` para ejecutar.
NUNCA pases `confirmado=True` sin que el usuario haya confirmado explícitamente después de ver el
preview. Si dudas de la intención, NO ejecutes: pregunta. El dinero solo se mueve por decisión del
admin/encargado — tú eres su instrumento, con su confirmación.

REGLAS DURAS:
- No inventes números: si una herramienta no trae el dato, dilo con precisión y di dónde se consulta.
  Reporta lo que ves, sin adornar y sin redondear a lo loco.
"""


@lru_cache(maxsize=1)
def _checkpointer():
    """Memoria de conversación durable (thread_id = teléfono).

    PostgresSaver sobre un pool de conexiones del Postgres del asistente: el hilo de cada usuario
    SOBREVIVE los reinicios/deploys (antes, con MemorySaver en-memoria, cada deploy borraba el
    contexto de todos → el bot "olvidaba" la conversación a mitad). `from_conn_string` devuelve un
    context manager (no sirve para un app de larga vida); por eso el pool.

    FALLBACK SEGURO: si el pool no arranca (Postgres inalcanzable en el cold start), cae a MemorySaver
    para seguir contestando en modo degradado (no-durable) en vez de tumbar el bot. La durabilidad de
    la conversación no es una frontera de seguridad, así que fail-open acá es correcto; se loguea fuerte."""
    try:
        from psycopg_pool import ConnectionPool
        from langgraph.checkpoint.postgres import PostgresSaver
        # PostgresSaver exige conexiones autocommit; prepare_threshold=0 evita conflictos de prepared
        # statements a través del pool. El pool queda vivo (module-level vía lru_cache) para el app.
        pool = ConnectionPool(
            conninfo=config.DATABASE_URL,
            max_size=10,
            kwargs={"autocommit": True, "prepare_threshold": 0},
        )
        saver = PostgresSaver(pool)
        saver.setup()  # idempotente: crea las tablas checkpoint* si faltan
        print("[checkpointer] PostgresSaver durable listo (conversación sobrevive reinicios).")
        return saver
    except Exception as ex:  # noqa: BLE001
        print(f"[checkpointer] Postgres no disponible ({ex}) → MemorySaver en-memoria (NO durable).")
        return MemorySaver()


def preparar_memoria() -> None:
    """Inicializa el checkpointer al arrancar (crea las tablas si faltan) para no pagar el `setup()`
    en la primera consulta. Best-effort: si falla, `_checkpointer()` reintenta en la primera consulta."""
    try:
        _checkpointer()
    except Exception as ex:  # noqa: BLE001
        print(f"[checkpointer] preparar_memoria avisó: {ex}")


def _modelo():
    """Construye el chat model según el proveedor configurado (agnóstico: cambiar = una línea).
    Ambos soportan tool-calling, que es lo que create_react_agent necesita."""
    if config.LLM_PROVIDER == "deepseek":
        # DeepSeek expone una API OpenAI-compatible → la usamos vía ChatOpenAI con su base_url.
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            model=config.DEEPSEEK_MODEL,
            api_key=config.DEEPSEEK_API_KEY,
            base_url=config.DEEPSEEK_BASE_URL,
            max_tokens=1024,
            temperature=0,
        )
    # Por defecto: Claude (Anthropic).
    from langchain_anthropic import ChatAnthropic
    return ChatAnthropic(
        model=config.LLM_MODEL,
        api_key=config.ANTHROPIC_API_KEY,
        max_tokens=1024,
        temperature=0,
    )


def responder(identidad: Identidad, consorcio: Consorcio, texto: str) -> str:
    """Corre el agente para un mensaje y devuelve la respuesta en texto."""
    if identidad.es_global:
        alcance = ("VE TODO el consorcio (rol de alcance global). Puede consultar cualquier banca, "
                   "totales del consorcio, cualquier empleado.")
    else:
        n = len(identidad.banca_ids)
        alcance = (
            f"ACOTADO: solo puede ver SUS bancas ({n} banca(s)). NO le des totales del consorcio, ni "
            f"datos de bancas o personas que no sean suyas. Si pide algo que abarca todo el consorcio "
            f"(o una banca ajena), acláralo y limítate a lo suyo. Las herramientas ya están acotadas a "
            f"su alcance: si una devuelve que algo está fuera de su alcance, respétalo."
        )
    system = SYSTEM_TMPL.format(
        consorcio=consorcio.nombre,
        nombre=identidad.nombre or "el usuario",
        rol=identidad.rol or "operador",
        fecha_hoy=date.today().isoformat(),
        alcance=alcance,
    )
    agente = create_react_agent(
        _modelo(),
        construir_tools(consorcio, identidad.telefono, identidad),
        prompt=system,
        checkpointer=_checkpointer(),
    )
    # thread_id = teléfono → cada usuario mantiene su hilo durable.
    conf = {"configurable": {"thread_id": identidad.telefono}}
    resultado = agente.invoke({"messages": [("user", texto)]}, conf)
    ultimo = resultado["messages"][-1]
    return ultimo.content if isinstance(ultimo.content, str) else str(ultimo.content)
