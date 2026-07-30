-- iguana-asistente · schema de la DB PROPIA del asistente (su capa por encima de los consorcios).
-- NO es la DB de ningún consorcio. Guarda tres cosas: registro, directorio, memoria.

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS vector;   -- pgvector, para la memoria semántica

-- ── Registro de consorcios ────────────────────────────────────────────────────
-- Dónde vive cada back y con qué CREDENCIAL entra el bot. El back emite un accessToken corto +
-- refreshToken (no hay token durable de servicio), así que guardamos las credenciales del usuario de
-- servicio y el asistente hace login/refresh. La contraseña va CIFRADA (app-level, ASISTENTE_SECRET_KEY);
-- nunca en claro en la fila.
CREATE TABLE consorcios (
  id                 uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  nombre             text NOT NULL,
  backend_url        text NOT NULL,             -- p.ej. https://iguana-api.iguanatech.net
  usuario            text NOT NULL,             -- nombre_usuario del bot (solo lectura)
  password_cifrado   text NOT NULL,             -- contraseña del bot (CIFRADA)
  activo             boolean NOT NULL DEFAULT true,
  creado_en          timestamptz NOT NULL DEFAULT now()
);

-- ── Directorio de teléfonos ───────────────────────────────────────────────────
-- teléfono (identidad verificada por WhatsApp) → a qué consorcio pertenece + quién es.
-- Se AUTOLLENA jalando el roster de cada back (empleados con teléfono) y se refresca solo.
CREATE TABLE directorio (
  telefono       text PRIMARY KEY,              -- E.164 sin '+', como lo da WhatsApp (wa_id)
  consorcio_id   uuid NOT NULL REFERENCES consorcios(id) ON DELETE CASCADE,
  usuario_id     uuid,                          -- id del usuario en el back de ese consorcio
  empleado_id    uuid,
  nombre         text,
  rol            text,                          -- admin | encargado | contable | ...
  sincronizado_en timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_directorio_consorcio ON directorio(consorcio_id);

-- ── Memoria de conversación ───────────────────────────────────────────────────
-- Estado durable de LangGraph (checkpointer) va en su propia tabla que crea la lib.
-- Acá guardamos la memoria SEMÁNTICA de largo plazo (hechos que el asistente aprende),
-- indexada por embedding para recuperarla por similitud.
CREATE TABLE memoria (
  id             uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  telefono       text NOT NULL,                 -- de quién es la memoria
  consorcio_id   uuid REFERENCES consorcios(id) ON DELETE CASCADE,
  contenido      text NOT NULL,                 -- el hecho/nota, en texto
  -- 384 = dimensión del modelo LOCAL en uso (paraphrase-multilingual-MiniLM-L12-v2). Antes decía
  -- 1536 con un TODO: ese número era de un proveedor externo que nunca se usó. `memoria.preparar()`
  -- vuelve a chequearlo al arrancar preguntándole la dimensión al modelo, así que un cambio de
  -- modelo no exige tocar este archivo (que además sólo corre al crear la base de cero).
  embedding      vector(384),
  modelo         text,                          -- qué vectorizador la produjo (ver memoria._firma_modelo)
  creado_en      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_memoria_telefono ON memoria(telefono);
-- Índice ANN para búsqueda por similitud (coseno). Crear tras cargar datos si el volumen crece.
CREATE INDEX idx_memoria_embedding ON memoria USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- ── Bitácora de interacciones (auditoría del asistente) ───────────────────────
-- Toda conversación queda registrada: quién, qué preguntó, qué herramientas llamó, qué respondió.
-- El asistente ve a TODOS → esto es parte de su superficie de seguridad.
CREATE TABLE bitacora (
  id             uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  telefono       text NOT NULL,
  consorcio_id   uuid REFERENCES consorcios(id) ON DELETE SET NULL,
  entrada        text,                          -- lo que mandó el usuario (o [voz])
  herramientas   jsonb,                         -- qué tools llamó y con qué args
  respuesta      text,                          -- lo que contestó el asistente
  creado_en      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_bitacora_telefono ON bitacora(telefono, creado_en DESC);
