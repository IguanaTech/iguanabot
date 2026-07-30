"""Memoria SEMÁNTICA de largo plazo del asistente.

Distinta de la memoria de conversación (el checkpointer de LangGraph, `graph._checkpointer`):
aquélla recuerda EL HILO —lo que se dijo hace tres mensajes— y se poda por inactividad. Ésta
recuerda HECHOS DURABLES de una persona y los trae de vuelta por PARECIDO, no por fecha: dentro
de dos meses, "¿cómo va mi banca?" tiene que poder resolverse contra un "mi banca principal es
La Suerte" dicho en marzo, que la ventana deslizante ya tiró hace rato.

QUÉ SE GUARDA, y por qué importa la distinción:
  · SÍ — preferencias y contexto estable: cuál es su banca, cómo quiere los reportes, quién es
    quién para él, cómo llama a las cosas.
  · NO — CIFRAS DE LA OPERACIÓN. Guardar "hoy hay 50 mil disponibles" es fabricar una mentira
    con fecha de vencimiento: dentro de una semana el bot lo recuerda y lo AFIRMA, y el número
    ya no existe. Los números se consultan al back, siempre. Esto está dicho en la descripción
    de la herramienta y repetido en el prompt porque es el único modo real de que se respete.

DOS FRONTERAS, con reglas opuestas a propósito:
  · DISPONIBILIDAD — fail-open. Si el modelo de embeddings o el Postgres no están, el bot
    contesta igual, sin memoria. Que se caiga entero por no poder recordar una preferencia
    sería peor que la falla que evita (mismo criterio que el checkpointer).
  · ALCANCE — fail-closed. La memoria es de UNA persona en UN consorcio. Sin `consorcio_id`
    resuelto no se lee ni se escribe NADA: el asistente le habla a gente distinta del mismo
    consorcio y a consorcios distintos, así que una fuga acá le pone en boca de uno lo que dijo
    otro. Ante la duda, no recordar.
"""
from __future__ import annotations

import re
from functools import lru_cache

from .config import config

# Cuántos recuerdos se le inyectan al prompt y desde qué parecido. Medido con el modelo real
# (paraphrase-multilingual-MiniLM-L12-v2) sobre frases dominicanas: un recuerdo y una consulta
# del mismo tema dan ~0.43-0.76; temas ajenos ~0.22-0.37. Las franjas SE TOCAN, así que un
# umbral alto se comería recuerdos buenos. Se prefiere un piso bajo y pocos resultados: un
# recuerdo de más entra como nota de contexto y el modelo lo ignora; uno de menos hace que la
# función entera no sirva.
TOP_K = 4
PISO_PARECIDO = 0.30


# ── Embeddings (locales, sin proveedor externo) ──────────────────────────────
# Todo en este bot se auto-hospeda (Whisper para transcribir, Piper para hablar, WeasyPrint para
# el PDF). Los embeddings siguen la misma línea: un modelo ONNX chico que corre en el mismo
# contenedor. Anthropic no ofrece embeddings, así que la alternativa era sumar OTRO proveedor y
# otra clave para vectorizar cuatro frases — no vale el acoplamiento.

@lru_cache(maxsize=1)
def _modelo():
    """Carga perezosa del modelo. Devuelve None si no está disponible (fail-open)."""
    try:
        from fastembed import TextEmbedding
        m = TextEmbedding(config.EMBEDDING_MODEL)
        print(f"[memoria] embeddings locales listos ({config.EMBEDDING_MODEL}).")
        return m
    except Exception as ex:  # noqa: BLE001
        print(f"[memoria] sin modelo de embeddings ({ex}) → memoria semántica APAGADA.")
        return None


def _vector(texto: str) -> list[float] | None:
    m = _modelo()
    if m is None or not texto or not texto.strip():
        return None
    try:
        return [float(x) for x in next(iter(m.embed([texto])))]
    except Exception as ex:  # noqa: BLE001
        print(f"[memoria] no se pudo vectorizar ({ex}).")
        return None


def _firma_modelo() -> str:
    """Identidad del vectorizador: modelo + versión menor de la librería.

    Dos vectores sólo se pueden comparar si salieron del MISMO modelo con el MISMO pooling, y el
    pooling es una decisión de la librería, no del modelo: fastembed 0.8 cambió MiniLM de CLS a
    mean pooling. Sin registrar esto, un salto de versión no rompe nada visible — simplemente las
    búsquedas dejan de encontrar lo viejo, sin un solo error. Guardarlo permite IGNORAR lo que
    quedó de otro vectorizador en vez de mezclarlo."""
    try:
        import fastembed
        v = ".".join(str(fastembed.__version__).split(".")[:2])
    except Exception:  # noqa: BLE001
        v = "?"
    return f"{config.EMBEDDING_MODEL}@fastembed{v}"


def _dimension() -> int | None:
    """Dimensión REAL del modelo cargado. Se pregunta, no se asume: el esquema original decía
    vector(1536) con un TODO de 'ajustar al modelo elegido', y ese número era de otro proveedor.
    Si el modelo cambia por config, la tabla se provisiona con la dimensión que corresponda."""
    v = _vector("dimensión")
    return len(v) if v else None


# ── Conexión ─────────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _pool():
    try:
        from psycopg_pool import ConnectionPool
        return ConnectionPool(conninfo=config.DATABASE_URL, max_size=4,
                              kwargs={"autocommit": True})
    except Exception as ex:  # noqa: BLE001
        print(f"[memoria] sin Postgres ({ex}) → memoria semántica APAGADA.")
        return None


def preparar() -> None:
    """Provisiona la tabla con la dimensión del modelo vigente. Idempotente, corre al arrancar.

    Hace falta porque `db/001_schema.sql` sólo se ejecuta cuando el volumen de Postgres se crea
    de cero (`docker-entrypoint-initdb.d`): en un despliegue que ya existe, ese archivo NO vuelve
    a correr nunca. Mismo patrón que `PostgresSaver.setup()`.

    Si la tabla ya existe con OTRA dimensión y tiene filas, NO se toca y se apaga la memoria: un
    `ALTER` ahí abajo tiraría los vectores guardados. Prefiero degradar con un aviso fuerte antes
    que destruir datos por conveniencia.
    """
    pool, dim = _pool(), _dimension()
    if pool is None or dim is None:
        return
    try:
        with pool.connection() as cx:
            cx.execute("CREATE EXTENSION IF NOT EXISTS vector")
            actual = cx.execute("""
                SELECT a.atttypmod
                  FROM pg_attribute a
                  JOIN pg_class c ON c.oid = a.attrelid
                 WHERE c.relname = 'memoria' AND a.attname = 'embedding' AND a.attnum > 0
            """).fetchone()

            if actual is None:
                cx.execute(f"""
                    CREATE TABLE IF NOT EXISTS memoria (
                      id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                      telefono     text NOT NULL,
                      -- MISMA definición que db/001_schema.sql, FK incluida: dos declaraciones de
                      -- la misma tabla que difieren terminan divergiendo. El CASCADE importa —
                      -- si se da de baja un consorcio, sus recuerdos se van con él.
                      consorcio_id uuid REFERENCES consorcios(id) ON DELETE CASCADE,
                      contenido    text NOT NULL,
                      embedding    vector({dim}),
                      modelo       text,
                      creado_en    timestamptz NOT NULL DEFAULT now()
                    )""")
            elif actual[0] != dim:
                filas = cx.execute("SELECT count(*) FROM memoria").fetchone()[0]
                if filas:
                    print(f"[memoria] la tabla tiene vector({actual[0]}) con {filas} fila(s) y el "
                          f"modelo da {dim} → memoria APAGADA para no borrar lo guardado. "
                          f"Migrar a mano o volver al modelo anterior.")
                    return
                cx.execute(f"ALTER TABLE memoria ALTER COLUMN embedding TYPE vector({dim})")
                print(f"[memoria] tabla vacía: dimensión ajustada {actual[0]} → {dim}.")

            # El índice va DESPUÉS de fijar la dimensión (ivfflat la exige concreta). Se recrea
            # sólo si falta; con pocos datos el escaneo secuencial es igual de rápido.
            # Columna nueva sobre tablas que ya existían (la del schema original no la tenía).
            cx.execute("ALTER TABLE memoria ADD COLUMN IF NOT EXISTS modelo text")
            cx.execute("CREATE INDEX IF NOT EXISTS idx_memoria_telefono ON memoria(telefono)")
            cx.execute("CREATE INDEX IF NOT EXISTS idx_memoria_embedding "
                       "ON memoria USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)")
        print("[memoria] memoria semántica lista.")
    except Exception as ex:  # noqa: BLE001
        print(f"[memoria] no se pudo preparar ({ex}) → memoria semántica APAGADA.")


def _sql_vector(v: list[float]) -> str:
    """pgvector acepta el literal '[a,b,c]' y lo castea. Se arma acá para no depender del
    adaptador `pgvector-python` sólo por esto."""
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


# ── API ──────────────────────────────────────────────────────────────────────

def recordar(telefono: str, consorcio_id: str | None, contenido: str) -> bool:
    """Guarda un hecho durable. Devuelve False si no se pudo (y el caller lo dice, no lo esconde)."""
    if not consorcio_id:
        return False                      # fail-closed: sin consorcio no hay dónde guardarlo
    contenido = (contenido or "").strip()
    if len(contenido) < 3:
        return False
    contenido = contenido[:500]
    pool, v = _pool(), _vector(contenido)
    if pool is None or v is None:
        return False
    try:
        with pool.connection() as cx:
            # Sin duplicados exactos: la gente repite lo mismo con otras palabras y no pasa nada,
            # pero guardar dos veces la MISMA frase sólo gasta lugar y sesga la recuperación
            # (dos vecinos idénticos ocupan dos de los cuatro cupos).
            ya = cx.execute(
                "SELECT 1 FROM memoria WHERE telefono=%s AND consorcio_id=%s AND lower(contenido)=lower(%s)",
                (telefono, consorcio_id, contenido)).fetchone()
            if ya:
                return True
            cx.execute(
                "INSERT INTO memoria (telefono, consorcio_id, contenido, embedding, modelo) "
                "VALUES (%s, %s, %s, %s::vector, %s)",
                (telefono, consorcio_id, contenido, _sql_vector(v), _firma_modelo()))
        return True
    except Exception as ex:  # noqa: BLE001
        print(f"[memoria] no se pudo guardar ({ex}).")
        return False


def recuperar(telefono: str, consorcio_id: str | None, consulta: str,
              k: int = TOP_K) -> list[str]:
    """Los recuerdos más parecidos a la consulta, de ESA persona en ESE consorcio."""
    if not consorcio_id:
        return []                         # fail-closed
    pool, v = _pool(), _vector(consulta)
    if pool is None or v is None:
        return []
    try:
        with pool.connection() as cx:
            # `<=>` es DISTANCIA coseno (0 = idéntico) → el parecido es 1 - distancia.
            # El filtro por telefono Y consorcio va en el WHERE, no después: si se filtrara en
            # Python, el ORDER BY habría ordenado sobre recuerdos ajenos y el LIMIT podría
            # devolver menos de los propios que existen.
            filas = cx.execute(
                "SELECT contenido, 1 - (embedding <=> %s::vector) AS parecido "
                "  FROM memoria "
                " WHERE telefono = %s AND consorcio_id = %s AND embedding IS NOT NULL "
                "   AND (modelo IS NULL OR modelo = %s) "
                " ORDER BY embedding <=> %s::vector "
                " LIMIT %s",
                (_sql_vector(v), telefono, consorcio_id, _firma_modelo(),
                 _sql_vector(v), k)).fetchall()
        return [c for (c, p) in filas if p is not None and p >= PISO_PARECIDO]
    except Exception as ex:  # noqa: BLE001
        print(f"[memoria] no se pudo recuperar ({ex}).")
        return []


def listar(telefono: str, consorcio_id: str | None, limite: int = 30) -> list[str]:
    """Todo lo que el bot recuerda de esa persona, lo más nuevo primero. Es la contraparte
    indispensable de poder guardar: si no se puede VER lo guardado, tampoco se puede corregir."""
    if not consorcio_id:
        return []
    pool = _pool()
    if pool is None:
        return []
    try:
        with pool.connection() as cx:
            filas = cx.execute(
                "SELECT contenido FROM memoria WHERE telefono=%s AND consorcio_id=%s "
                "ORDER BY creado_en DESC LIMIT %s",
                (telefono, consorcio_id, limite)).fetchall()
        return [c for (c,) in filas]
    except Exception as ex:  # noqa: BLE001
        print(f"[memoria] no se pudo listar ({ex}).")
        return []


def olvidar(telefono: str, consorcio_id: str | None, patron: str) -> int:
    """Borra los recuerdos que contengan `patron` (sin distinguir mayúsculas). Devuelve cuántos.

    Por TEXTO y no por parecido a propósito: borrar es irreversible, y "olvida lo de la banca"
    resuelto por vecindad semántica podría llevarse por delante algo que la persona no nombró.
    Que borre de menos y haya que repetirlo es preferible a que borre de más."""
    if not consorcio_id or not (patron or "").strip():
        return 0
    pool = _pool()
    if pool is None:
        return 0
    try:
        like = "%" + re.sub(r"[%_]", lambda m: "\\" + m.group(), patron.strip()) + "%"
        with pool.connection() as cx:
            filas = cx.execute(
                "DELETE FROM memoria WHERE telefono=%s AND consorcio_id=%s "
                "AND contenido ILIKE %s RETURNING id",
                (telefono, consorcio_id, like)).fetchall()
        return len(filas)
    except Exception as ex:  # noqa: BLE001
        print(f"[memoria] no se pudo olvidar ({ex}).")
        return 0


def purgar(dias: int) -> int:
    """Borra recuerdos más viejos que `dias`. Lo llama el job de retención."""
    pool = _pool()
    if pool is None or dias <= 0:
        return 0
    try:
        with pool.connection() as cx:
            filas = cx.execute(
                "DELETE FROM memoria WHERE creado_en < now() - make_interval(days => %s) RETURNING id",
                (dias,)).fetchall()
        return len(filas)
    except Exception as ex:  # noqa: BLE001
        print(f"[memoria] no se pudo purgar ({ex}).")
        return 0
