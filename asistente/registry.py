"""Registro de consorcios: dónde vive cada back y con qué CREDENCIAL entra el bot.

El back emite accessToken corto + refreshToken (no hay token durable). Por eso guardamos las
credenciales del usuario de servicio (usuario + password) y el asistente hace login/refresh (ver
auth.py). La contraseña se guarda CIFRADA (Fernet con ASISTENTE_SECRET_KEY) y se descifra solo en
memoria al momento de loguear. Nunca en claro en reposo.
"""
from dataclasses import dataclass
from functools import lru_cache

import psycopg
from cryptography.fernet import Fernet

from .config import config


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    if not config.SECRET_KEY:
        raise RuntimeError("ASISTENTE_SECRET_KEY no configurada — no puedo descifrar credenciales")
    return Fernet(config.SECRET_KEY.encode())


def cifrar(claro: str) -> str:
    """Para dar de alta un consorcio: cifra la contraseña del bot antes de guardarla."""
    return _fernet().encrypt(claro.encode()).decode()


@dataclass
class Consorcio:
    id: str
    nombre: str
    backend_url: str
    usuario: str
    password: str  # ya descifrada, lista para el login


def consorcio_por_id(consorcio_id: str) -> Consorcio | None:
    with psycopg.connect(config.DATABASE_URL) as conn:
        row = conn.execute(
            "SELECT id, nombre, backend_url, usuario, password_cifrado FROM consorcios "
            "WHERE id = %s AND activo = true",
            (consorcio_id,),
        ).fetchone()
    if not row:
        return None
    password = _fernet().decrypt(row[4].encode()).decode()
    return Consorcio(id=str(row[0]), nombre=row[1], backend_url=row[2].rstrip("/"),
                     usuario=row[3], password=password)


def listar_consorcios() -> list[Consorcio]:
    """Sin descifrar la contraseña (para tareas que solo necesitan id/nombre/url)."""
    with psycopg.connect(config.DATABASE_URL) as conn:
        rows = conn.execute(
            "SELECT id, nombre, backend_url, usuario FROM consorcios WHERE activo = true ORDER BY nombre"
        ).fetchall()
    return [Consorcio(id=str(r[0]), nombre=r[1], backend_url=r[2].rstrip("/"), usuario=r[3], password="")
            for r in rows]
