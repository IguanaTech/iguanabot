"""Onboardear un consorcio = 1 fila. Cifra la contraseña del bot, la mete al registro, y jala su roster.

Uso (dentro del contenedor del asistente, con .env cargado):
    python -m asistente.alta_consorcio "Nombre del Consorcio" https://back.url USUARIO PASSWORD

USUARIO/PASSWORD son las credenciales del usuario de SERVICIO acotado (solo lectura) que crea el script
`scripts/crear_usuario_servicio.js` del back.
"""
import sys

import psycopg

from .config import config
from .identity import sincronizar_roster
from .registry import Consorcio, cifrar


def main() -> None:
    if len(sys.argv) != 5:
        print(__doc__)
        sys.exit(1)
    nombre, backend_url, usuario, password = sys.argv[1], sys.argv[2].rstrip("/"), sys.argv[3], sys.argv[4]

    with psycopg.connect(config.DATABASE_URL) as conn:
        row = conn.execute(
            "INSERT INTO consorcios (nombre, backend_url, usuario, password_cifrado) "
            "VALUES (%s,%s,%s,%s) RETURNING id",
            (nombre, backend_url, usuario, cifrar(password)),
        ).fetchone()
        conn.commit()
        cid = str(row[0])
    print(f"Consorcio registrado: {nombre} ({cid})")

    # Jala el roster de una vez para poblar el directorio.
    try:
        n = sincronizar_roster(Consorcio(id=cid, nombre=nombre, backend_url=backend_url,
                                         usuario=usuario, password=password))
        print(f"Directorio: {n} teléfonos cargados desde el roster.")
    except Exception as ex:  # noqa: BLE001
        print(f"Aviso: no pude jalar el roster ahora ({ex}). Se reintenta al arrancar el asistente.")


if __name__ == "__main__":
    main()
