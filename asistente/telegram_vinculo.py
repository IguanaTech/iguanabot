"""Vínculo `chat_id de Telegram` ↔ `teléfono`.

POR QUÉ EXISTE ESTE ARCHIVO, y por qué es la parte delicada de todo el canal de Telegram:

Todo el gobierno del asistente cuelga del TELÉFONO. `identity.resolver(telefono)` le pregunta a
los backs quién es esa persona, de qué consorcio, con qué rol y viendo qué bancas — y es
fail-closed: si el back no contesta, no se atiende a nadie. Esa tabla se configura desde el CRM y
es la que decide quién puede hablarle al bot.

Telegram NO da un teléfono: da un `chat_id`. La tentación es inventarle una identidad propia al
canal (una lista de chat_ids autorizados, un rol por Telegram). Eso sería un SEGUNDO modelo de
permisos viviendo en paralelo al del CRM, y en cuanto los dos digan cosas distintas sobre la misma
persona hay un agujero, no una molestia.

Así que acá no se decide NADA sobre permisos: sólo se traduce `chat_id → teléfono`, y de ahí para
abajo manda exactamente el mismo camino de siempre. Este módulo es un traductor, no una autoridad.

CÓMO SE OBTIENE EL TELÉFONO, y por qué así: Telegram tiene un botón nativo de "compartir mi
contacto". El número que llega por ese camino lo pone TELEGRAM desde la cuenta, no lo teclea la
persona — o sea que no se puede escribir el número de otro y hacerse pasar por él. Pero hay un
borde: por ese mismo campo se puede reenviar el contacto de UN TERCERO. Por eso se exige que el
`user_id` del contacto sea el `user_id` de quien escribe (`vincular` lo verifica); si no coinciden,
es alguien mandando el contacto ajeno y se rechaza.
"""
from __future__ import annotations

import re
from functools import lru_cache

from .config import config


@lru_cache(maxsize=1)
def _pool():
    try:
        from psycopg_pool import ConnectionPool
        return ConnectionPool(conninfo=config.DATABASE_URL, max_size=4,
                              kwargs={"autocommit": True})
    except Exception as ex:  # noqa: BLE001
        print(f"[telegram] sin Postgres ({ex}) → el canal de Telegram no puede resolver identidad.")
        return None


def preparar() -> None:
    """Crea la tabla si falta. Idempotente, al arrancar — `db/001_schema.sql` sólo corre cuando el
    volumen de Postgres se crea de cero, así que en un despliegue existente no volvería a pasar."""
    pool = _pool()
    if pool is None:
        return
    try:
        with pool.connection() as cx:
            cx.execute("""
                CREATE TABLE IF NOT EXISTS telegram_vinculo (
                  chat_id    bigint PRIMARY KEY,
                  user_id    bigint NOT NULL,
                  telefono   text   NOT NULL,
                  creado_en  timestamptz NOT NULL DEFAULT now()
                )""")
            # Un teléfono, un chat. Si la persona cambia de cuenta de Telegram, el vínculo nuevo
            # reemplaza al viejo (lo hace `vincular`): dos chats vivos para el mismo teléfono
            # harían que un mensaje proactivo saliera por el canal equivocado, o por los dos.
            cx.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_telegram_vinculo_telefono "
                       "ON telegram_vinculo(telefono)")
    except Exception as ex:  # noqa: BLE001
        print(f"[telegram] no se pudo preparar el vínculo ({ex}).")


def _normalizar(telefono: str) -> str:
    """Sólo dígitos, como los guarda el resto del sistema (los wa_id vienen así). Telegram manda el
    número con '+' y a veces con espacios; sin normalizar, el MISMO teléfono no se reconocería."""
    return re.sub(r"\D", "", telefono or "")


def telefono_de(chat_id: int) -> str | None:
    """El teléfono vinculado a ese chat, o None si todavía no compartió su contacto."""
    pool = _pool()
    if pool is None:
        return None
    try:
        with pool.connection() as cx:
            r = cx.execute("SELECT telefono FROM telegram_vinculo WHERE chat_id = %s",
                           (chat_id,)).fetchone()
        return r[0] if r else None
    except Exception as ex:  # noqa: BLE001
        print(f"[telegram] no se pudo resolver el chat {chat_id} ({ex}).")
        return None


def chat_de(telefono: str) -> int | None:
    """El chat de un teléfono, para los mensajes que INICIA el bot (reporte de cierre, alarmas).
    None = esa persona no está en Telegram; el que llama debe irse por el otro canal."""
    pool = _pool()
    if pool is None:
        return None
    try:
        with pool.connection() as cx:
            r = cx.execute("SELECT chat_id FROM telegram_vinculo WHERE telefono = %s",
                           (_normalizar(telefono),)).fetchone()
        return int(r[0]) if r else None
    except Exception as ex:  # noqa: BLE001
        print(f"[telegram] no se pudo resolver el teléfono ({ex}).")
        return None


def vincular(chat_id: int, quien_escribe_user_id: int,
             contacto_user_id: int | None, contacto_telefono: str) -> str | None:
    """Ata el chat al teléfono del contacto compartido. Devuelve el teléfono, o None si se rechaza.

    LA VERIFICACIÓN QUE IMPORTA: el contacto tiene que ser el DE QUIEN ESCRIBE. Telegram permite
    reenviar la tarjeta de contacto de un tercero por el mismo campo; sin este chequeo, cualquiera
    podría mandar el contacto del administrador y quedar vinculado como él — y a partir de ahí el
    bot le hablaría con el alcance del administrador. El `user_id` del contacto sólo viene cuando
    esa persona TIENE cuenta de Telegram, y si no coincide con quien escribe, se rechaza.
    """
    tel = _normalizar(contacto_telefono)
    if not tel:
        return None
    if contacto_user_id is None or int(contacto_user_id) != int(quien_escribe_user_id):
        print(f"[telegram] RECHAZADO: el chat {chat_id} mandó el contacto de otra persona "
              f"(contacto user_id={contacto_user_id}, quien escribe={quien_escribe_user_id}).")
        return None

    pool = _pool()
    if pool is None:
        return None
    try:
        with pool.connection() as cx:
            # Se borra cualquier vínculo previo de ESE teléfono (cambió de cuenta de Telegram) y
            # cualquier vínculo previo de ESE chat (alguien más usaba este teléfono). Después se
            # inserta el nuevo: así nunca quedan dos vivos apuntándose entre sí.
            cx.execute("DELETE FROM telegram_vinculo WHERE telefono = %s OR chat_id = %s",
                       (tel, chat_id))
            cx.execute("INSERT INTO telegram_vinculo (chat_id, user_id, telefono) "
                       "VALUES (%s, %s, %s)", (chat_id, quien_escribe_user_id, tel))
        print(f"[telegram] chat {chat_id} vinculado.")
        return tel
    except Exception as ex:  # noqa: BLE001
        print(f"[telegram] no se pudo vincular ({ex}).")
        return None


def desvincular(chat_id: int) -> bool:
    """Corta el vínculo (la persona pide salir, o se le retira el acceso)."""
    pool = _pool()
    if pool is None:
        return False
    try:
        with pool.connection() as cx:
            r = cx.execute("DELETE FROM telegram_vinculo WHERE chat_id = %s RETURNING chat_id",
                           (chat_id,)).fetchone()
        return r is not None
    except Exception as ex:  # noqa: BLE001
        print(f"[telegram] no se pudo desvincular ({ex}).")
        return False
