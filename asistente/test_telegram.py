"""Prueba del vínculo de Telegram contra un Postgres REAL.

Lo que se custodia acá es una frontera de SEGURIDAD, no una comodidad: si alguien puede vincular
su chat al teléfono de otro, el bot le contesta con el alcance de ese otro — le muestra bancas,
dinero y gente que no son suyos. Telegram deja reenviar la tarjeta de contacto de un tercero por
el mismo campo por donde llega la propia, así que la diferencia entre "compartí mi contacto" y
"mandé el contacto del jefe" es un solo campo, y es el que se verifica.

    docker compose -f docker-compose.test.yml up -d
    DATABASE_URL=postgresql://asistente:asistente@127.0.0.1:15433/asistente \\
      python -m asistente.test_telegram
"""
import sys

from . import telegram_vinculo as tv

CHAT_ANA, USER_ANA, TEL_ANA = 1001, 5001, "+1 809 111 0000"
CHAT_MALO, USER_MALO = 1002, 5002
TEL_ADMIN = "18095550000"

fallos = []


def debe(cond, msg):
    print(("  ok   " if cond else "  FALLA ") + msg)
    if not cond:
        fallos.append(msg)


def main() -> int:
    tv.preparar()
    pool = tv._pool()
    if pool is None:
        print("sin Postgres: no se puede probar.")
        return 1
    with pool.connection() as cx:
        cx.execute("DELETE FROM telegram_vinculo WHERE chat_id IN (%s,%s)", (CHAT_ANA, CHAT_MALO))

    print("\n1. vincular compartiendo el contacto PROPIO")
    # LOS ÚLTIMOS 10 DÍGITOS, sin el 1 del país. Este test esperaba "18091110000" y quedó en rojo
    # cuando la normalización cambió (2026-08-06) — encodeaba el contrato viejo. El cambio fue a
    # propósito y por un caso real: Eduardo estaba vinculado a Telegram y el reporte de cierre no le
    # llegaba, porque Telegram comparte `18099074550` y su ficha guarda `(809) 907-4550`. Dos formas
    # del mismo número que no coincidían. Un dominicano son 10 dígitos; el 1 sobra.
    tel = tv.vincular(CHAT_ANA, USER_ANA, USER_ANA, TEL_ANA)
    debe(tel == "8091110000", f"normaliza a los últimos 10 dígitos (dio {tel!r})")
    debe(tv.telefono_de(CHAT_ANA) == "8091110000", "el chat resuelve a su teléfono")
    debe(tv.chat_de("8091110000") == CHAT_ANA, "y el teléfono resuelve a su chat (envío proactivo)")
    debe(tv.chat_de("+1-809-111-0000") == CHAT_ANA,
         "el teléfono se resuelve venga como venga formateado")
    # LO QUE EL CAMBIO VINO A GARANTIZAR, escrito para que no se pierda otra vez: el MISMO número
    # con y sin el 1 del país tiene que caer en el mismo chat.
    debe(tv.chat_de("18091110000") == CHAT_ANA,
         "con el 1 del país adelante NO resolvió al mismo chat — es exactamente el bug que dejó a "
         "Eduardo sin recibir el reporte de cierre")
    debe(tv.chat_de("(809) 111-0000") == CHAT_ANA,
         "como lo guarda la ficha del empleado tampoco resolvió")

    print("\n2. SUPLANTACIÓN — lo que este archivo existe para impedir")
    debe(tv.vincular(CHAT_MALO, USER_MALO, USER_ANA, TEL_ANA) is None,
         "reenviar el contacto de OTRO no vincula")
    debe(tv.vincular(CHAT_MALO, USER_MALO, None, TEL_ADMIN) is None,
         "un contacto sin dueño (sin cuenta de Telegram) tampoco vincula")
    debe(tv.telefono_de(CHAT_MALO) is None, "el chat del que lo intentó quedó SIN vínculo")
    debe(tv.chat_de("18091110000") == CHAT_ANA, "y el de Ana sigue siendo el suyo")

    print("\n3. un teléfono, un chat")
    tv.vincular(CHAT_MALO, USER_MALO, USER_MALO, TEL_ANA)   # Ana cambió de cuenta de Telegram
    debe(tv.chat_de("18091110000") == CHAT_MALO, "el vínculo nuevo reemplaza al viejo")
    debe(tv.telefono_de(CHAT_ANA) is None,
         "y el chat anterior queda sin teléfono (si no, el reporte saldría por los dos)")

    print("\n4. desvincular")
    debe(tv.desvincular(CHAT_MALO) is True, "desvincula")
    debe(tv.telefono_de(CHAT_MALO) is None and tv.chat_de("18091110000") is None, "y no queda nada")
    debe(tv.desvincular(CHAT_MALO) is False, "desvincular dos veces no miente diciendo que sí")

    print("\n5. sin vínculo, no hay teléfono que inventar")
    debe(tv.telefono_de(999999) is None, "un chat desconocido no resuelve a nadie")
    debe(tv.chat_de(TEL_ADMIN) is None, "un teléfono sin Telegram no resuelve a ningún chat")

    print("\n" + ("TODO OK" if not fallos else f"{len(fallos)} FALLA(S): " + "; ".join(fallos)))
    return 1 if fallos else 0


if __name__ == "__main__":
    sys.exit(main())
