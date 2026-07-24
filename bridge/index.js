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

const ASISTENTE_URL = process.env.ASISTENTE_URL || 'http://asistente:8000'
const PORT = Number(process.env.BRIDGE_PORT || 3100)
const logger = pino({ level: 'warn' })

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
  if (r.texto) await sock.sendMessage(jid, { text: r.texto })
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
  app.get('/salud', (_req, res) => res.json({ ok: true, wa: waConnected }))
  app.post('/enviar', async (req, res) => {
    try {
      if (!sock) return res.status(503).json({ error: 'WhatsApp no conectado' })
      const { telefono, texto, documento_base64, documento_nombre } = req.body
      const jid = `${String(telefono).replace(/\D/g, '')}@s.whatsapp.net`
      await enviarA(jid, { texto, documento_base64, documento_nombre })
      res.json({ ok: true })
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
    if (connection === 'open') { waConnected = true; console.log('[bridge] WhatsApp conectado.') }
    if (connection === 'close') {
      waConnected = false
      const code = lastDisconnect?.error?.output?.statusCode
      const reconectar = code !== DisconnectReason.loggedOut
      console.log(`[bridge] conexión cerrada (${code}); ${reconectar ? 'reconectando…' : 'sesión cerrada'}`)
      if (reconectar) iniciar()
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
}

apiEnvio()
iniciar().catch((e) => { console.error('[bridge] fatal:', e); process.exit(1) })
