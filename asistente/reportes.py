"""Reportes: 'cómo le fue al negocio'. Junta lecturas del back y arma un resumen (texto para
WhatsApp + PDF opcional). Sirve para el modo A-PEDIDO (cliente pide) y el AUTOMÁTICO (cierre del día).

Todo es LECTURA — un reporte no mueve nada. El usuario de servicio necesita, además de los 3 permisos
de solo-lectura, poder ver ventas/balance (REPORTES_VER_VENTAS, BALANCE_VER) para las cifras del día.
"""
from __future__ import annotations

from dataclasses import dataclass, field
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


def estado_dia_operativo(consorcio: Consorcio) -> dict | None:
    """Estado del cierre del día operativo del consorcio (LECTURA). Devuelve el dict del back
    {fecha, cerrado, razon, ...} o None si el back no respondió (se reintenta en el próximo sondeo).
    El disparo del reporte de cierre se ata a `cerrado=true` (última lotería publicó premios + margen),
    no a una hora fija."""
    try:
        return _get(consorcio, "/api/crm/dashboard/dia-operativo")
    except Exception as ex:  # noqa: BLE001
        print(f"[cierre] estado del día de {consorcio.nombre} falló: {ex}")
        return None


@dataclass
class Reporte:
    consorcio: str
    fecha: str
    datos: dict
    # Qué bloques NO se pudieron leer. Sin esta lista, un endpoint caído es
    # indistinguible de un día sin movimiento — ver `hubo_movimiento`.
    fallidos: list[str] = field(default_factory=list)


# Los bloques sin los cuales NO se puede afirmar que el día estuvo quieto.
CLAVES_DE_MOVIMIENTO = ("ventas", "ganadores")


def hubo_movimiento(rep: "Reporte") -> bool:
    """¿Este día tiene ALGO que contar?

    Regla de Eduardo (2026-07-30), y vale para todo lo que el bot manda solo: si no hay nada que
    reportar, no se manda. Un mensaje diario que a veces dice "no pasó nada" entrena a la gente a
    no abrirlo — y el día que trae algo importante, tampoco lo abre. El silencio es información:
    significa que no hubo movimiento.

    "Algo que contar" = se vendió, se pagó o quedó pendiente un premio. Si los tres están en cero,
    la banca no operó ese día y no hay reporte que mandar.

    PERO el silencio sólo es información si los números se pudieron LEER. Si `ventas` o `ganadores`
    fallaron, sus resúmenes vienen vacíos, los contadores dan cero y esta función concluía "no hubo
    movimiento" — o sea que un back caído se traducía en un día de silencio y Eduardo entendía que
    la operación estuvo quieta. Es el mismo modo de fallar que el `except: pass` del disponible y
    que el «no encontré ese ticket» cuando el que no responde es el sistema: un error mudo se lee
    como un dato. Si no se pudo mirar, se manda igual, y el texto lo dice.
    """
    if any(rep.datos.get(k) is None for k in CLAVES_DE_MOVIMIENTO):
        return True
    d = rep.datos or {}
    def _n(bloque, *claves):
        r = ((d.get(bloque) or {}).get("resumen") or {})
        for k in claves:
            try:
                if int(float(r.get(k) or 0)) != 0:
                    return True
            except (TypeError, ValueError):
                continue
        return False
    return (_n("ventas", "total_monto", "total_vendido", "total_tickets")
            or _n("ganadores", "total_premios", "premios_por_pagar", "total_pagados"))


def datos_dia(consorcio: Consorcio, dia: date | None = None) -> Reporte:
    dia = dia or date.today()
    d = dia.isoformat()
    # Endpoints verificados contra fortuna-api (claves reales del resumen, 2026-07-22):
    # ventas → resumen.total_monto ; pnl → resumen.resultado_operativo ;
    # ganadores → resumen.total_premios / premios_por_pagar / total_pagados. Una fecha pelada = día
    # dominicano completo (el back interpreta YYYY-MM-DD como 00:00–23:59 −04:00).
    datos = {
        "operativo":  _safe(consorcio, "/api/crm/dashboard/operativo"),
        "ventas":     _safe(consorcio, "/api/reportes/ventas", {"desde": d, "hasta": d}),
        "pnl":        _safe(consorcio, "/api/reportes/pnl", {"desde": d, "hasta": d}),
        "ganadores":  _safe(consorcio, "/api/reportes/ganadores", {"desde": d, "hasta": d}),
        "riesgo":     _safe(consorcio, "/api/crm/riesgo/cobertura"),
        "fraude":     _safe(consorcio, "/api/crm/fraude/score"),
    }
    # Un bloque que no se pudo leer se OMITE del texto (porque `_linea` descarta los None), y
    # omitido es indistinguible de "ese número no aplica hoy". Se anota para poder decirlo.
    return Reporte(consorcio=consorcio.nombre, fecha=d, datos=datos,
                   fallidos=[k for k, v in datos.items() if v is None])


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


def _entero(v) -> int:
    """Monto (string bigint del wire) como int. 0 si no se puede leer — nunca revienta el reporte."""
    try:
        return int(v)
    except (ValueError, TypeError):
        return 0


def venta_del_dia(rep: "Reporte") -> str:
    """La venta del día ya formateada, o «no la pude leer».

    La usa la señal de vida, que manda UN número. Si ese número no se pudo leer, se dice: una señal
    de vida que informa «Venta: RD$ 0» cuando en realidad no se pudo consultar es peor que no
    mandarla — afirma que no se vendió nada.
    """
    v = _resumen(rep.datos.get("ventas"), "total_monto", "monto")
    return _rd(v) if v is not None else "no la pude leer"


def texto(rep: Reporte) -> str:
    """Resumen para el cuerpo del mensaje de WhatsApp."""
    d = rep.datos
    lineas = [f"📊 *{rep.consorcio}* — cómo fue el {rep.fecha}", ""]
    op = d.get("operativo") or {}
    riesgo = d.get("riesgo") or {}
    fraude = d.get("fraude") or {}
    dic = (op.get("dinero_en_la_calle") or {}) if isinstance(op, dict) else {}
    for l in [
        # El resumen de /api/reportes/ventas expone `total_monto` (no `monto`): sin esto la línea de
        # ventas se caía SIEMPRE y el reporte de cierre parecía "sin ventas" aun con ventas reales.
        _linea("Ventas del día", _rd(_resumen(d.get("ventas"), "total_monto", "monto"))),
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

    # Lo que no se pudo leer se dice. Una línea que falta se lee como "ese número no aplica hoy";
    # peor todavía, un reporte con la mitad de los números se lee como el reporte completo, y con
    # eso se toman decisiones de plata.
    # ── QUÉ HAY PARA RETIRAR, banca por banca ────────────────────────────────────────────────
    #
    # Pedido de Eduardo (2026-08-04): «todas las bancas que tienen disponible para retirar deberían
    # decirlo». Antes el reporte traía sólo el TOTAL, y un total no sirve para decidir a dónde
    # mandar al mensajero — hay que saber cuál banca y cuánto.
    #
    # Va como LISTA y no como alarma por banca, también decisión suya: si cada una disparara su
    # aviso, a media tarde estarían sonando todas y se dejarían de mirar. Acá lo dice todo y no
    # molesta.
    #
    # SIN UMBRAL: se listan todas las que tengan algo. El dato ya venía en el operativo
    # (`bancas_en_calle`, el desglose del disponible positivo); lo único que faltaba era decirlo.
    detalle = (op.get("bancas_en_calle") or []) if isinstance(op, dict) else []
    conMonto = [b for b in detalle if _entero(b.get("disponible")) > 0]
    if conMonto:
        conMonto.sort(key=lambda b: _entero(b.get("disponible")), reverse=True)
        lineas.append("")
        lineas.append("💰 *Disponible para retirar*")
        for b in conMonto:
            lineas.append(f"   • {b.get('nombre') or 'Banca'} — {_rd(b.get('disponible'))}")
        total = sum(_entero(b.get("disponible")) for b in conMonto)
        if len(conMonto) > 1:
            lineas.append(f"   _Total: {_rd(total)} en {len(conMonto)} bancas_")

    if rep.fallidos:
        NOMBRES = {
            "operativo": "estado operativo", "ventas": "ventas", "pnl": "ganancia",
            "ganadores": "premios", "riesgo": "cobertura", "fraude": "señales de fraude",
        }
        faltan = ", ".join(NOMBRES.get(k, k) for k in rep.fallidos)
        lineas.append("")
        lineas.append(f"⚠️ No pude leer: {faltan}. Lo que falta arriba NO es cero — es que no lo vi.")

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


# ── Destinatarios del reporte automático ─────────────────────────────────────
#
# DE DÓNDE SALEN, Y POR QUÉ CAMBIÓ.
#
# Salían del `directorio`, que se sincroniza del ROSTER DE EMPLEADOS del back: quien tenga teléfono
# cargado en su ficha, entra. Eso tenía un problema que se cobró caro: el CRM tiene una pantalla
# —«contactos del asistente»— donde el admin dice explícitamente QUIÉN usa el bot, y esa lista NO
# era la que se usaba para mandar los reportes. Dos listas para lo mismo, y nadie las sincronizaba.
#
# El resultado real (2026-08-06): Eduardo estaba configurado como admin activo en la pantalla del
# CRM, su ficha de empleado no tenía teléfono, y el reporte salía sólo para un encargado a un número
# viejo — que además no estaba en Telegram, así que caía a WhatsApp, que llevaba un día caído. Él
# había configurado todo bien; la configuración simplemente no llegaba a ningún lado.
#
# Ahora la fuente es `GET /api/crm/asistente/contactos`: la lista que el admin administra y ve. Si
# el back no contesta, se cae al `directorio` de siempre — un reporte que no sale por un problema
# de red es peor que uno que sale a la lista vieja.
CONTACTOS_PATH = "/api/crm/asistente/contactos"


def _contactos_del_crm(consorcio, roles: set[str]) -> list[str] | None:
    """Teléfonos ACTIVOS de la lista que el admin administra en el CRM, filtrados por rol.
    Devuelve None si no se pudo consultar — el que llama decide si cae al directorio."""
    try:
        with httpx.Client(timeout=20) as client:
            r = client.get(consorcio.backend_url + CONTACTOS_PATH,
                           headers={"Authorization": f"Bearer {auth.token_de(consorcio)}"})
            r.raise_for_status()
            data = r.json()
        filas = data.get("data", data) if isinstance(data, dict) else data
    except Exception as ex:  # noqa: BLE001
        print(f"[destinatarios] no pude leer los contactos del CRM ({ex}); uso el directorio")
        return None
    out = []
    for c in filas or []:
        if c.get("activo") is False:
            continue
        if str(c.get("empleado_rol") or "") not in roles:
            continue
        tel = "".join(ch for ch in str(c.get("telefono") or "") if ch.isdigit())
        if len(tel) >= 10:
            out.append(tel)
    return out


def _del_directorio(consorcio_id: str, roles: set[str]) -> list[str]:
    with psycopg.connect(config.DATABASE_URL) as conn:
        rows = conn.execute(
            "SELECT telefono FROM directorio WHERE consorcio_id = %s AND rol = ANY(%s)",
            (consorcio_id, list(roles)),
        ).fetchall()
    return [r[0] for r in rows]


def destinatarios(consorcio_id: str, consorcio=None) -> list[str]:
    """Quienes reciben el reporte del cierre: admin/encargado."""
    roles = {"admin", "encargado"}
    if consorcio is not None:
        del_crm = _contactos_del_crm(consorcio, roles)
        if del_crm:
            return del_crm
    return _del_directorio(consorcio_id, roles)


def destinatarios_alarma(consorcio_id: str, consorcio=None) -> list[str]:
    """Quienes reciben ALARMAS DE DINERO: sólo roles GLOBALES (admin/contable).
    Se excluye al encargado a propósito: es banca-acotado y una alarma trae la banca/exposición de
    cualquier banca del consorcio (le filtraría bancas ajenas). Las alarmas son señal a nivel dueño."""
    roles = {"admin", "contable"}
    if consorcio is not None:
        del_crm = _contactos_del_crm(consorcio, roles)
        if del_crm:
            return del_crm
    return _del_directorio(consorcio_id, roles)
