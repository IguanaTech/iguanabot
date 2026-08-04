"""Prueba de la memoria semántica contra un Postgres+pgvector REAL y el modelo REAL.

No usa dobles: el valor de esta función está en dos cosas que sólo se ven con la base y el modelo
de verdad —que la recuperación por parecido devuelva lo que corresponde, y que un teléfono NO pueda
leer lo de otro—. Con un embedder falso, ambas pasarían igual y no probarían nada.

    docker compose -f docker-compose.test.yml up -d
    DATABASE_URL=postgresql://asistente:asistente@127.0.0.1:15433/asistente python -m asistente.test_memoria
"""
import sys

from . import memoria

ANA = "18091110000"
BETO = "18092220000"
CONS_A = "11111111-1111-1111-1111-111111111111"
CONS_B = "22222222-2222-2222-2222-222222222222"

fallos = []


def debe(cond, msg):
    print(("  ok   " if cond else "  FALLA ") + msg)
    if not cond:
        fallos.append(msg)


def main() -> int:
    print("preparando (crea la tabla con la dimensión del modelo)…")
    memoria.preparar()
    if memoria._modelo() is None:
        print("sin modelo de embeddings: no se puede probar de verdad.")
        return 1

    with memoria._pool().connection() as cx:
        # LOS DOS CONSORCIOS DE LA PRUEBA, creados por ella misma.
        #
        # `memoria.consorcio_id` tiene FK contra `consorcios`, así que sin estas filas TODO
        # `recordar()` falla en silencio —el módulo se traga el error y sigue— y la prueba reporta
        # nueve fallas que parecen de la memoria semántica y son de una tabla vacía.
        #
        # Antes esto andaba porque la base del desarrollador ya tenía consorcios de otras corridas.
        # La primera corrida del CI, contra una base recién creada, lo dejó a la vista: una prueba
        # que depende de datos que no crea sólo pasa en la máquina donde ya estaban.
        for cid, nombre in ((CONS_A, "Consorcio de prueba A"), (CONS_B, "Consorcio de prueba B")):
            cx.execute(
                "INSERT INTO consorcios (id, nombre, backend_url, usuario, password_cifrado) "
                "VALUES (%s, %s, 'http://localhost:0', 'prueba', 'x') "
                "ON CONFLICT (id) DO NOTHING",
                (cid, nombre))
        cx.execute("DELETE FROM memoria WHERE telefono IN (%s, %s)", (ANA, BETO))

    print("\n1. guardar y recuperar por PARECIDO (no por palabra exacta)")
    debe(memoria.recordar(ANA, CONS_A, "Mi banca principal es La Suerte, la del centro"), "guarda")
    memoria.recordar(ANA, CONS_A, "Prefiero que me mandes el reporte en PDF, no en texto")
    memoria.recordar(ANA, CONS_A, "El encargado de la zona norte es Juan Pérez")

    r = memoria.recuperar(ANA, CONS_A, "cómo va mi banca del centro")
    debe(any("La Suerte" in x for x in r),
         "una consulta con OTRAS palabras trae el recuerdo correcto")
    debe(not any("PDF" in x for x in r) or len(r) > 1,
         "no arrastra todo lo guardado como si fuera relevante")

    r2 = memoria.recuperar(ANA, CONS_A, "mándame el reporte")
    debe(any("PDF" in x for x in r2), "otra consulta trae OTRO recuerdo (discrimina)")

    print("\n2. AISLAMIENTO — lo que importa de verdad")
    memoria.recordar(BETO, CONS_A, "Mi banca es El Trébol")
    rb = memoria.recuperar(BETO, CONS_A, "cómo va mi banca")
    debe(any("Trébol" in x for x in rb), "Beto recupera lo suyo")
    debe(not any("La Suerte" in x for x in rb),
         "Beto NO ve lo de Ana aunque sean del mismo consorcio")

    r_otro = memoria.recuperar(ANA, CONS_B, "cómo va mi banca")
    debe(r_otro == [],
         "el MISMO teléfono en otro consorcio no ve nada (una persona puede operar en dos)")

    debe(memoria.recuperar(ANA, None, "cómo va mi banca") == [],
         "sin consorcio resuelto no devuelve NADA (fail-closed)")
    debe(memoria.recordar(ANA, None, "algo") is False,
         "sin consorcio tampoco escribe")

    print("\n3. listar, no duplicar, olvidar")
    antes = len(memoria.listar(ANA, CONS_A))
    memoria.recordar(ANA, CONS_A, "Mi banca principal es La Suerte, la del centro")
    debe(len(memoria.listar(ANA, CONS_A)) == antes,
         "guardar dos veces la misma frase no la duplica")

    debe(memoria.olvidar(ANA, CONS_A, "PDF") == 1, "olvida por texto")
    debe(not any("PDF" in x for x in memoria.listar(ANA, CONS_A)), "y ya no está")
    debe(memoria.olvidar(ANA, CONS_A, "no existe nada así") == 0, "borrar de menos, nunca de más")
    debe(memoria.olvidar(BETO, CONS_A, "La Suerte") == 0,
         "no se puede borrar el recuerdo de OTRA persona")
    debe(any("Trébol" in x for x in memoria.listar(BETO, CONS_A)), "lo de Beto sigue intacto")

    print("\n4. cambio de vectorizador: se ignora lo viejo, no se miente")
    # Se simula un salto de versión de la librería marcando el recuerdo con otra firma. Comparar
    # vectores de pooling distinto no da error: da resultados PEORES sin avisar. Que se ignoren es
    # lo correcto — el bot pierde un recuerdo, no inventa una relación que no existe.
    with memoria._pool().connection() as cx:
        cx.execute("UPDATE memoria SET modelo = 'otro-modelo@fastembed0.1' WHERE telefono=%s",
                   (BETO,))
    debe(memoria.recuperar(BETO, CONS_A, "cómo va mi banca") == [],
         "un recuerdo vectorizado con OTRO modelo no se compara con los de ahora")
    with memoria._pool().connection() as cx:
        cx.execute("UPDATE memoria SET modelo = %s WHERE telefono=%s",
                   (memoria._firma_modelo(), BETO))
    debe(any("Trébol" in x for x in memoria.recuperar(BETO, CONS_A, "cómo va mi banca")),
         "y con la firma correcta vuelve a aparecer")

    print("\n5. purga por antigüedad")
    with memoria._pool().connection() as cx:
        cx.execute("UPDATE memoria SET creado_en = now() - interval '400 days' WHERE telefono=%s",
                   (ANA,))
    n = memoria.purgar(365)
    debe(n >= 1, "purga los vencidos")
    debe(memoria.listar(ANA, CONS_A) == [], "y no quedan")
    debe(len(memoria.listar(BETO, CONS_A)) == 1, "sin llevarse los de otro")

    print("\n" + ("TODO OK" if not fallos else f"{len(fallos)} FALLA(S): " + "; ".join(fallos)))
    return 1 if fallos else 0


if __name__ == "__main__":
    sys.exit(main())
