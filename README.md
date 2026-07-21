# iguana-asistente

Asistente de IA para la plataforma IguanaSuite. Se comunica por **WhatsApp** (texto y notas de voz),
entiende quién le escribe, resuelve a qué **consorcio** pertenece, y actúa contra el back de ESE
consorcio en nombre del usuario — heredando todas las guardas de dinero del back.

> **Es el 5º proyecto, separado.** No se mezcla con `fortuna-api`, `fortuna-crm`, `Macularius` ni
> `iguanapp_flutter`. Tiene su propio contenedor, su propia base de datos y (a futuro) su propio repo.

## Filosofía

El asistente **no es un sistema nuevo de dinero**: es **un usuario más del mismo espacio de acciones**
(los endpoints del back). Construimos el **cerebro** (LangGraph + LLM) y la **boca/oído** (WhatsApp +
voz). Las manos (mover dinero) ya existen en el back, con sus guardas: ledger idempotente, un solo
asentador, reglas de oro, separación de funciones, self-grant cap, auditoría.

**Fase 0 (esto):** solo LECTURA. El asistente ve el operativo, el riesgo, el fraude, la predicción de
déficit, responde preguntas y **entrega reportes**. **No mueve dinero.** El dinero-con-confirmación es Fase 2.

## Alarmas de dinero (vigilante proactivo)

El asistente **vigila y avisa solo** — no espera a que le pregunten. Cada ~90s revisa el riesgo de cada
consorcio y, si aparece algo, le manda la alarma a sus admin/encargado. Las dos críticas:

- **🔴 Premio sin cobertura** — una banca cuyo número más jugado, si sale, **no lo paga con su caja**.
  Trae cuánto pagaría, su caja, cuánto le falta y el cupo sugerido. Fuente: `/api/crm/riesgo/cobertura`.
- **🔥 Número caliente** — la misma jugada en muchas bancas en poco tiempo (posible dato filtrado).
  Fuente: `/api/crm/riesgo/velocidad` (nivel "fuerte").
- **🏆 Ganador alto (real)** — un ticket que **ya ganó** un premio grande hoy (≥ `UMBRAL_GANADOR`).
  Fuente: `/api/reportes/ganadores`. Distinto de la exposición: acá el premio ya salió.

No recomputa nada: relaya lo que el back ya calcula, con cooldown para no repetir. **Avisar es lectura**
— bajar el cupo o bloquear el número lo decide el humano en el CRM. También responden a pedido: puedes
preguntarle "¿hay algún número caliente?" o "¿alguna banca descubierta?".

> ⚠ Estas alarmas van a **pocos** admins de un consorcio (bajo volumen, más tolerable en Baileys que el
> reporte masivo del cierre), pero siguen siendo bot-inicia → a escala grande, migrar a Cloud API (Fase 1).
> Prendible/apagable con `ALARMAS_PUSH`.

## Reportes

- **A pedido:** el cliente escribe "mándame el reporte de hoy" → el bot lo arma (ventas, ganancia,
  bancas, riesgo, fraude) y lo entrega como texto + PDF adjunto. Seguro en Fase 0 (le respondes a quien
  te escribió).
- **Automático al cierre:** a la hora configurada (`REPORTE_EOD_HORA`, TZ Santo Domingo) el bot le manda
  solo el reporte del día a los admin/encargado de cada consorcio.
  > ⚠ **Baileys y el envío que el bot INICIA:** mandar mensajes no solicitados a muchos números por un
  > WhatsApp no oficial es la vía rápida a un **baneo**. Por eso el envío automático viene **APAGADO**
  > (`REPORTE_EOD_AUTO=false`) — úsalo para pocos/pruebas. El envío masivo diario correcto es con la
  > **Cloud API oficial + plantillas aprobadas** (Fase 1). El código ya está listo; solo se prende cuando
  > la boca sea la oficial.

## Arquitectura

```
[Cliente WhatsApp] ──texto/voz──> [ Puente Baileys (Node) ]  ← Fase 0: número normal, sin Meta
                                          │  HTTP
                                          ▼
                                 [ Asistente (Python) ]
                                 · FastAPI (recibe del puente)
                                 · Whisper (voz→texto) + TTS (texto→voz)
                                 · LangGraph ReAct + Claude Haiku 4.5 (HITL)
                                 · DB propia: registro + directorio + memoria (pgvector)
                                          │  resuelve teléfono → consorcio → usuario
                                          ▼  llama como usuario de servicio (Bearer)
                                 [ Back del consorcio (fortuna-api) ]  ← read-only en Fase 0
```

- **Puente Baileys** (`bridge/`): en Fase 0 usa un número **normal** de WhatsApp (una SIM/celular viejo),
  sin verificación con Meta. En Fase 1 se cambia por la Cloud API oficial — el asistente no cambia.
- **Asistente** (`asistente/`): el cerebro. Recibe `{from, text|audio}` del puente, resuelve identidad,
  corre el grafo, y devuelve la respuesta (texto y/o audio).
- **DB propia** (`db/`): registro de consorcios (dónde vive cada back + su token de servicio, cifrado),
  directorio de teléfonos (teléfono → consorcio + usuario), y memoria de conversación (pgvector).

## Fase 0 → Fase 1

| | Fase 0 (prototipo) | Fase 1 (primer cliente) |
|---|---|---|
| Hosting | Mismo server que el back, contenedor aparte | Despliegue propio (o co-locado si el 1er cliente eres tú) |
| WhatsApp | Baileys sobre número normal | Cloud API oficial de Meta |
| Consorcios | 1 (el de prueba) | N (registro gana filas) |
| Acciones | Solo lectura + confirmar | + dinero-con-confirmación (Fase 2) |

## Qué falta para correr (credenciales externas)

1. **Una llave de LLM.** El cerebro es **agnóstico** (`LLM_PROVIDER`): 
   - **Producción:** Anthropic (`console.anthropic.com`, pago por uso). Haiku 4.5 ≈ US$1/US$5 por millón.
   - **Para probar YA (sin pagar Anthropic):** una llave de **DeepSeek** (`LLM_PROVIDER=deepseek`) — usa
     su API OpenAI-compatible. Cambiar de proveedor es una línea del `.env`. ⚠ DeepSeek es un tercero
     (servidores en China): perfecto para pruebas; para producción con dinero real, preferir Claude o
     self-host por soberanía del dato.
2. **Un número con WhatsApp** para el bot (SIM/celular viejo, no toca a Meta).
3. **Un usuario de servicio** en el back del consorcio, con permisos acotados de **solo-lectura** (7):
   `DASHBOARD_VER`, `TOPES_VER`, `DEUDAS_EMPLEADA_VER`, `REPORTES_VER_VENTAS`, `REPORTES_VER_GANADORES`,
   `BALANCE_VER`, y `TICKETS_SCOPE_GLOBAL` (alcance de LECTURA a todo el consorcio — sin él el scope lo
   deja ciego; NO da poderes de escritura). NADA de dinero. Se crea de un paso con el script del back:
   `node scripts/crear_usuario_servicio.js` (imprime `usuario` + `password`). El bot guarda esas
   credenciales (cifradas) y hace login/refresh (el back da accessToken corto + refresh; no hay token
   durable). Alta en el registro: `python -m asistente.alta_consorcio "Consorcio" https://back USUARIO PASSWORD`.

**Directorio (quién es quién):** en Fase 0 se cargan a mano los pocos admins/encargados con
`python -m asistente.agregar_persona <consorcio_id> <telefono> "<Nombre>" <rol>`. El auto-sync del
roster desde el back existe pero requiere darle al bot el permiso `EMPLEADOS_VER` (opcional).

## Correr (cuando lleguen las credenciales)

```bash
cp .env.example .env      # llenar ANTHROPIC_API_KEY, DB, back URL + token de servicio
docker compose up --build # levanta pgvector + asistente (Python) + puente (Node)
# La primera vez, el puente imprime un QR: escanéalo con el WhatsApp del número del bot.
```

## Estado

**Andamiaje en construcción.** Estructura + esqueleto de cada pieza cableado; falta llenar la lógica
fina, las credenciales, y la prueba end-to-end. Ver `docs/` y los `TODO:` en el código.
