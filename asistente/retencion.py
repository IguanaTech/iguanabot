"""Retención de la memoria del bot (escala #3).

Sin esto, la memoria crece PARA SIEMPRE: LangGraph guarda un checkpoint por usuario (thread_id =
teléfono) que nunca se limpia, y la `bitacora` es write-only (nadie la lee, nunca se purga). A muchos
consorcios/usuarios eso es disco sin tope. Este job (1×/día):

  1. Borra los HILOS INACTIVOS: threads cuyo último checkpoint (`checkpoint->>'ts'`) es más viejo que
     CHECKPOINT_RETENCION_DIAS → se eliminan de las 3 tablas de checkpoint. Si el usuario vuelve a
     escribir, empieza un hilo nuevo (pierde el contexto viejo, que ya no importaba).
  2. Purga la `bitacora` más vieja que BITACORA_RETENCION_DIAS.
  3. Purga los `recordatorio_diario` viejos (el dedup de hoy no necesita el histórico).
  4. Purga los RECUERDOS semánticos más viejos que MEMORIA_RETENCION_DIAS. Plazo mucho más largo
     que el del hilo (son justamente las cosas que deben sobrevivirlo), pero no eterno: una
     preferencia de hace un año probablemente ya no aplica y nadie se acuerda de borrarla.

Idempotente y best-effort: nunca rompe el arranque ni el resto del bot.
"""
import psycopg

from .config import config


def limpiar() -> None:
    try:
        with psycopg.connect(config.DATABASE_URL) as conn:
            with conn.cursor() as cur:
                # 1. Hilos inactivos (thread_id = teléfono). El último ts del checkpoint marca la
                #    última actividad; si es viejo, se borra de las 3 tablas de checkpoint.
                cur.execute(
                    "SELECT thread_id FROM checkpoints "
                    "GROUP BY thread_id "
                    "HAVING max((checkpoint->>'ts')::timestamptz) < now() - make_interval(days => %s)",
                    (config.CHECKPOINT_RETENCION_DIAS,),
                )
                viejos = [r[0] for r in cur.fetchall()]
                if viejos:
                    for tabla in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                        cur.execute(f"DELETE FROM {tabla} WHERE thread_id = ANY(%s)", (viejos,))
                # 2. Bitácora vieja (write-only).
                cur.execute(
                    "DELETE FROM bitacora WHERE creado_en < now() - make_interval(days => %s)",
                    (config.BITACORA_RETENCION_DIAS,),
                )
                bitacora_borradas = cur.rowcount or 0
                # 3. Recordatorios diarios viejos (basta con los recientes para el dedup).
                cur.execute(
                    "DELETE FROM recordatorio_diario WHERE dia < (now() - make_interval(days => 7))::date"
                )
                # 4. Recuerdos semánticos vencidos. `to_regclass` porque la tabla la provisiona el
                #    módulo de memoria al arrancar: si la memoria está apagada, puede no existir y
                #    esto NO debe tumbar el resto de la retención.
                cur.execute("SELECT to_regclass('public.memoria')")
                recuerdos_borrados = 0
                if cur.fetchone()[0] is not None:
                    cur.execute(
                        "DELETE FROM memoria WHERE creado_en < now() - make_interval(days => %s)",
                        (config.MEMORIA_RETENCION_DIAS,),
                    )
                    recuerdos_borrados = cur.rowcount or 0
            conn.commit()
        print(f"[retencion] hilos inactivos borrados: {len(viejos)} · bitácora purgada: "
              f"{bitacora_borradas} filas · recuerdos vencidos: {recuerdos_borrados}.")
    except Exception as ex:  # noqa: BLE001
        print(f"[retencion] (aviso) falló, se reintenta mañana: {ex}")


def arrancar() -> None:
    """Programa la retención 1×/día (4:30am, hora tranquila)."""
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except Exception as ex:  # noqa: BLE001
        print(f"[retencion] APScheduler no disponible ({ex}); no se programa la retención.")
        return
    sched = BackgroundScheduler(timezone=config.REPORTE_TZ)
    sched.add_job(limpiar, "cron", hour=4, minute=30, id="retencion_memoria")
    sched.start()
    print("[retencion] retención de memoria programada (diaria 4:30am).")
