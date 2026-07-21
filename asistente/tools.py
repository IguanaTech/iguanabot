"""Herramientas del asistente (Fase 0: SOLO LECTURA).

Cada tool llama un endpoint del back del consorcio con el token de servicio. Se construyen
POR CONSORCIO (closure): así el LLM tiene herramientas ya apuntadas al back correcto y no puede
equivocarse de consorcio. Disciplina de datos: devolvemos resúmenes/agregados, no volcados crudos
(controla costo de tokens y no filtra detalle sensible al modelo).

Endpoints usados (todos read-only, gateados por permisos de solo-lectura del usuario de servicio):
  GET /api/crm/dashboard/operativo         — centro de operaciones: dinero total (calle+bancas), alertas
  GET /api/crm/dashboard/prediccion-deficit — bancas que se prevé queden cortas hoy   [feature #1]
  GET /api/crm/riesgo/cobertura            — bancas descubiertas + tope sugerido        [feature #3]
  GET /api/crm/riesgo/exposicion           — ventas por número/lotería (número más vendido)
  GET /api/crm/fraude/score                — a quién mirar primero (score de fraude)    [feature #4]
  GET /api/reportes/ganadores              — premios pagados/por pagar (cuánto nos han sacado)
  GET /api/crm/dashboard/alertas/historial — alertas del día

NOTA de wire: el back serializa TODO en snake_case (deepSnake global), aun cuando el controlador
use camelCase internamente. Por eso acá se leen claves snake (total_premios, no totalPremios).
"""
import math
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx
from langchain_core.tools import StructuredTool

from . import auth, reportes
from .registry import Consorcio


def _get(consorcio: Consorcio, path: str, params: dict | None = None) -> Any:
    url = consorcio.backend_url + path
    headers = {"Authorization": f"Bearer {auth.token_de(consorcio)}"}
    with httpx.Client(timeout=30) as client:
        resp = client.get(url, headers=headers, params=params or {})
        resp.raise_for_status()
        return resp.json()


def _rd(v) -> str:
    """Monto → 'RD$ 137.925' (punto de miles, estilo dominicano). Acepta bigint ('137925') o
    decimal ('3300.00'); redondea a peso entero."""
    if v is None:
        return "RD$ 0"
    try:
        return f"RD$ {int(round(float(v))):,}".replace(",", ".")
    except (ValueError, TypeError):
        return f"RD$ {v}"


def _dist_km(lat1, lng1, lat2, lng2) -> float | None:
    """Distancia en km entre dos puntos (Haversine). None si falta alguna coordenada."""
    try:
        r = 6371.0
        p1, p2 = math.radians(float(lat1)), math.radians(float(lat2))
        dphi = math.radians(float(lat2) - float(lat1))
        dl = math.radians(float(lng2) - float(lng1))
        a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    except (ValueError, TypeError):
        return None


def _hace(ts) -> str:
    """Timestamp ISO → 'hace N min' / 'hace N h M min' (para frescura del GPS)."""
    if not ts:
        return "sin dato"
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        secs = (datetime.now(timezone.utc) - t).total_seconds()
        if secs < 90:
            return "hace segundos"
        m = int(secs // 60)
        return f"hace {m} min" if m < 60 else f"hace {m // 60} h {m % 60} min"
    except (ValueError, TypeError):
        return str(ts)


def _rango(desde: str, hasta: str) -> tuple[str, str]:
    """Normaliza el rango de fechas. Vacío = hoy. Solo `desde` = desde..hoy. Formato AAAA-MM-DD."""
    hoy = date.today().isoformat()
    d = (desde or "").strip() or hoy
    h = (hasta or "").strip() or hoy
    return d, h


def _catalogo(consorcio: Consorcio, path: str, q: str = "", extra: dict | None = None) -> list[dict]:
    """Lista un catálogo (bancas/loterías/empleados/estaciones). Devuelve el array `data`."""
    params = {"limit": 500}
    if q:
        params["q"] = q
    if extra:
        params.update(extra)
    d = _get(consorcio, path, params)
    return d.get("data", d) if isinstance(d, dict) else (d or [])


def _resolver(consorcio: Consorcio, path: str, nombre: str, campo: str = "nombre",
              extra: dict | None = None) -> tuple[dict | None, list[str]]:
    """Traduce un NOMBRE a su registro (con id), TOLERANTE a apellidos mal oídos o parciales.
    Estrategia: (1) exacto/substring del nombre completo; (2) por TOKENS (nombre + apellido): reúne
    candidatos buscando cada palabra y puntúa por cuántas aparecen. Si queda UN SOLO candidato ganador,
    lo usa aunque el apellido no calce exacto (ej.: una sola 'Rosita' → no pide corregir el apellido).
    Solo devuelve None cuando hay EMPATE real (dos personas igual de parecidas). El 2º valor es la
    lista de nombres para sugerir en caso de ambigüedad."""
    n = (nombre or "").strip().lower()
    if not n:
        return None, []

    def _txt(it: dict) -> str:
        # Texto buscable: nombre + apodo/abreviatura + código (así "GH"/"la nacional"/"LDS" matchean).
        partes = [it.get(campo), it.get("nombre_corto"), it.get("codigo")]
        return " ".join(str(p) for p in partes if p).strip().lower()

    base = _catalogo(consorcio, path, q=nombre, extra=extra)
    for it in base:  # match exacto (nombre o apodo/código)
        if n == str(it.get(campo) or "").strip().lower() or n == str(it.get("nombre_corto") or "").strip().lower() \
           or n == str(it.get("codigo") or "").strip().lower():
            return it, [str(x.get(campo) or "") for x in base if x.get(campo)]
    for it in base:  # el nombre completo como substring
        if n in _txt(it):
            return it, [str(x.get(campo) or "") for x in base if x.get(campo)]

    # Por tokens: buscar por cada palabra (>=3 letras) y reunir candidatos (dedup por id).
    tokens = [t for t in re.split(r"[^0-9a-záéíóúñü]+", n) if len(t) >= 3]
    cand: dict[str, dict] = {}
    for tok in tokens:
        for it in _catalogo(consorcio, path, q=tok, extra=extra):
            cand[str(it.get("id"))] = it
    if not cand:  # nada; traer todo para poder sugerir
        for it in _catalogo(consorcio, path, extra=extra):
            cand[str(it.get("id"))] = it

    def _score(it: dict) -> int:
        nm = _txt(it)
        return sum(1 for t in tokens if t in nm)

    puntuados = sorted(((_score(it), it) for it in cand.values()), key=lambda x: x[0], reverse=True)
    con_match = [(s, it) for s, it in puntuados if s > 0]
    if not con_match:
        return None, [str(it.get(campo) or "") for it in cand.values() if it.get(campo)][:30]
    mejor = con_match[0][0]
    ganadores = [it for s, it in con_match if s == mejor]
    if len(ganadores) == 1:  # un solo mejor candidato → úsalo (aunque el apellido no calce)
        return ganadores[0], [str(it.get(campo) or "") for it in ganadores]
    return None, [str(it.get(campo) or "") for it in ganadores][:30]  # empate real → sugerir


def construir_tools(consorcio: Consorcio, telefono: str, identidad=None) -> list[StructuredTool]:
    """Devuelve las herramientas de solo-lectura ya atadas al back de ESTE consorcio.
    `telefono` = quién escribe (para adjuntar el PDF del reporte). `identidad` define el ALCANCE:
    si el usuario NO es global (un encargado), solo recibe herramientas POR-BANCA y acotadas a SUS
    bancas — no ve totales del consorcio ni bancas ajenas (segmentación, Fase B)."""
    es_global = identidad is None or getattr(identidad, "es_global", True)
    mis_bancas = set(getattr(identidad, "banca_ids", None) or []) if not es_global else None

    def _banca_permitida(banca_id) -> bool:
        return es_global or str(banca_id) in mis_bancas

    def _mis_bancas_info() -> list[dict]:
        # Nombres de las bancas del usuario acotado (para pedir/sugerir cuál).
        if es_global:
            return []
        return [b for b in _catalogo(consorcio, "/api/bancas") if str(b.get("id")) in mis_bancas]

    def _exigir_banca(banca: str):
        """Para usuarios acotados: resuelve la banca pedida y verifica que sea suya. Devuelve
        (banca|None, mensaje_error|None). Si no piden banca y tienen una sola, usa esa."""
        info = _mis_bancas_info()
        nombres = ", ".join(str(b.get("nombre")) for b in info) or "—"
        if not banca:
            if len(info) == 1:
                return info[0], None
            return None, f"¿De cuál de tus bancas? Tienes: {nombres}."
        b, _ = _resolver(consorcio, "/api/bancas", banca)
        if not b:
            return None, f"No encontré esa banca entre las tuyas. Tus bancas: {nombres}."
        if not _banca_permitida(b.get("id")):
            return None, f"Esa banca no es tuya. Solo puedo darte lo de tus bancas: {nombres}."
        return b, None

    def operativo() -> str:
        """Foto AHORA de la operación y del DINERO completo. Úsalo para: '¿cómo va la operación?',
        '¿cuánto dinero hay?', '¿cuánta plata tenemos en la calle?', '¿cuánto efectivo hay afuera?',
        'resumen de ahora', 'estado del negocio'. Da el dinero TOTAL en la operación = lo que tienen
        los mensajeros en mano MÁS lo disponible en las bancas (no solo el mensajero)."""
        d = _get(consorcio, "/api/crm/dashboard/operativo")
        dic = d.get("dinero_en_la_calle") or {}
        mens = d.get("mensajeros_en_calle") or []
        n_mens = len(mens)
        partes = [
            f"Dinero total en la operación: {_rd(dic.get('total'))} "
            f"(en mano de mensajeros {_rd(dic.get('mensajeros'))} + disponible en bancas "
            f"{_rd(dic.get('bancas_disponible'))}).",
            f"Mensajeros activos: {n_mens}"
            + (": " + ", ".join(f"{m.get('mensajero_nombre')} {_rd(m.get('saldo'))} ({m.get('modo')})"
                                for m in mens[:8]) if n_mens else "."),
            f"Bancas abiertas: {len(d.get('bancas_abiertas') or [])}.",
        ]
        en_perdida = dic.get("bancas_en_perdida") or 0
        if en_perdida:
            partes.append(f"Ojo: {en_perdida} banca(s) en pérdida, hueco {_rd(dic.get('en_perdida'))}.")
        al = d.get("alertas")
        n_al = len(al) if isinstance(al, list) else (sum(len(v) for v in al.values() if isinstance(v, list)) if isinstance(al, dict) else 0)
        partes.append(f"Alertas vivas: {n_al}.")
        return " ".join(partes)

    def disponible_para_retirar() -> str:
        """Cuánto se puede RETIRAR/SACAR/RECOGER de las bancas ahora mismo (el excedente extraíble
        de cada banca, sin descapitalizarla). Úsalo para: '¿cuánto tenemos disponible para retirar?',
        '¿cuánto podemos sacar de las bancas?', '¿cuánta plata hay para recoger?', 'efectivo retirable',
        '¿cuánto hay en las bancas?'. Da el total y el desglose por banca."""
        d = _get(consorcio, "/api/crm/dashboard/operativo")
        dic = d.get("dinero_en_la_calle") or {}
        bancas = d.get("bancas_en_calle") or []
        if not bancas:
            return f"Disponible para retirar en bancas: {_rd(dic.get('bancas_disponible'))} (sin desglose por banca ahora)."
        desglose = "; ".join(f"{b.get('nombre')}: {_rd(b.get('disponible'))}"
                             for b in sorted(bancas, key=lambda x: int(x.get('disponible') or 0), reverse=True)[:15])
        extra = ""
        if dic.get("bancas_en_perdida"):
            extra = f" (además {dic.get('bancas_en_perdida')} banca(s) en pérdida, hueco {_rd(dic.get('en_perdida'))})"
        return f"Disponible para retirar en bancas: {_rd(dic.get('bancas_disponible'))}{extra}. Por banca — {desglose}."

    def premios_pagados(dias: int = 1) -> str:
        """Cuánto dinero ha SALIDO en premios (lo que 'nos han sacado' los ganadores). Úsalo para:
        '¿cuánto nos han sacado?', '¿cuánto hemos pagado en premios?', '¿cuánto ganaron los clientes?',
        '¿cuánto salió en premios?'. `dias` = ventana hacia atrás desde hoy (1 = solo hoy, 7 = última
        semana, 30 = último mes). Distingue lo ya PAGADO de lo que está POR PAGAR."""
        hasta = date.today()
        desde = hasta - timedelta(days=max(1, dias) - 1)
        d = _get(consorcio, "/api/reportes/ganadores",
                 {"desde": desde.isoformat(), "hasta": hasta.isoformat()})
        r = d.get("resumen") or {}
        total = int(r.get("total_premios") or 0)          # pagado + pendiente (en la ventana)
        por_pagar = int(r.get("premios_por_pagar") or 0)  # pendiente
        pagado = max(0, total - por_pagar)
        rango = "hoy" if dias <= 1 else f"últimos {dias} días"
        return (
            f"Premios {rango}: PAGADO {_rd(pagado)} ({r.get('total_pagados', 0)} tickets); "
            f"por pagar {_rd(por_pagar)} ({r.get('total_pendientes', 0)} tickets). "
            f"Total en premios {_rd(total)}."
        )

    def numero_mas_vendido() -> str:
        """El/los número(s) MÁS VENDIDO(S) hoy, DESGLOSADO POR LOTERÍA. Úsalo para: '¿cuál es el número
        más vendido?', '¿qué número se está jugando más?', '¿cuál número entra más?', 'top de números',
        'número más jugado por lotería'. Distinto de 'número caliente' (eso es dispersión sospechosa en
        varias bancas); esto es sencillamente lo que más se ha vendido por lotería."""
        d = _get(consorcio, "/api/crm/riesgo/exposicion")
        filas = d.get("exposicion") or []
        if not filas:
            return "Todavía no hay ventas registradas hoy para rankear números."
        # Agrupar por lotería y quedarnos con el número de mayor 'consumido' (ventas) de cada una.
        top_por_loteria: dict[str, dict] = {}
        for f in filas:
            lot = f.get("loteria_nombre") or "—"
            cons = int(f.get("consumido") or 0)
            if lot not in top_por_loteria or cons > int(top_por_loteria[lot].get("consumido") or 0):
                top_por_loteria[lot] = f
        orden = sorted(top_por_loteria.items(), key=lambda kv: int(kv[1].get("consumido") or 0), reverse=True)
        return "Número más vendido por lotería — " + "; ".join(
            f"{lot}: {f.get('numero')} ({f.get('tipo_jugada')}) {_rd(f.get('consumido'))}"
            for lot, f in orden[:12]
        )

    def prediccion_deficit() -> str:
        """Bancas que se prevé queden CORTAS de efectivo hoy (anticipa el despacho). Devuelve
        cada banca con la hora estimada del déficit y el faltante."""
        d = _get(consorcio, "/api/crm/dashboard/prediccion-deficit")
        bancas = d.get("bancas", [])
        if not bancas:
            return "Ninguna banca se prevé corta hoy."
        return "; ".join(
            f"{b.get('nombre')}: corta ~{b.get('hora_deficit')} (falta {b.get('faltante_estimado')}, "
            f"confianza {b.get('confianza')})"
            for b in bancas[:10]
        )

    def cobertura_riesgo() -> str:
        """Bancas DESCUBIERTAS: si sale su número más jugado, no lo pagan con su caja. Devuelve
        la banca, cuánto pagaría, su caja, y el cupo sugerido."""
        d = _get(consorcio, "/api/crm/riesgo/cobertura")
        filas = d.get("cobertura", [])
        if not filas:
            return "Ninguna banca descubierta ahora."
        return "; ".join(
            f"{f.get('banca_nombre')} #{f.get('numero')}: si sale paga {f.get('exposicion_pago')}, "
            f"caja {f.get('caja')}, cupo sugerido {f.get('tope_sugerido')}"
            for f in filas[:10]
        )

    def numeros_calientes() -> str:
        """Números CALIENTES ahora: la misma jugada apareciendo en varias bancas en poco tiempo
        (posible dato filtrado / dinero inteligente). Úsalo para '¿hay algún número caliente?',
        '¿algún número sospechoso?', '¿están jugando raro algún número?'. NO es lo mismo que 'el más
        vendido' (para eso está la herramienta de número más vendido); esto es una señal de riesgo."""
        d = _get(consorcio, "/api/crm/riesgo/velocidad", {"ventana_min": 60})
        cal = d.get("calientes", [])
        if not cal:
            return "Ningún número caliente ahora."
        return "; ".join(
            f"{c.get('numero')} ({c.get('loteria_nombre')} {c.get('tipo_jugada')}): {c.get('bancas')} bancas, "
            f"RD$ {c.get('consumido')} [{c.get('nivel')}]"
            for c in cal[:10]
        )

    def score_fraude() -> str:
        """A quién mirar primero por riesgo de fraude: score 0-100 por banca (goteo de caja chica +
        anulaciones + sesgo de arqueo). Cola de revisión, no acusación."""
        d = _get(consorcio, "/api/crm/fraude/score")
        bancas = d.get("bancas", [])
        if not bancas:
            return "Sin bancas con señales de fraude ahora."
        return "; ".join(
            f"{b.get('banca_nombre')} score {b.get('score')} ({b.get('nivel')}), a mirar: {b.get('quien')}"
            for b in bancas[:8]
        )

    def alertas_del_dia() -> str:
        """Alertas del panel de operaciones del día de hoy."""
        d = _get(consorcio, "/api/crm/dashboard/alertas/historial", params={})
        alertas = d.get("data", d) if isinstance(d, dict) else d
        n = len(alertas) if isinstance(alertas, list) else 0
        return f"{n} alertas registradas hoy." if n else "Sin alertas hoy."

    def reporte_del_dia() -> str:
        """Arma el REPORTE del día del negocio (ventas, ganancia, bancas, riesgo, fraude) y se lo
        entrega al cliente. Úsalo cuando pidan 'el reporte', 'cómo fue el día', 'resumen de hoy'.
        Si el PDF está activo, queda adjunto a la respuesta; devuelve el resumen en texto."""
        rep = reportes.datos_dia(consorcio)
        pdf = reportes.pdf(rep)
        if pdf:
            reportes.stash_pendiente(telefono, f"reporte-{rep.fecha}.pdf", pdf)
        return reportes.texto(rep)

    # ── Consultas FILTRADAS (mapa completo): reportes con banca/encargado/lotería/fechas ─────────
    _CATALOGOS = {
        "bancas":     ("/api/bancas", "nombre", None),
        "loterias":   ("/api/loterias", "nombre", None),
        "estaciones": ("/api/estaciones", "nombre", None),
        "encargados": ("/api/empleados", "nombre_real", {"rol": "encargado"}),
        "mensajeros": ("/api/empleados", "nombre_real", {"rol": "mensajero"}),
        "vendedores": ("/api/empleados", "nombre_real", {"rol": "vendedor"}),
    }

    def catalogo(que: str) -> str:
        """Lista los NOMBRES de un catálogo del negocio, para saber qué existe y por qué se puede
        filtrar/preguntar. `que` ∈ bancas | loterias | estaciones | encargados | mensajeros |
        vendedores. Úsalo si te preguntan '¿qué bancas hay?', '¿cuáles son los encargados?', o si no
        reconoces un nombre que te dieron y quieres ofrecer los que sí existen."""
        key = (que or "").strip().lower()
        if key not in _CATALOGOS:
            return f"No conozco el catálogo '{que}'. Disponibles: {', '.join(_CATALOGOS)}."
        if not es_global:  # acotado: solo su propia lista de bancas
            if key == "bancas":
                nombres = sorted(str(b.get("nombre")) for b in _mis_bancas_info() if b.get("nombre"))
                return "Tus bancas: " + (", ".join(nombres) or "—") + "."
            return "Con tu alcance solo puedo listarte tus bancas."
        path, campo, extra = _CATALOGOS[key]
        items = _catalogo(consorcio, path, extra=extra)
        nombres = [str(it.get(campo) or "") for it in items if it.get(campo)]
        if not nombres:
            return f"No hay {key} registrados."
        return f"{key.capitalize()} ({len(nombres)}): " + ", ".join(sorted(nombres)[:60])

    def reporte_ventas(desde: str = "", hasta: str = "", banca: str = "", loteria: str = "",
                       vendedor: str = "") -> str:
        """Ventas en un rango, con filtros opcionales por BANCA, LOTERÍA y/o VENDEDOR (por nombre).
        Úsalo para '¿cuánto vendió la banca X?', '¿cuánto vendió Rosita?', '¿cuánto se vendió este mes?',
        'ventas por lotería'. Devuelve el total, el # de tickets y el desglose por lotería y por
        modalidad (quiniela/palé/tripleta/SP)."""
        d, h = _rango(desde, hasta)
        params = {"desde": d, "hasta": h}
        etiqueta = f"{d} a {h}"
        if not es_global:  # acotado: banca obligatoria y debe ser suya
            b, err = _exigir_banca(banca)
            if err:
                return err
            params["banca_id"] = b["id"]; etiqueta += f", banca {b.get('nombre')}"
        elif banca:
            b, disp = _resolver(consorcio, "/api/bancas", banca)
            if not b:
                return f"No encontré una banca llamada '{banca}'. Bancas: {', '.join(disp[:30]) or '—'}."
            params["banca_id"] = b["id"]; etiqueta += f", banca {b.get('nombre')}"
        if vendedor:
            v, disp = _resolver(consorcio, "/api/empleados", vendedor, campo="nombre_real")
            if not v:
                # Usuario acotado: NO volcar la nómina completa (fuga de nombres); global sí la lista.
                if not es_global:
                    return f"No encontré un vendedor llamado '{vendedor}'."
                return f"No encontré un vendedor llamado '{vendedor}'. Empleados: {', '.join(disp[:20]) or '—'}."
            params["empleado_id"] = v["id"]; etiqueta += f", vendedor {v.get('nombre_real')}"
        if loteria:
            lot, disp = _resolver(consorcio, "/api/loterias", loteria)
            if not lot:
                return f"No encontré una lotería llamada '{loteria}'. Loterías: {', '.join(disp[:30]) or '—'}."
            params["loteria_id"] = lot["id"]; etiqueta += f", lotería {lot.get('nombre')}"
        r = (_get(consorcio, "/api/reportes/ventas", params) or {}).get("resumen") or {}
        out = [f"Ventas ({etiqueta}): {_rd(r.get('total_monto'))} en {r.get('total_tickets', 0)} tickets."]
        por_lot = r.get("por_loteria") or []
        if por_lot and not loteria:
            out.append("Por lotería — " + "; ".join(f"{x.get('loteria')}: {_rd(x.get('monto'))}" for x in por_lot[:8]))
        por_tipo = r.get("por_tipo") or []
        if por_tipo:
            out.append("Por modalidad — " + "; ".join(f"{x.get('tipo')}: {_rd(x.get('monto'))}" for x in por_tipo))
        return " ".join(out)

    def reporte_ganancia(desde: str = "", hasta: str = "", banca: str = "") -> str:
        """P&L / RENTABILIDAD del negocio (o de una banca) en un rango: ventas − premios pagados −
        gastos = resultado. Úsalo para '¿cuánto ganamos?', '¿cómo va la rentabilidad?', '¿la banca X
        deja o pierde?'. Opcional filtrar por BANCA (por nombre)."""
        d, h = _rango(desde, hasta)
        params = {"desde": d, "hasta": h}
        etiqueta = f"{d} a {h}"
        if not es_global:
            b, err = _exigir_banca(banca)
            if err:
                return err
            params["banca_id"] = b["id"]; etiqueta += f", banca {b.get('nombre')}"
        elif banca:
            b, disp = _resolver(consorcio, "/api/bancas", banca)
            if not b:
                return f"No encontré una banca llamada '{banca}'. Bancas: {', '.join(disp[:30]) or '—'}."
            params["banca_id"] = b["id"]; etiqueta += f", banca {b.get('nombre')}"
        r = (_get(consorcio, "/api/reportes/pnl", params) or {}).get("resumen") or {}
        return (
            f"Resultado ({etiqueta}): {_rd(r.get('resultado_operativo'))} "
            f"(margen {r.get('margen_pct', '—')}%). Ventas {_rd(r.get('ventas'))}, "
            f"premios pagados {_rd(r.get('premios_pagados'))}, gastos {_rd(r.get('gastos_aprobados'))}. "
            f"{r.get('bancas', 0)} bancas, {r.get('tickets', 0)} tickets."
        )

    def ganancia_encargado(encargado: str) -> str:
        """Ganancia / balance de un ENCARGADO (su P&L vivo desde el último corte): ventas, premios,
        gastos, balance y reparto estimado. Úsalo para '¿cómo va el encargado Pedro?', '¿cuánto le
        toca a X?', '¿quién está en rojo?'. Da el nombre del encargado."""
        lista = _catalogo(consorcio, "/api/contabilidad/encargados")
        n = (encargado or "").strip().lower()
        match = next((e for e in lista if n and n == str(e.get("nombre") or "").strip().lower()), None) \
            or next((e for e in lista if n and n in str(e.get("nombre") or "").lower()), None)
        if not match:  # por tokens: si un solo encargado calza con nombre/apellido, úsalo
            toks = [t for t in re.split(r"[^0-9a-záéíóúñü]+", n) if len(t) >= 3]
            punt = sorted(((sum(1 for t in toks if t in str(e.get("nombre") or "").lower()), e)
                           for e in lista), key=lambda x: x[0], reverse=True)
            punt = [(s, e) for s, e in punt if s > 0]
            if punt and len([e for s, e in punt if s == punt[0][0]]) == 1:
                match = punt[0][1]
        if not match:
            nombres = [str(e.get("nombre") or "") for e in lista if e.get("nombre")]
            return f"No encontré un encargado llamado '{encargado}'. Encargados: {', '.join(nombres[:30]) or '—'}."
        det = _get(consorcio, f"/api/contabilidad/encargados/{match['empleado_id']}") or match
        rojo = " (EN ROJO)" if det.get("en_rojo") else ""
        return (
            f"Encargado {match.get('nombre')} — desde su último corte: ventas {_rd(det.get('ventas'))}, "
            f"premios pagados {_rd(det.get('premios_pagados'))}, gastos {_rd(det.get('gastos_total'))}, "
            f"balance {_rd(det.get('balance'))}{rojo}. Reparto estimado {_rd(det.get('reparto_estimado'))}."
        )

    def numeros_top(desde: str = "", hasta: str = "", loteria: str = "", banca: str = "") -> str:
        """Ranking de los NÚMEROS MÁS VENDIDOS en un rango (histórico), opcional por LOTERÍA y/o BANCA.
        Úsalo para '¿el número más vendido de Leidsa esta semana?', 'top de números del mes', '¿qué
        se jugó más en la banca X?'. Devuelve el top por cantidad de tickets y el desglose por modalidad."""
        d, h = _rango(desde, hasta)
        params = {"desde": d, "hasta": h}
        etiqueta = f"{d} a {h}"
        if loteria:
            lot, disp = _resolver(consorcio, "/api/loterias", loteria)
            if not lot:
                return f"No encontré una lotería llamada '{loteria}'. Loterías: {', '.join(disp[:30]) or '—'}."
            params["loteria"] = lot["id"]; etiqueta += f", {lot.get('nombre')}"  # OJO: param 'loteria', no 'loteria_id'
        if not es_global:
            b, err = _exigir_banca(banca)
            if err:
                return err
            params["banca_id"] = b["id"]; etiqueta += f", banca {b.get('nombre')}"
        elif banca:
            b, disp = _resolver(consorcio, "/api/bancas", banca)
            if not b:
                return f"No encontré una banca llamada '{banca}'. Bancas: {', '.join(disp[:30]) or '—'}."
            params["banca_id"] = b["id"]; etiqueta += f", banca {b.get('nombre')}"
        d_ = _get(consorcio, "/api/reportes/detalle-venta", params) or {}
        top = d_.get("top_numeros") or []
        if not top:
            return f"No hay ventas registradas ({etiqueta}) para rankear números."
        return f"Números más vendidos ({etiqueta}): " + "; ".join(
            f"{t.get('numero')} — {t.get('tickets_count')} tickets, {_rd(t.get('monto_total'))}"
            for t in top[:10]
        )

    def asistencia_empleado(empleado: str, desde: str = "", hasta: str = "") -> str:
        """Asistencia / TARDANZAS de un empleado en un rango: cuántos días llegó tarde, cuántos minutos,
        ausencias y días justificados. Úsalo para '¿cuántos días ha llegado tarde Pedro?', '¿cómo va la
        asistencia de X?', '¿quién ha faltado?'. Da el nombre del empleado. Rango por defecto: el mes
        actual (pasa desde/hasta AAAA-MM-DD para otro período)."""
        emp, disp = _resolver(consorcio, "/api/empleados", empleado, campo="nombre_real")
        if not emp:
            return f"No encontré un empleado llamado '{empleado}'. Empleados: {', '.join(disp[:30]) or '—'}."
        hoy = date.today()
        d = (desde or "").strip() or hoy.replace(day=1).isoformat()
        h = (hasta or "").strip() or hoy.isoformat()
        data = (_get(consorcio, "/api/crm/rrhh/asistencia",
                     {"empleado_id": emp["id"], "desde": d, "hasta": h}) or {}).get("data") or []
        if not data:
            return f"No hay registros de asistencia de {emp.get('nombre_real')} entre {d} y {h}."
        tarde = [x for x in data if int(x.get("minutos_tarde") or 0) > 0]
        min_tarde = sum(int(x.get("minutos_tarde") or 0) for x in tarde)
        ausente = [x for x in data if "ausen" in str(x.get("estado") or "").lower()]
        justif = [x for x in data if x.get("justificado")]
        return (
            f"Asistencia de {emp.get('nombre_real')} ({d} a {h}): {len(data)} días registrados. "
            f"Llegó TARDE {len(tarde)} día(s) (acumulado {min_tarde} min). "
            f"Ausencias: {len(ausente)}. Días justificados: {len(justif)}."
        )

    def vacaciones_empleado(empleado: str, anio: str = "") -> str:
        """Vacaciones de un empleado: días que le QUEDAN (pendientes), a los que tiene derecho y los
        tomados, del año dado (default: año actual). Úsalo para '¿cuántos días de vacaciones tiene X?',
        '¿le quedan vacaciones a Y?'. Da el nombre del empleado."""
        emp, disp = _resolver(consorcio, "/api/empleados", empleado, campo="nombre_real")
        if not emp:
            return f"No encontré un empleado llamado '{empleado}'. Empleados: {', '.join(disp[:20]) or '—'}."
        params = {"anio": anio.strip()} if anio.strip() else {}
        d = _get(consorcio, f"/api/crm/rrhh/vacaciones/{emp['id']}", params) or {}
        v, nm = d.get("vacacion"), emp.get("nombre_real")
        if not v:
            return f"Las vacaciones de {nm} para {d.get('anio', 'ese año')} aún no están calculadas en el sistema."
        return (f"Vacaciones de {nm} ({v.get('anio')}): le quedan {v.get('dias_pendientes')} día(s) "
                f"(derecho {v.get('dias_derecho')}, tomados {v.get('dias_tomados')}). Estado: {v.get('estado')}.")

    def regalia_empleado(empleado: str, anio: str = "") -> str:
        """Regalía (aguinaldo) de un empleado: cuánto le corresponde y cuánto recibe neto, del año dado
        (default: actual). Úsalo para '¿cuánto de regalía lleva X?', '¿cuánto de aguinaldo le toca?'."""
        emp, disp = _resolver(consorcio, "/api/empleados", empleado, campo="nombre_real")
        if not emp:
            return f"No encontré un empleado llamado '{empleado}'. Empleados: {', '.join(disp[:20]) or '—'}."
        params = {"anio": anio.strip()} if anio.strip() else {}
        d = _get(consorcio, f"/api/crm/rrhh/regalia/{emp['id']}", params) or {}
        r, nm = d.get("regalia"), emp.get("nombre_real")
        if not r:
            return f"La regalía de {nm} para {d.get('anio', 'ese año')} aún no está calculada."
        return (f"Regalía de {nm} ({r.get('anio')}): neto a recibir {_rd(r.get('monto_neto'))} "
                f"(obligación {_rd(r.get('monto_obligacion'))}, {r.get('meses_trabajados')} meses). "
                f"Estado: {r.get('estado')}.")

    def ficha_empleado(empleado: str) -> str:
        """Ficha laboral de un empleado: cuánto GANA (salario vigente), desde cuándo trabaja, su cargo y
        estado laboral. Úsalo para '¿cuánto gana X?', '¿cuál es el sueldo de Y?', '¿desde cuándo trabaja
        Z?', '¿qué cargo tiene?'."""
        emp, disp = _resolver(consorcio, "/api/empleados", empleado, campo="nombre_real")
        if not emp:
            return f"No encontré un empleado llamado '{empleado}'. Empleados: {', '.join(disp[:20]) or '—'}."
        d = _get(consorcio, f"/api/crm/rrhh/empleados/{emp['id']}/laboral") or {}
        lab = d.get("laboral") or {}
        comp = d.get("compensacion_vigente") or {}
        nm = emp.get("nombre_real")
        partes = [f"{nm} — cargo: {emp.get('rol')}."]
        if comp.get("sueldo_fijo") is not None:
            partes.append(f"Salario vigente: {_rd(comp.get('sueldo_fijo'))}.")
        if lab.get("fecha_ingreso"):
            partes.append(f"Ingresó: {lab.get('fecha_ingreso')}.")
        if lab.get("estado_laboral"):
            partes.append(f"Estado laboral: {lab.get('estado_laboral')}.")
        if lab.get("tipo_contrato"):
            partes.append(f"Contrato: {lab.get('tipo_contrato')}.")
        return " ".join(partes)

    def deudas_empleadas() -> str:
        """Quién DEBE dinero (deudas de empleada / arqueo): saldo pendiente por persona y banca. Úsalo
        para '¿quién debe?', '¿cuánto debe X?', '¿hay deudas pendientes?'. Ordena por saldo."""
        data = _catalogo(consorcio, "/api/crm/deudas-empleada/")
        deudores = [x for x in data if int(x.get("saldo") or 0) != 0]
        if not deudores:
            return "Nadie tiene saldo pendiente ahora mismo."
        deudores.sort(key=lambda x: int(x.get("saldo") or 0), reverse=True)
        total = sum(int(x.get("saldo") or 0) for x in deudores)
        return f"Deudas pendientes ({_rd(total)} en total) — " + "; ".join(
            f"{(x.get('empleado') or {}).get('nombre')} debe {_rd(x.get('saldo'))} "
            f"({(x.get('banca') or {}).get('nombre')})" for x in deudores[:12]
        )

    def detalle_deuda(empleado: str) -> str:
        """De DÓNDE viene la deuda de una persona: el historial de movimientos (arqueos/faltantes,
        abonos) que suman su saldo, con fecha, monto y el motivo. Úsalo para '¿de dónde viene la deuda
        de Dora?', '¿por qué debe X?', '¿de qué es esa deuda?'. Da el nombre de la persona."""
        lista = _catalogo(consorcio, "/api/crm/deudas-empleada/")
        toks = [t for t in re.split(r"[^0-9a-záéíóúñü]+", (empleado or "").lower()) if len(t) >= 3]
        mios = [x for x in lista if toks
                and any(t in str((x.get("empleado") or {}).get("nombre") or "").lower() for t in toks)
                and int(x.get("saldo") or 0) != 0]
        if not mios:
            nombres = sorted({str((x.get("empleado") or {}).get("nombre") or "")
                              for x in lista if int(x.get("saldo") or 0) != 0})
            return f"No encontré una deuda de '{empleado}'. Con deuda: {', '.join(nombres) or '—'}."
        partes = []
        for x in mios[:5]:
            emp = x.get("empleado") or {}; ban = x.get("banca") or {}
            det = _get(consorcio, f"/api/crm/deudas-empleada/{emp.get('empleado_id')}",
                       {"banca_id": ban.get("banca_id")}) or {}
            movs = det.get("movimientos") or []
            cab = f"{emp.get('nombre')} debe {_rd(x.get('saldo'))} en {ban.get('nombre')}"
            if not movs:
                partes.append(cab + " (sin detalle de movimientos).")
                continue
            det_txt = "; ".join(
                f"{m.get('tipo')} por {m.get('origen')} {_rd(m.get('monto'))}"
                + (f" — {m.get('observacion')}" if m.get('observacion') else "")
                + (f" ({str(m.get('creado_en'))[:10]})" if m.get('creado_en') else "")
                for m in movs[:8]
            )
            partes.append(f"{cab}, compuesta por: {det_txt}.")
        return " ".join(partes)

    def anulaciones(desde: str = "", hasta: str = "") -> str:
        """Anulaciones/cancelaciones de tickets en un rango: cuántas y cuánto se devolvió. Úsalo para
        '¿cuántas anulaciones hubo?', '¿cuánto se anuló/canceló?', '¿mucha anulación?'."""
        d, h = _rango(desde, hasta)
        r = (_get(consorcio, "/api/reportes/anulaciones", {"desde": d, "hasta": h}) or {}).get("resumen") or {}
        return f"Anulaciones ({d} a {h}): {r.get('total', 0)} tickets anulados, devuelto {_rd(r.get('monto_devuelto'))}."

    def recargas(desde: str = "", hasta: str = "") -> str:
        """Recargas de teléfono en un rango: cuántas, exitosas/rechazadas y monto. Úsalo para '¿cuánto
        se hizo en recargas?', '¿cómo van las recargas telefónicas?'."""
        d, h = _rango(desde, hasta)
        r = (_get(consorcio, "/api/reportes/recargas", {"desde": d, "hasta": h}) or {}).get("resumen") or {}
        return (f"Recargas ({d} a {h}): {r.get('total', 0)} en total "
                f"({r.get('exitosas', 0)} exitosas, {r.get('rechazadas', 0)} rechazadas), "
                f"monto exitoso {_rd(r.get('monto_exitoso'))}.")

    def caja_chica(desde: str = "", hasta: str = "") -> str:
        """Movimientos de CAJA CHICA en un rango: ingresos, egresos y neto. Úsalo para '¿cómo va la
        caja chica?', '¿cuánto se gastó de caja chica?', '¿entradas y salidas de caja?'."""
        d, h = _rango(desde, hasta)
        r = (_get(consorcio, "/api/reportes/caja-chica", {"desde": d, "hasta": h}) or {}).get("resumen") or {}
        return (f"Caja chica ({d} a {h}): {r.get('total', 0)} movimientos, "
                f"ingresos {_rd(r.get('ingresos'))}, egresos {_rd(r.get('egresos'))}.")

    def depositos(desde: str = "", hasta: str = "") -> str:
        """Depósitos al banco (de los mensajeros) en un rango: cuántos, aplicados/rechazados y monto.
        Úsalo para '¿cuánto se depositó al banco?', '¿cómo van los depósitos?'."""
        d, h = _rango(desde, hasta)
        r = (_get(consorcio, "/api/reportes/depositos", {"desde": d, "hasta": h}) or {}).get("resumen") or {}
        return (f"Depósitos ({d} a {h}): {r.get('total', 0)} en total "
                f"({r.get('aplicados', 0)} aplicados, {r.get('rechazados', 0)} rechazados, "
                f"{r.get('en_verificacion', 0)} en verificación), monto aplicado {_rd(r.get('monto_aplicado'))}.")

    def sla_arreglos(desde: str = "", hasta: str = "", banca: str = "") -> str:
        """SLA de arreglos/despachos en un rango: cuántos a tiempo vs. fuera de tiempo, cuántos colgados
        ahora. Úsalo para '¿cómo va el SLA?', '¿los arreglos se están atendiendo a tiempo?', '¿hay
        arreglos colgados?'. Opcional filtrar por BANCA."""
        d, h = _rango(desde, hasta)
        params = {"desde": d, "hasta": h}
        etiqueta = f"{d} a {h}"
        if not es_global:
            b, err = _exigir_banca(banca)
            if err:
                return err
            params["banca_id"] = b["id"]; etiqueta += f", banca {b.get('nombre')}"
        elif banca:
            b, disp = _resolver(consorcio, "/api/bancas", banca)
            if not b:
                return f"No encontré una banca llamada '{banca}'. Bancas: {', '.join(disp[:20]) or '—'}."
            params["banca_id"] = b["id"]; etiqueta += f", banca {b.get('nombre')}"
        r = (_get(consorcio, "/api/reportes/sla", params) or {}).get("resumen") or {}
        return (f"SLA arreglos ({etiqueta}): {r.get('total', 0)} en total, "
                f"{r.get('dentro_umbral', 0)} a tiempo / {r.get('fuera_umbral', 0)} fuera "
                f"({r.get('pct_dentro_umbral', '—')} dentro), {r.get('colgadas_ahora', 0)} colgados ahora "
                f"(umbral {r.get('umbral_horas', '—')}h).")

    _ESTADO_TAREA = {"pendiente": "pendiente", "con_salida": "en camino", "cerrado": "entregado",
                     "cancelado": "cancelada", "expirado": "expirada", "revertido": "revertida"}

    def tareas_mensajero(mensajero: str = "", banca: str = "", incluir_cerradas: bool = False) -> str:
        """Estado de las TAREAS/ENCARGOS del despacho: qué se está llevando, qué está pendiente, qué ya
        se entregó. Úsalo para '¿ya Juancito entregó tal encargo?', '¿qué tiene pendiente el mensajero
        X?', '¿ya se despachó a la banca Y?'. Filtra por mensajero y/o banca (por nombre). Por defecto
        solo las vivas (pendientes / en camino); incluir_cerradas=true para ver las ya entregadas."""
        params: dict = {"limit": 200}
        if not incluir_cerradas:
            params["activos"] = "true"
        tareas = (_get(consorcio, "/api/tareas", params) or {}).get("arreglos") or []
        if not es_global:  # acotado: solo tareas hacia SUS bancas
            tareas = [t for t in tareas if str(t.get("banca_id")) in mis_bancas]
        bmap = {str(b.get("id")): b.get("nombre") for b in _catalogo(consorcio, "/api/bancas")}
        desc = []
        if mensajero:
            emp, disp = _resolver(consorcio, "/api/empleados", mensajero, campo="nombre_real")
            if not emp:
                if not es_global:  # acotado: no volcar la nómina completa (fuga de nombres)
                    return f"No encontré un mensajero llamado '{mensajero}'."
                return f"No encontré un mensajero llamado '{mensajero}'. Empleados: {', '.join(disp[:20]) or '—'}."
            mid = str(emp["id"])
            tareas = [t for t in tareas if str(t.get("mensajero_asignado_id")) == mid]
            desc.append(f"de {emp.get('nombre_real')}")
        if banca:
            b, disp = _resolver(consorcio, "/api/bancas", banca)
            if not b:
                return f"No encontré una banca llamada '{banca}'. Bancas: {', '.join(disp[:20]) or '—'}."
            tareas = [t for t in tareas if str(t.get("banca_id")) == str(b["id"])]
            desc.append(f"hacia {b.get('nombre')}")
        vivas = "" if incluir_cerradas else "vivas "
        if not tareas:
            return f"No hay tareas {vivas}{' '.join(desc)}.".replace("  ", " ").strip()

        def _linea(t: dict) -> str:
            est = _ESTADO_TAREA.get(t.get("estado"), t.get("estado"))
            bn = bmap.get(str(t.get("banca_id"))) or "banca"
            who = t.get("mensajero_nombre") or "en pool (sin asignar)"
            extra = ""
            if t.get("estado") == "con_salida" and t.get("salida_en"):
                extra = f", salió {_hace(t.get('salida_en'))}"
            elif t.get("estado") == "cerrado" and t.get("recibo_en"):
                extra = f", entregado {_hace(t.get('recibo_en'))}"
            return f"{t.get('tipo')} → {bn}: {est} ({who}{extra})"

        cabeza = f"Tareas {vivas}{' '.join(desc)}".strip()
        return f"{cabeza} — {len(tareas)}: " + "; ".join(_linea(t) for t in tareas[:12])

    def _mensajeros_hacia_mis_bancas() -> set[str]:
        """Ids (empleado) de mensajeros con una tarea VIVA asignada hacia una de MIS bancas. Un
        encargado acotado solo puede ubicar a un mensajero que viene hacia lo suyo — no a cualquiera."""
        tareas = (_get(consorcio, "/api/tareas", {"limit": 200, "activos": "true"}) or {}).get("arreglos") or []
        return {str(t.get("mensajero_asignado_id")) for t in tareas
                if str(t.get("banca_id")) in mis_bancas and t.get("mensajero_asignado_id")}

    def ubicacion_mensajero(mensajero: str, banca: str = "") -> str:
        """Dónde está un mensajero AHORA (GPS en vivo) y, si preguntan, cuánto le falta para llegar a
        una banca. Úsalo para '¿dónde está Juancito?', '¿cuánto falta para que llegue a la banca X?',
        '¿está cerca?', '¿ya casi llega?'. Da el nombre del mensajero (y opcional la banca destino).
        La distancia/tiempo es estimada (línea recta a velocidad normal), no ruta exacta."""
        pos = _get(consorcio, "/api/crm/dashboard/mensajeros-posiciones")
        pos = pos.get("data") or pos.get("mensajeros") or [] if isinstance(pos, dict) else (pos or [])
        if not pos:
            return "No hay posiciones GPS de mensajeros ahora mismo."
        if not es_global:
            # Acotado: solo mensajeros que vienen hacia SUS bancas (no puede rastrear a cualquiera).
            permitidos = _mensajeros_hacia_mis_bancas()
            pos = [p for p in pos if str(p.get("mensajero_empleado_id")) in permitidos]
            if not pos:
                return "Ahora mismo no hay ningún mensajero en camino hacia tus bancas."
        toks = [t for t in re.split(r"[^0-9a-záéíóúñü]+", (mensajero or "").lower()) if len(t) >= 3]
        cands = [p for p in pos if toks and any(t in str(p.get("mensajero_nombre") or "").lower() for t in toks)]
        if len(cands) != 1:
            nombres = [str(p.get("mensajero_nombre")) for p in pos if p.get("mensajero_nombre")]
            if not cands:
                return f"No ubico a '{mensajero}' con GPS activo. En calle: {', '.join(nombres) or '—'}."
            return f"Varios calzan con '{mensajero}': {', '.join(str(c.get('mensajero_nombre')) for c in cands)}. ¿Cuál?"
        m = cands[0]
        nm = m.get("mensajero_nombre")
        fresco = _hace(m.get("reportado_en"))
        # El efectivo que carga el mensajero es dato agregado de la operación: SOLO alcance global.
        con_saldo = f" y lleva {_rd(m.get('saldo'))} encima" if es_global else ""
        if m.get("caido"):
            return f"{nm}: el GPS está CAÍDO (última posición {fresco}){con_saldo}." + (" Ojo con eso." if es_global else "")
        if banca:
            b, disp = _resolver(consorcio, "/api/bancas", banca)
            if not b:
                return f"Ubico a {nm} (posición {fresco}), pero no encontré la banca '{banca}'."
            if not es_global and not _banca_permitida(b.get("id")):
                info = _mis_bancas_info()
                nombres = ", ".join(str(x.get("nombre")) for x in info) or "—"
                return f"Esa banca no es tuya. Tus bancas: {nombres}."
            mapa = _get(consorcio, "/api/crm/dashboard/bancas-mapa")
            bl = (mapa.get("data", mapa) if isinstance(mapa, dict) else mapa) or []
            bco = next((x for x in bl if str(x.get("banca_id")) == str(b["id"])), None)
            dkm = _dist_km(m.get("lat"), m.get("lng"), (bco or {}).get("lat"), (bco or {}).get("lng")) if bco else None
            if dkm is not None:
                eta = max(1, round(dkm / 22 * 60))  # ~22 km/h urbano
                return (f"{nm} está a ~{dkm:.1f} km de {b.get('nombre')} (unos {eta} min estimados). "
                        f"Última posición {fresco}.")
            return f"{nm} — última posición {fresco}. No pude calcular la distancia a {b.get('nombre')} (falta GPS de la banca o del mensajero)."
        return f"{nm} — última posición {fresco}{con_saldo}" + (f" (modo {m.get('modo')})." if es_global else ".")

    def ticket_mayor_premio(desde: str = "", hasta: str = "") -> str:
        """El/los TICKET(S) de mayor PREMIO pagado en un rango (los premios más grandes que pagamos).
        Úsalo para '¿cuál fue el premio más grande?', 'el ticket más grande que pagamos', 'top de
        premios'. Nota: es a nivel de todo el consorcio (el back no ordena por banca)."""
        d, h = _rango(desde, hasta)
        data = (_get(consorcio, "/api/reportes/ganadores",
                     {"desde": d, "hasta": h, "estado": "ganador_pagado", "limit": 500}) or {}).get("data") or []
        if not data:
            return f"No hay premios pagados entre {d} y {h}."
        top = sorted(data, key=lambda t: int(t.get("premio") or 0), reverse=True)[:5]
        return f"Premios más grandes pagados ({d} a {h}): " + "; ".join(
            f"ticket {t.get('codigo_ticket')} {_rd(t.get('premio'))}" for t in top
        )

    todas = [
        # Dinero / operación en vivo
        StructuredTool.from_function(operativo),
        StructuredTool.from_function(disponible_para_retirar),
        StructuredTool.from_function(premios_pagados),
        StructuredTool.from_function(numero_mas_vendido),
        # Riesgo / fraude / alertas
        StructuredTool.from_function(prediccion_deficit),
        StructuredTool.from_function(cobertura_riesgo),
        StructuredTool.from_function(numeros_calientes),
        StructuredTool.from_function(score_fraude),
        StructuredTool.from_function(alertas_del_dia),
        # Reportes filtrados (mapa completo)
        StructuredTool.from_function(catalogo),
        StructuredTool.from_function(reporte_ventas),
        StructuredTool.from_function(reporte_ganancia),
        StructuredTool.from_function(ganancia_encargado),
        StructuredTool.from_function(numeros_top),
        StructuredTool.from_function(ticket_mayor_premio),
        StructuredTool.from_function(asistencia_empleado),
        StructuredTool.from_function(vacaciones_empleado),
        StructuredTool.from_function(regalia_empleado),
        StructuredTool.from_function(ficha_empleado),
        # Cobertura dinero/operación (Ola 3)
        StructuredTool.from_function(deudas_empleadas),
        StructuredTool.from_function(detalle_deuda),
        StructuredTool.from_function(anulaciones),
        StructuredTool.from_function(recargas),
        StructuredTool.from_function(caja_chica),
        StructuredTool.from_function(depositos),
        StructuredTool.from_function(sla_arreglos),
        # Despacho en vivo (Ola 5)
        StructuredTool.from_function(tareas_mensajero),
        StructuredTool.from_function(ubicacion_mensajero),
        # Reporte armado (PDF)
        StructuredTool.from_function(reporte_del_dia),
    ]
    if es_global:
        return todas
    # Usuario ACOTADO (encargado): SOLO herramientas por-banca, ya restringidas a SUS bancas.
    # Se excluyen las de consorcio completo, RRHH-personal, dinero agregado y ganancia de otros.
    seguras = {"reporte_ventas", "reporte_ganancia", "numeros_top", "sla_arreglos",
               "tareas_mensajero", "ubicacion_mensajero", "catalogo"}
    return [t for t in todas if t.name in seguras]
