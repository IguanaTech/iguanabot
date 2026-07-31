"""Contrato de idioma del bot: tuteo dominicano, nunca voseo argentino.

    python3 -m asistente.test_idioma

Por qué existe, y por qué acá pesa más que en el CRM:

El prompt del sistema le ORDENA al modelo no vosear —«nunca "vos tenés",
"fijate"»— y doce líneas más abajo le hablaba de vos: «revisá la lista de
herramientas otra vez». Con un modelo de lenguaje el EJEMPLO pesa más que la
orden: se le estaba enseñando exactamente lo que se le prohibía, en el mismo
texto. No es un detalle de estilo; es una instrucción que se contradice sola.

Y el modo de fallar es peor que en una pantalla: en el CRM el voseo lo ve
Eduardo y lo reporta. Acá el bot le escribe a las bancas, así que un tono
equivocado sale por WhatsApp/Telegram a gente de afuera antes de que nadie lo
note.

Se revisan los .py enteros —prompts, mensajes y comentarios— porque en este
repo el texto de los prompts ES código.

No usa pytest a propósito: en este repo las pruebas se corren como script
suelto (ver test_memoria.py), y esta tiene que poder correrse sin instalar nada.
"""
from __future__ import annotations

import pathlib
import re
import sys

RAIZ = pathlib.Path(__file__).resolve().parent.parent

# Formas INEQUÍVOCAS. Fuera quedan «pedí» y «salí» a propósito: también son
# pretérito de primera persona en tuteo («yo pedí»). Una prueba que grita por
# texto correcto es una prueba que alguien termina borrando.
VOSEO = [
    # presente
    "tenés", "podés", "querés", "sabés", "hacés", "debés", "ponés", "venís",
    "decidís", "dejás", "mandás", "tocás", "llevás", "agarrás",
    # imperativo
    "andá", "mirá", "fijate", "dejá", "tocá", "poné", "hacé", "elegí", "revisá",
    "volvé", "escribí", "buscá", "guardá", "marcá", "probá", "entrá", "cerrá",
    "mandá", "contá", "sacá", "sacale", "esperá", "acordate", "agarrá", "llevá",
    # imperativo CON PRONOMBRE PEGADO. Se sumaron después de que la primera
    # versión de esta prueba pasara verde con «decilo» todavía en el prompt del
    # sistema: sin el pronombre atrás la palabra no lleva tilde, así que ninguna
    # regla basada en el acento las ve.
    "decilo", "decile", "decila", "hacelo", "hacele", "ponelo", "ponele",
    "mandalo", "mandale", "dejalo", "dejala", "dejame", "tocalo", "sacalo",
    "sacala", "buscalo", "buscala", "miralo", "mirala", "quedate", "tomate",
    "andate", "contame", "mostrame", "esperame",
]

LETRA = "A-Za-zÁÉÍÓÚÜÑáéíóúüñ"
RE = re.compile(rf"(?<![{LETRA}])({'|'.join(VOSEO)})(?![{LETRA}])", re.IGNORECASE)

# El propio prompt NOMBRA las formas prohibidas para enseñárselas al modelo.
# Esas menciones son el contenido de la regla, no una infracción.
EXENTAS = {
    "asistente/graph.py": ('nunca voseo argentino',),
}


def _fuentes():
    for p in sorted(RAIZ.rglob("*.py")):
        if any(x in p.parts for x in (".git", "__pycache__", "node_modules", "venv", ".venv")):
            continue
        # Este archivo LISTA las formas prohibidas: es el catálogo, no una
        # infracción. Se salta entero y no por línea, porque exentar "toda línea
        # con comillas" taparía voseo de verdad escrito acá adentro.
        if p.name == "test_idioma.py":
            continue
        yield p


def revisar() -> list[str]:
    culpables = []
    for p in _fuentes():
        rel = str(p.relative_to(RAIZ))
        exentas = EXENTAS.get(rel, ())
        for n, linea in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if exentas and any(e in linea for e in exentas):
                continue
            for m in RE.finditer(linea):
                culpables.append(f"{rel}:{n} «{m.group(1)}»")
    return culpables


if __name__ == "__main__":
    fuentes = list(_fuentes())
    if len(fuentes) < 5:
        print(f"FALLA: sólo {len(fuentes)} fuentes — el barrido no encontró el repo")
        sys.exit(1)

    culpables = revisar()
    if culpables:
        print(f"FALLA: {len(culpables)} formas de voseo (la regla es tuteo dominicano):")
        for c in culpables:
            print("  ·", c)
        sys.exit(1)

    print(f"OK — {len(fuentes)} fuentes, sin voseo.")
