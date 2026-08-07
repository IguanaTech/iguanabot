"""Un error NO se dice como un dato.

    python3 -m asistente.test_reportes_mudos

Es la clase de bug que más caro salió esta semana, y apareció cuatro veces en
cuatro formas distintas:

  1. el `_safe` del bot se comía un 404 y devolvía {} → se leyó como
     "hoy no se aprobó nada" en un reporte de RENDICIÓN DE CUENTAS;
  2. `/api/crm/dinero/disponible` no existía, la llamada vivía en un
     `except: pass` → el resumen de la banca salía sin el disponible y no
     había forma de saber que faltaba algo;
  3. `buscar_ticket` contestaba «no encontré ningún ticket» ante CUALQUIER
     falla → decirle a alguien que un ticket ganador no existe;
  4. y la peor: si `ventas` y `ganadores` fallaban, sus contadores daban cero,
     `hubo_movimiento` concluía "el día estuvo quieto" y el bot NO MANDABA
     NADA. Un back caído se traducía en silencio, y el silencio significa
     "no hubo movimiento" por decisión explícita de Eduardo.

Las cuatro comparten forma: el sistema no distingue "no hay" de "no pude
mirar", y el que lee entiende "no hay". Sobre plata, un dato equivocado es
peor que un error — con el error uno vuelve a preguntar.

Esta prueba fija lo contrario: cuando la lectura falla, se nota.
"""
from __future__ import annotations

import sys
import types

# `reportes` importa httpx y psycopg para pegar al back; lo que se prueba acá es lógica pura
# (¿se manda o no?, ¿qué dice el texto?) y no toca la red. Se ponen módulos vacíos para que esto
# corra en cualquier máquina sin instalar nada — igual que el resto de las pruebas de este repo,
# que se ejecutan como script suelto.
for _m in ("httpx", "psycopg"):
    sys.modules.setdefault(_m, types.ModuleType(_m))

from . import reportes  # noqa: E402

fallos: list[str] = []


def check(cond: bool, que: str) -> None:
    if not cond:
        fallos.append(que)


# ── 1. Un día realmente quieto NO se manda ───────────────────────────────────
quieto = reportes.Reporte(
    consorcio="X", fecha="2026-07-31",
    datos={"ventas": {"resumen": {"total_monto": "0", "total_tickets": 0}},
           "ganadores": {"resumen": {"total_premios": "0", "premios_por_pagar": "0",
                                     "total_pagados": 0}}},
    fallidos=[])
check(reportes.hubo_movimiento(quieto) is False,
      "un día con todo en cero debería seguir siendo silencio (regla de Eduardo)")

# ── 2. Un día que no se pudo LEER sí se manda ────────────────────────────────
ciego = reportes.Reporte(
    consorcio="X", fecha="2026-07-31",
    datos={"ventas": None, "ganadores": None},
    fallidos=["ventas", "ganadores"])
check(reportes.hubo_movimiento(ciego) is True,
      "si ventas/premios no se pudieron leer NO se puede concluir que el día estuvo quieto")

# Alcanza con que falle UNO.
medio = reportes.Reporte(
    consorcio="X", fecha="2026-07-31",
    datos={"ventas": {"resumen": {"total_monto": "0"}}, "ganadores": None},
    fallidos=["ganadores"])
check(reportes.hubo_movimiento(medio) is True,
      "con los premios sin leer tampoco se puede afirmar que no pasó nada")

# ── 3. El texto DICE qué no pudo leer ────────────────────────────────────────
txt = reportes.texto(ciego)
check("No pude leer" in txt, "el texto debe avisar que hubo bloques que no se pudieron leer")
check("ventas" in txt and "premios" in txt, "el aviso debe NOMBRAR los bloques que faltan")
check("NO es cero" in txt,
      "el aviso debe aclarar que lo que falta no es un cero (es justo la confusión que se busca evitar)")

# Y NO lo dice cuando no hay nada que avisar — un aviso que sale siempre no se lee.
completo = reportes.Reporte(
    consorcio="X", fecha="2026-07-31",
    datos={"ventas": {"resumen": {"total_monto": "1000"}},
           "ganadores": {"resumen": {"total_premios": "0"}}},
    fallidos=[])
check("No pude leer" not in reportes.texto(completo),
      "sin fallas no debe aparecer el aviso")

# ── 3b. «Nada pendiente» no se dice sin haberlo mirado ───────────────────────
#
# El bot le mandó a Eduardo: «día cerrado — Venta: RD$ 0. Nada pendiente.» Esa línea sale de la
# rama corta, la que se toma cuando `hubo_movimiento` da False. Y ese False sólo miraba ventas y
# premios: un día sin vender puede tener perfectamente RD$ 40,000 esperando en las bancas, una
# deuda abierta y papelería vencida. O sea que el bot AFIRMABA que no había nada pendiente sin
# haberlo mirado nunca. Eduardo: «aunque no se haya vendido nada hay dinero que recoger».
cero = {"ventas": {"resumen": {"total_monto": "0", "total_tickets": 0}},
        "ganadores": {"resumen": {"total_premios": "0", "premios_por_pagar": "0",
                                  "total_pagados": 0}}}

con_plata = reportes.Reporte(
    consorcio="X", fecha="2026-08-06",
    datos={**cero, "operativo": {"bancas_en_calle": [
        {"nombre": "Banca el Cine", "disponible": "40000"},
        {"nombre": "Otra", "disponible": "0"},
    ]}},
    fallidos=[])
check(reportes.hubo_movimiento(con_plata) is True,
      "sin ventas pero con dinero para retirar, el día SÍ tiene algo que contar")
check(any("40.000" in p for p in reportes.hay_pendientes(con_plata)),
      "los pendientes deben decir CUÁNTO hay para retirar, no sólo que hay algo")

con_alerta = reportes.Reporte(
    consorcio="X", fecha="2026-08-06",
    datos={**cero, "operativo": {"alertas": {"bancas_tickets_sin_recoger": [{"banca_id": "b1"}]}}},
    fallidos=[])
check(reportes.hubo_movimiento(con_alerta) is True,
      "una alerta abierta es algo que atender aunque no se haya vendido nada")

sin_nada = reportes.Reporte(
    consorcio="X", fecha="2026-08-06",
    datos={**cero, "operativo": {"bancas_en_calle": [], "alertas": {"x": []}}},
    fallidos=[])
check(reportes.hubo_movimiento(sin_nada) is False,
      "un día de verdad quieto sigue siendo silencio (regla de Eduardo)")

operativo_caido = reportes.Reporte(
    consorcio="X", fecha="2026-08-06",
    datos={**cero, "operativo": None},
    fallidos=["operativo"])
check(reportes.hubo_movimiento(operativo_caido) is True,
      "si el estado operativo no se pudo leer, no se puede afirmar que no hay pendientes")


# ── 4. `fallidos` se llena solo, no a mano ───────────────────────────────────
# (se verifica la forma, no la red: `datos_dia` pega al back)
check("fallidos" in reportes.Reporte.__dataclass_fields__,
      "Reporte debe llevar la lista de bloques fallidos")


if __name__ == "__main__":
    if fallos:
        print(f"FALLA: {len(fallos)}")
        for f in fallos:
            print("  ·", f)
        sys.exit(1)
    print("OK — un error no se dice como un dato.")
