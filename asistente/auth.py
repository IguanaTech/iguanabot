"""Auth del bot contra cada back: login + refresh, con caché del accessToken por consorcio.

El back (fortuna-api) da un accessToken corto + refreshToken. El bot loguea una vez
(POST /api/auth/login/admin) con las credenciales del usuario de servicio (requiere_2fa=false →
token directo), cachea el accessToken hasta poco antes de expirar, y lo renueva con
POST /api/auth/refresh. Si el refresh falla, re-loguea.
"""
import threading
import time

import httpx

from .registry import Consorcio

# consorcio_id → {access, exp, refresh}
_cache: dict[str, dict] = {}
_lock = threading.Lock()
_MARGEN = 30  # renovar 30s antes de que expire


def _login(c: Consorcio) -> dict:
    r = httpx.post(
        f"{c.backend_url}/api/auth/login/admin",
        json={"nombre_usuario": c.usuario, "password": c.password},
        timeout=30,
    )
    r.raise_for_status()
    d = r.json()
    # Wire snake_case (verificado contra el back). Sin access_token → Branch A/B/C (cambio de clave o
    # 2FA) = cuenta mal configurada para un bot.
    if "access_token" not in d:
        raise RuntimeError(f"login del bot no directo (¿requiere_2fa o cambio de clave?): {list(d)}")
    return {
        "access": d["access_token"],
        "exp": time.time() + int(d.get("access_token_expires_in_sec", 900)) - _MARGEN,
        "refresh": d.get("refresh_token"),
    }


def _refresh(c: Consorcio, refresh_token: str) -> dict:
    r = httpx.post(f"{c.backend_url}/api/auth/refresh", json={"refresh_token": refresh_token}, timeout=30)
    r.raise_for_status()
    d = r.json()
    return {
        "access": d["access_token"],
        "exp": time.time() + int(d.get("access_token_expires_in_sec", 900)) - _MARGEN,
        "refresh": d.get("refresh_token", refresh_token),
    }


def token_de(c: Consorcio) -> str:
    """Devuelve un accessToken válido para llamar al back de este consorcio (loguea/renueva si hace falta)."""
    with _lock:
        e = _cache.get(c.id)
        if e and e["exp"] > time.time():
            return e["access"]
        # Vencido o inexistente: intentar refresh, si no, login.
        if e and e.get("refresh"):
            try:
                e = _refresh(c, e["refresh"])
                _cache[c.id] = e
                return e["access"]
            except Exception:  # noqa: BLE001 — refresh caducó/inválido → login limpio
                pass
        e = _login(c)
        _cache[c.id] = e
        return e["access"]
