"""Reportes: 'cómo le fue al negocio'. Junta lecturas del back y arma un resumen (texto para
WhatsApp + PDF opcional). Sirve para el modo A-PEDIDO (cliente pide) y el AUTOMÁTICO (cierre del día).

Todo es LECTURA — un reporte no mueve nada. El usuario de servicio necesita, además de los 3 permisos
de solo-lectura, poder ver ventas/balance (REPORTES_VER_VENTAS, BALANCE_VER) para las cifras del día.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import httpx
import psycopg

from . import auth
from .config import config
from .registry import Consorcio


# ── Buffer de adjuntos pendientes (modo a-pedido) ─────────────────────────────
# Cuando el LLM llama la herramienta de reporte, deja el PDF acá bajo el teléfono; app.py lo adjunta
# a la respuesta que va al puente. Simple y suficiente para un proceso; si se escala, mover a la DB.
_pendientes: dict[str, tuple[str, bytes]] = {}


def stash_pendiente(telefono: str, nombre_archivo: str, pdf: bytes) -> None:
    _pendientes[telefono] = (nombre_archivo, pdf)


def tomar_pendiente(telefono: str) -> tuple[str, bytes] | None:
    return _pendientes.pop(telefono, None)


# ── Datos del día ─────────────────────────────────────────────────────────────
def _get(consorcio: Consorcio, path: str, params: dict | None = None):
    url = consorcio.backend_url + path
    headers = {"Authorization": f"Bearer {auth.token_de(consorcio)}"}
    with httpx.Client(timeout=30) as client:
        r = client.get(url, headers=headers, params=params or {})
        r.raise_for_status()
        return r.json()


def _safe(consorcio, path, params=None):
    """Cada lectura es best-effort: si un endpoint falla, el reporte sigue con lo que sí trajo."""
    try:
        return _get(consorcio, path, params)
    except Exception as ex:  # noqa: BLE001
        print(f"[reporte] {path} falló: {ex}")
        return None


@dataclass
class Reporte:
    consorcio: str
    fecha: str
    datos: dict


def datos_dia(consorcio: Consorcio, dia: date | None = None) -> Reporte:
    dia = dia or date.today()
    d = dia.isoformat()
    # Endpoints verificados contra fortuna-api (2026-07-17). Rango del día: desde/hasta = d.
    # ventas → resumen.monto ; pnl → resumen.resultado_operativo ; ganadores → resumen.totalPremios.
    datos = {
        "operativo":  _safe(consorcio, "/api/crm/dashboard/operativo"),
        "ventas":     _safe(consorcio, "/api/reportes/ventas", {"desde": d, "hasta": d}),
        "pnl":        _safe(consorcio, "/api/reportes/pnl", {"desde": d, "hasta": d}),
        "ganadores":  _safe(consorcio, "/api/reportes/ganadores", {"desde": d, "hasta": d}),
        "riesgo":     _safe(consorcio, "/api/crm/riesgo/cobertura"),
        "fraude":     _safe(consorcio, "/api/crm/fraude/score"),
    }
    return Reporte(consorcio=consorcio.nombre, fecha=d, datos=datos)


# ── Render ────────────────────────────────────────────────────────────────────
def _linea(etiqueta, valor):
    return f"• {etiqueta}: {valor}" if valor is not None else None


def _resumen(bloque, *claves):
    """Saca un valor del `resumen` de un reporte del back, tolerante a None/forma."""
    if not isinstance(bloque, dict):
        return None
    r = bloque.get("resumen")
    if not isinstance(r, dict):
        return None
    for k in claves:
        if r.get(k) is not None:
            return r[k]
    return None


def _rd(v) -> str | None:
    """Formatea un monto (string bigint) como RD$ con separador de miles."""
    if v is None:
        return None
    try:
        return f"RD$ {int(v):,}".replace(",", ".")
    except (ValueError, TypeError):
        return str(v)


def texto(rep: Reporte) -> str:
    """Resumen para el cuerpo del mensaje de WhatsApp."""
    d = rep.datos
    lineas = [f"📊 *{rep.consorcio}* — cómo fue el {rep.fecha}", ""]
    op = d.get("operativo") or {}
    riesgo = d.get("riesgo") or {}
    fraude = d.get("fraude") or {}
    dic = (op.get("dinero_en_la_calle") or {}) if isinstance(op, dict) else {}
    for l in [
        _linea("Ventas del día", _rd(_resumen(d.get("ventas"), "monto"))),
        _linea("Ganancia (resultado)", _rd(_resumen(d.get("pnl"), "resultado_operativo"))),
        # El wire del back es snake_case (deepSnake global): total_premios, premios_por_pagar.
        _linea("Premios (total)", _rd(_resumen(d.get("ganadores"), "total_premios"))),
        _linea("Premios por pagar", _rd(_resumen(d.get("ganadores"), "premios_por_pagar"))),
        _linea("Tickets ganadores pagados", _resumen(d.get("ganadores"), "total_pagados")),
        _linea("Bancas abiertas", len(op.get("bancas_abiertas") or []) if isinstance(op, dict) else None),
        _linea("Dinero en la operación (calle+bancas)", _rd(dic.get("total"))),
        _linea("Disponible para retirar (bancas)", _rd(dic.get("bancas_disponible"))),
        _linea("En mano de mensajeros", _rd(dic.get("mensajeros"))),
        _linea("Bancas descubiertas (riesgo)", len(riesgo.get("cobertura", [])) if isinstance(riesgo, dict) else None),
        _linea("Bancas a revisar (fraude)", len(fraude.get("bancas", [])) if isinstance(fraude, dict) else None),
    ]:
        if l:
            lineas.append(l)
    lineas.append("")
    lineas.append("_Reporte generado por el asistente. Solo lectura._")
    return "\n".join(lineas)


def pdf_de_texto(titulo: str, cuerpo: str) -> bytes | None:
    """PDF a demanda de una respuesta cualquiera del bot (HTML→PDF con WeasyPrint). Limpia el markdown
    y respeta los saltos de línea. None si WeasyPrint no está o falla."""
    try:
        from weasyprint import HTML
    except Exception as ex:  # noqa: BLE001
        print(f"[reporte] WeasyPrint no disponible ({ex})")
        return None
    import html as _html
    limpio = _html.escape((cuerpo or "").replace("*", "").replace("_", "").replace("`", "").replace("#", ""))
    doc = f"""
    <html><head><meta charset="utf-8"><style>
      body {{ font-family: Helvetica, Arial, sans-serif; color:#16202c; padding:32px; }}
      h1 {{ color:#1E3AE2; font-size:19px; margin:0 0 4px; }}
      .fecha {{ color:#5a6876; font-size:12px; margin-bottom:18px; }}
      pre {{ font-family: inherit; font-size:14px; line-height:1.6; white-space:pre-wrap; }}
      .pie {{ margin-top:24px; color:#8a97a4; font-size:11px; border-top:1px solid #e4e8ee; padding-top:10px; }}
    </style></head><body>
      <h1>{_html.escape(titulo or 'Consulta')}</h1>
      <div class="fecha">{date.today().isoformat()}</div>
      <pre>{limpio}</pre>
      <div class="pie">IguanaSuite · asistente · generado a pedido · solo lectura</div>
    </body></html>"""
    return HTML(string=doc).write_pdf()


def pdf(rep: Reporte) -> bytes | None:
    """Renderiza el reporte a PDF (HTML→PDF con WeasyPrint). None si el PDF está deshabilitado o falla."""
    if not config.REPORTE_PDF:
        return None
    try:
        from weasyprint import HTML  # import perezoso; requiere libs del sistema (ver Dockerfile)
    except Exception as ex:  # noqa: BLE001
        print(f"[reporte] WeasyPrint no disponible ({ex}); se manda solo texto")
        return None
    cuerpo = texto(rep).replace("*", "").replace("_", "")
    html = f"""
    <html><head><meta charset="utf-8"><style>
      body {{ font-family: Helvetica, Arial, sans-serif; color:#16202c; padding:32px; }}
      h1 {{ color:#1E3AE2; font-size:20px; margin:0 0 4px; }}
      .fecha {{ color:#5a6876; font-size:13px; margin-bottom:18px; }}
      pre {{ font-family: inherit; font-size:14px; line-height:1.6; white-space:pre-wrap; }}
      .pie {{ margin-top:24px; color:#8a97a4; font-size:11px; border-top:1px solid #e4e8ee; padding-top:10px; }}
    </style></head><body>
      <h1>{rep.consorcio}</h1>
      <div class="fecha">Reporte del día · {rep.fecha}</div>
      <pre>{cuerpo}</pre>
      <div class="pie">IguanaSuite · asistente · generado automáticamente · solo lectura</div>
    </body></html>"""
    return HTML(string=html).write_pdf()


# ── Destinatarios del reporte automático ──────────────────────────────────────
def destinatarios(consorcio_id: str) -> list[str]:
    """Teléfonos de quienes reciben el reporte del cierre: admin/encargado del consorcio."""
    with psycopg.connect(config.DATABASE_URL) as conn:
        rows = conn.execute(
            "SELECT telefono FROM directorio WHERE consorcio_id = %s AND rol IN ('admin','encargado')",
            (consorcio_id,),
        ).fetchall()
    return [r[0] for r in rows]


def destinatarios_alarma(consorcio_id: str) -> list[str]:
    """Teléfonos que reciben ALARMAS DE DINERO de ESTE consorcio: solo roles GLOBALES (admin/contable).
    Se excluye al encargado a propósito: es banca-acotado y una alarma trae la banca/exposición de
    cualquier banca del consorcio (le filtraría bancas ajenas). Las alarmas son señal a nivel dueño."""
    with psycopg.connect(config.DATABASE_URL) as conn:
        rows = conn.execute(
            "SELECT telefono FROM directorio WHERE consorcio_id = %s AND rol IN ('admin','contable')",
            (consorcio_id,),
        ).fetchall()
    return [r[0] for r in rows]
