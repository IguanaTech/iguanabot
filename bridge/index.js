// Puente WhatsApp ↔ asistente (Fase 0, Baileys sobre un número normal — sin Meta).
//
// Dos direcciones:
//  · Entrante: llega un mensaje (texto/voz) → lo POSTeo al asistente → me devuelve la respuesta
//    (texto y/o voz y/o documento PDF) → la mando de vuelta al que escribió.
//  · Saliente proactivo: el asistente (scheduler del cierre) me POSTea a /enviar para mandarle un
//    reporte a un cliente que NO escribió (ojo: bot-inicia → riesgo de baneo en Baileys; ver README).
//
// La primera vez imprime un QR: escanéalo con el WhatsApp del número del bot (Dispositivos vinculados).

import makeWASocket, {
  useMultiFileAuthState,
  downloadMediaMessage,
  fetchLatestBaileysVersion,
  Browsers,
  DisconnectReason,
} from '@whiskeysockets/baileys'
import express from 'express'
import pino from 'pino'
import qrcode from 'qrcode-terminal'
import QRImage from 'qrcode'
import fs from 'node:fs'
const ASISTENTE_URL = process.env.ASISTENTE_URL || 'http://asistente:8000'
const PORT = Number(process.env.BRIDGE_PORT || 3100)
const logger = pino({ level: process.env.BRIDGE_LOG_LEVEL || 'warn' })

let sock = null // referencia viva al socket de WhatsApp (la usan tanto lo entrante como /enviar)
let waConnected = false // estado REAL de la conexión (para /salud y el watchdog del asistente)

async function preguntarAlAsistente(payload) {
  const resp = await fetch(`${ASISTENTE_URL}/mensaje`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  if (!resp.ok) throw new Error(`asistente respondió ${resp.status}`)
  return resp.json() // { texto, audio_base64?, documento_base64?, documento_nombre? }
}

// Manda una respuesta (texto + opcional voz + opcional PDF) a un jid.
async function enviarA(jid, r) {
  if (r.documento_base64) {
    await sock.sendMessage(jid, {
      document: Buffer.from(r.documento_base64, 'base64'),
      fileName: r.documento_nombre || 'reporte.pdf',
      mimetype: 'application/pdf',
    })
  }
  if (r.audio_base64) {
    await sock.sendMessage(jid, {
      audio: Buffer.from(r.audio_base64, 'base64'),
      ptt: true,
      mimetype: 'audio/ogg; codecs=opus',
    })
  }
  if (r.texto) {
    const sent = await sock.sendMessage(jid, { text: r.texto })
    const msgId = sent?.key?.id
    console.log(`[bridge] TX a ${jid} → id=${msgId} status=${sent?.status}`)
    // Esperamos el acuse REAL antes de dar el envío por bueno.
    const ack = await esperarAck(msgId)
    if (!ack.entregado) anotarNoEntregado({ jid, msgId, motivo: ack.motivo })
    return ack
  }
  return { entregado: true, status: null }   // solo audio/documento: sin seguimiento de texto
}

// ── Registro de ENTREGA (auditoría 2026-07-25) ────────────────────────────────
// `sendMessage` resuelve en status=1 (PENDING): eso NO es entregado. El rechazo
// (p.ej. error 463) llega DESPUÉS por `messages.update`. Antes /enviar contestaba
// {ok:true} en cuanto salía, el scheduler lo marcaba "ya-enviado" y NADIE
// reintentaba: las alarmas de dinero se perdían en silencio con estado de
// entregadas. Ahora esperamos el acuse real (status>=2) antes de confirmar.
const ACK_TIMEOUT_MS = Number(process.env.BRIDGE_ACK_TIMEOUT_MS) || 15000
const pendientesAck = new Map()   // msgId → {resolve, timer}
let reintentos = 0                // backoff de reconexión
const noEntregados = []           // cola muerta para diagnóstico (últimos 50)

function esperarAck(msgId) {
  if (!msgId) return Promise.resolve({ entregado: false, motivo: 'sin id de mensaje' })
  return new Promise((resolve) => {
    const timer = setTimeout(() => {
      pendientesAck.delete(msgId)
      resolve({ entregado: false, motivo: `sin acuse en ${ACK_TIMEOUT_MS} ms` })
    }, ACK_TIMEOUT_MS)
    pendientesAck.set(msgId, { resolve, timer })
  })
}

function resolverAck(msgId, status, extra) {
  const p = pendientesAck.get(msgId)
  if (!p) return
  clearTimeout(p.timer)
  pendientesAck.delete(msgId)
  // 0=ERROR 1=PENDING 2=SERVER_ACK 3=DELIVERY_ACK 4=READ. WhatsApp lo ACEPTÓ desde 2.
  p.resolve(status >= 2
    ? { entregado: true, status }
    : { entregado: false, motivo: extra || `WhatsApp devolvió status=${status}`, status })
}

function anotarNoEntregado(item) {
  noEntregados.unshift({ ...item, en: new Date().toISOString() })
  if (noEntregados.length > 50) noEntregados.pop()
  console.error(`[bridge] NO ENTREGADO → ${JSON.stringify(item)}`)
}

function textoDe(msg) {
  const m = msg.message
  if (!m) return null
  return m.conversation || m.extendedTextMessage?.text || null
}

// ── API para envíos proactivos (el asistente le pega acá) ─────────────────────
function apiEnvio() {
  const app = express()
  app.use(express.json({ limit: '15mb' })) // PDFs en base64
  // `wa` = estado REAL de la conexión (waConnected), no `!!sock`: el socket sigue existiendo tras un
  // device_removed → `!!sock` daba wa:true con la sesión muerta (falso positivo). El watchdog del
  // asistente lee esto para el heartbeat.
  // `wa` = socket conectado. `entregando` = además WhatsApp ACEPTA lo que mandamos
  // (no hay envíos recientes sin acuse). Con la cuenta restringida (463) el socket
  // sigue abierto pero nada llega: `wa:true` solo hacía que el CRM pintara el bot
  // en verde mientras estaba mudo. (auditoría 2026-07-25)
  app.get('/salud', (_req, res) => res.json({
    ok: true,
    wa: waConnected,
    entregando: waConnected && noEntregados.length === 0,
    no_entregados: noEntregados.length,
    ultimo_fallo: noEntregados[0] || null,
  }))
  app.post('/enviar', async (req, res) => {
    try {
      if (!sock) return res.status(503).json({ error: 'WhatsApp no conectado' })
      const { telefono, texto, documento_base64, documento_nombre, jid: jidRaw } = req.body
      // jidRaw = override para diagnóstico (ej. enviar a un @lid). Normal: teléfono → @s.whatsapp.net.
      const jid = jidRaw || `${String(telefono).replace(/\D/g, '')}@s.whatsapp.net`
      const ack = await enviarA(jid, { texto, documento_base64, documento_nombre })
      // 502 cuando WhatsApp NO aceptó el mensaje: así el llamador (scheduler/watcher)
      // NO lo marca como enviado y puede reintentar. Antes siempre era 200 y las
      // alarmas rechazadas por el 463 quedaban registradas como entregadas.
      if (ack && ack.entregado === false) {
        return res.status(502).json({ ok: false, entregado: false, error: ack.motivo })
      }
      res.json({ ok: true, entregado: true })
    } catch (e) {
      res.status(500).json({ error: e.message })
    }
  })
  app.listen(PORT, () => console.log(`[bridge] API de envío en :${PORT}`))
}

// ── WhatsApp (Baileys) ────────────────────────────────────────────────────────
async function iniciar() {
  const { state, saveCreds } = await useMultiFileAuthState('/app/auth')
  // WA rechaza (405) si la versión de WhatsApp Web es stale → hay que pasarle la última.
  const { version } = await fetchLatestBaileysVersion()
  console.log(`[bridge] WA Web version: ${version.join('.')}`)
  sock = makeWASocket({
    version,
    auth: state,
    logger,
    printQRInTerminal: false,
    browser: Browsers.appropriate('Chrome'),
  })

  sock.ev.on('creds.update', saveCreds)

  // CÓDIGO DE EMPAREJAMIENTO (más robusto que el QR: el QR de WA rota cada ~20s y la carrera
  // generar→mostrar→escanear no da tiempo). Si hay número del bot configurado y aún no está
  // registrado, se pide un código de 8 letras que dura varios minutos y se TECLEA en WhatsApp
  // (Dispositivos vinculados → Vincular un dispositivo → Vincular con número de teléfono).
  if (process.env.BRIDGE_PAIR_NUMBER && !sock.authState.creds.registered) {
    setTimeout(async () => {
      try {
        const num = String(process.env.BRIDGE_PAIR_NUMBER).replace(/\D/g, '')
        const code = await sock.requestPairingCode(num)
        console.log(`\n=== CÓDIGO DE EMPAREJAMIENTO: ${code} ===`)
        console.log('(WhatsApp del bot → Dispositivos vinculados → Vincular un dispositivo → Vincular con número de teléfono)\n')
      } catch (e) {
        console.error('[bridge] requestPairingCode falló:', e.message)
      }
    }, 3000)
  }

  sock.ev.on('connection.update', ({ connection, lastDisconnect, qr }) => {
    if (qr) {
      console.log('\n=== Escanea este QR con el WhatsApp del BOT (Dispositivos vinculados) ===\n')
      qrcode.generate(qr, { small: true })
      // También lo escribimos como PNG (se sobreescribe en cada rotación → siempre el fresco).
      QRImage.toFile('/app/qrout/qr.png', qr, { width: 512, margin: 2 })
        .then(() => console.log('[bridge] QR PNG actualizado: qr/qr.png'))
        .catch((e) => console.error('[bridge] no pude escribir el PNG:', e.message))
    }
    if (connection === 'open') { waConnected = true; reintentos = 0; console.log('[bridge] WhatsApp conectado.') }
    if (connection === 'close') {
      waConnected = false
      const code = lastDisconnect?.error?.output?.statusCode
      const reconectar = code !== DisconnectReason.loggedOut
      console.log(`[bridge] conexión cerrada (${code}); ${reconectar ? 'reconectando…' : 'sesión cerrada'}`)
      if (reconectar) {
        // Backoff: antes se re-entraba en seco desde el handler del socket viejo,
        // así que una caída repetida martillaba fetchLatestBaileysVersion.
        reintentos += 1
        const espera = Math.min(30000, 1000 * 2 ** Math.min(reintentos, 5))
        console.log(`[bridge] reintento ${reintentos} en ${espera} ms`)
        setTimeout(() => { iniciar().catch((e) => console.error('[bridge] fallo al reconectar:', e.message)) }, espera)
      } else {
        // loggedOut: la sesión murió de verdad. Antes el proceso quedaba VIVO
        // sirviendo Express, así que `restart: unless-stopped` nunca actuaba y ni
        // un restart manual recuperaba (creds.registered seguía true → sin QR).
        // Borramos las credenciales y salimos: Docker reinicia y pide pareo.
        try {
          fs.rmSync('/app/auth', { recursive: true, force: true })
          console.error('[bridge] sesión cerrada por WhatsApp: credenciales borradas, saliendo para re-parear.')
        } catch (e) {
          console.error('[bridge] no pude borrar /app/auth:', e.message)
        }
        process.exit(1)
      }
    }
  })

  sock.ev.on('messages.upsert', async ({ messages, type }) => {
    if (type !== 'notify') return
    for (const msg of messages) {
      if (msg.key.fromMe || !msg.message) continue
      const jid = msg.key.remoteJid
      if (!jid || jid.endsWith('@g.us')) continue // grupos: fuera en Fase 0
      // WhatsApp migró a @lid (identificador de privacidad): remoteJid puede ser un LID
      // (ej. 268933789696060@lid) en vez del teléfono. Dos cosas distintas:
      //  · IDENTIDAD: el asistente reconoce por NÚMERO → usamos el teléfono real de key.senderPn
      //    (attr sender_pn del stanza). Con el LID el asistente no reconocía el contacto → no respondía.
      //  · ENTREGA: hay que responder al MISMO chat/sesión por el que llegó (el @lid). Enviar al PN
      //    `@s.whatsapp.net` no entra en la sesión @lid activa (WhatsApp lo acepta pero no lo entrega).
      const pn = msg.key.senderPn || jid           // número para identificar el contacto
      const telefono = pn.split('@')[0]
      console.log(`[bridge] entrante jid=${jid} senderPn=${msg.key.senderPn || '—'} → telefono=${telefono}`)
      try {
        const audioMsg = msg.message.audioMessage
        let payload
        if (audioMsg) {
          const buf = await downloadMediaMessage(msg, 'buffer', {}, { logger, reuploadRequest: sock.updateMediaMessage })
          payload = { telefono, audio_base64: buf.toString('base64') }
        } else {
          const texto = textoDe(msg)
          if (!texto) continue
          payload = { telefono, texto }
        }
        const r = await preguntarAlAsistente(payload)
        await enviarA(jid, r)   // responder al chat original (el @lid), la sesión activa
      } catch (e) {
        console.error('[bridge] error atendiendo mensaje:', e.message)
        try { await sock.sendMessage(jid, { text: 'Se me complicó procesar eso. Intenta de nuevo.' }) } catch {}
      }
    }
  })

  // DIAGNÓSTICO: estado de ENTREGA de lo que el bot manda. WhatsApp emite messages.update con
  // status (0=ERROR, 1=PENDING, 2=SERVER_ACK, 3=DELIVERY_ACK, 4=READ). Si un TX se queda en
  // PENDING/ERROR y nunca sube a SERVER_ACK, WhatsApp NO lo está aceptando (no es el bot).
  sock.ev.on('messages.update', (updates) => {
    for (const u of updates) {
      if (u.update?.status !== undefined || u.update?.messageStubType !== undefined) {
        console.log(`[bridge] UPDATE id=${u.key?.id} fromMe=${u.key?.fromMe} status=${u.update?.status} stub=${u.update?.messageStubType ?? ''}`)
        if (u.update?.status !== undefined) resolverAck(u.key?.id, u.update.status)
      }
    }
  })
  sock.ev.on('message-receipt.update', (updates) => {
    for (const u of updates) {
      console.log(`[bridge] RECEIPT id=${u.key?.id} for=${u.key?.remoteJid} ts=${u.receipt?.receiptTimestamp || u.receipt?.readTimestamp || '—'}`)
    }
  })
}

apiEnvio()
iniciar().catch((e) => { console.error('[bridge] fatal:', e); process.exit(1) })
