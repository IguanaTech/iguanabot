"""Alta manual de una persona al directorio (teléfono → consorcio + nombre + rol).

Para Fase 0 (pocos admins) esto es lo más simple y confiable: no depende de que el bot tenga permiso
de listar empleados. El auto-sync del roster (identity.sincronizar_roster) queda como camino futuro y
requiere darle al usuario de servicio el permiso EMPLEADOS_VER.

El `rol` importa: 'admin'/'encargado' son los que reciben el reporte del cierre y las alarmas de dinero.

Uso:
    python -m asistente.agregar_persona <consorcio_id> <telefono> "<Nombre>" <rol>
Ejemplo:
    python -m asistente.agregar_persona 2a97b243-... 18095551234 "Eduardo" admin
"""
import re
import sys

import psycopg

from .config import config


def main() -> None:
    if len(sys.argv) != 5:
        print(__doc__)
        sys.exit(1)
    consorcio_id, telefono, nombre, rol = sys.argv[1:5]
    tel = re.sub(r"\D", "", telefono)
    if len(tel) < 10:
        print("Teléfono inválido (esperado E.164 sin '+', p.ej. 18095551234).")
        sys.exit(1)

    with psycopg.connect(config.DATABASE_URL) as conn:
        conn.execute(
            """
            INSERT INTO directorio (telefono, consorcio_id, nombre, rol, sincronizado_en)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (telefono) DO UPDATE SET
                consorcio_id = EXCLUDED.consorcio_id,
                nombre = EXCLUDED.nombre,
                rol = EXCLUDED.rol,
                sincronizado_en = now()
            """,
            (tel, consorcio_id, nombre, rol),
        )
        conn.commit()
    print(f"OK: {nombre} ({rol}) · {tel} → consorcio {consorcio_id}")


if __name__ == "__main__":
    main()
