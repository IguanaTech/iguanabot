"""El aviso se prueba por el TEXTO que sale, no por los datos que entran.

    python3 -m asistente.test_avisos_texto

POR QUÉ EXISTE
El 7 de agosto le llegó al admin este mensaje, por dinero:

    🎫 TICKETS SIN RECOGER — Dora La Exploradora
    ? ticket(s) vencidos sin recoger.

Dora no tenía tickets: tenía una DEUDA de RD$48.936 sin abonar hace 49 días. En el back, las listas
de alertas estaban corridas una posición, así que bajo el rótulo de «tickets» viajaban las deudas.

Lo que interesa acá no es ese bug —ya está arreglado y tiene su prueba de contrato— sino POR QUÉ
sobrevivió a tres barridos: **ninguna prueba leía el mensaje que sale**. Se probaban las consultas,
los endpoints, las formas… y el texto final, el único que ve una persona, no lo miraba nadie. El «?»
era el síntoma visible y no había quien lo viera.

Estas pruebas arman el aviso desde un ítem realista y miran el string. Sin red, sin base: pura
función de datos a texto, que es exactamente donde vivía el defecto.
"""

import sys

from .watcher import _alarmas_dinero_estancado


fallos: list[str] = []


def check(cond: bool, que: str) -> None:
    if not cond:
        fallos.append(que)


class ConsorcioFalso:
    """Lo mínimo que `_alarmas_dinero_estancado` toca del consorcio."""
    id = "c1"
    nombre = "Consorcio de prueba"


def avisos_con(alertas: dict) -> list[tuple[str, str]]:
    """Corre el generador de avisos contra un tablero fabricado."""
    import asistente.watcher as w
    original = w._get
    w._get = lambda consorcio, path, params=None: {"alertas": alertas}
    try:
        return _alarmas_dinero_estancado(ConsorcioFalso())
    finally:
        w._get = original


# ── 1. NINGÚN AVISO SALE CON UN «?» DONDE VA UN NÚMERO ───────────────────────
# La regla dura y la que habría cazado el bug del 7 de agosto. Un signo de pregunta en un mensaje de
# dinero significa que el dato no llegó — y un aviso que no sabe cuánto es no sirve para decidir.
tablero = {
    "bancas_tickets_sin_recoger": [{
        "banca_id": "b1", "nombre": "Banca el Cine", "pendientes": 2,
        "dias_mas_viejo": 10, "monto_total": "18020", "codigos": ["636b89a9", "b1ec9a7f"],
    }],
    "mensajero_saldo_estancado": [{
        "mensajero_empleado_id": "e1", "mensajero_nombre": "Mario", "saldo": "9000", "dias_con_dinero": 3,
    }],
}
avisos = avisos_con(tablero)
check(len(avisos) == 2, f"tenían que salir 2 avisos y salieron {len(avisos)}")
for _ref, texto in avisos:
    check("?" not in texto, f"un aviso de dinero salió con un «?»: {texto[:90]}")


# ── 2. EL AVISO DICE CUÁNTO, CUÁNDO Y CUÁLES ─────────────────────────────────
# Antes decía «2 ticket(s) vencidos sin recoger» y nada más. Dos tickets pueden ser RD$40 o
# RD$18.020, y mandar un mensajero hoy o el lunes no es la misma decisión.
texto_tickets = next(t for _r, t in avisos if "TICKETS" in t)
check("Banca el Cine" in texto_tickets, "el aviso no dice de qué banca es")
check("2 ticket" in texto_tickets, "el aviso no dice cuántos tickets son")
check("18.020" in texto_tickets or "18,020" in texto_tickets,
      f"el aviso no dice CUÁNTO dinero es: {texto_tickets[:110]}")
check("10 día" in texto_tickets, "el aviso no dice desde cuándo está el papel ahí")
check("636b89a9" in texto_tickets and "b1ec9a7f" in texto_tickets,
      "el aviso no nombra los tickets: por teléfono hay que poder decir cuáles son")


# ── 3. EL NOMBRE DEL AVISO ES EL DE SU PROPIA LISTA ──────────────────────────
# El bug exacto del 7 de agosto: bajo «tickets sin recoger» viajaban las DEUDAS. Acá se le mete al
# generador un ítem con forma de deuda en la lista de tickets, y el aviso NO puede salir como si
# fuera un ticket con todos sus datos: sin `pendientes` ni `monto_total`, lo que saldría es
# justamente el «?» — y el chequeo 1 lo mataría. Se verifica que ese caso NO se pueda colar.
disfrazado = avisos_con({
    "bancas_tickets_sin_recoger": [
        {"empleado_id": "e9", "nombre": "Dora La Exploradora", "saldo": "48936", "dias_sin_abonar": 49},
    ],
})
for _ref, texto in disfrazado:
    check("?" not in texto,
          "un ítem con forma de DEUDA se coló en la lista de tickets y el aviso salió con «?». "
          "Es literalmente el mensaje que recibió el admin el 2026-08-07")


# ── 4. SIN DATOS NO SE INVENTA UN AVISO ──────────────────────────────────────
check(avisos_con({}) == [], "con el tablero vacío no puede salir ningún aviso")
check(avisos_con({"bancas_tickets_sin_recoger": []}) == [],
      "con la lista vacía no puede salir ningún aviso")


if __name__ == "__main__":
    if fallos:
        print(f"FALLA: {len(fallos)}")
        for f in fallos:
            print("  ·", f)
        sys.exit(1)
    print("OK — el aviso dice cuánto, cuáles y de quién; y nunca sale con un «?».")
