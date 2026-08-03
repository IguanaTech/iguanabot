# CLAUDE.md — iguana-asistente (el bot)

Bot de WhatsApp y Telegram del consorcio. Python + LangGraph. Es el 5º proyecto de IguanaSuite y
vive en su propio VPS.

## Deploy Configuration (configured by /setup-deploy)
- Platform: VPS `root@69.55.55.179`, Docker Compose en `/root/iguana-asistente`
- Production URL: n/a (habla por WhatsApp y Telegram, no expone web)
- Deploy workflow: `rsync` del código + `docker compose up -d --build asistente`
- Deploy status command: `ssh root@69.55.55.179 "cd /root/iguana-asistente && docker compose ps"`
- Merge method: merge (rama `main`)
- Project type: agente conversacional (Python/LangGraph + pgvector)
- Post-deploy health check: los 4 contenedores arriba (asistente, bridge, pgvector, telegram)

### Custom deploy hooks
- Pre-merge: `python3 -c "import ast; ast.parse(open('asistente/tools.py').read())"` como mínimo
- Deploy trigger:
  ```
  rsync -az --delete --exclude '.git' --exclude '.env' --exclude 'docker-compose*.yml' \
        --exclude 'qr' --exclude '__pycache__' --exclude '.venv' --exclude '*.key' --exclude 'keys' \
        ./asistente/ root@69.55.55.179:/root/iguana-asistente/asistente/
  ssh root@69.55.55.179 "cd /root/iguana-asistente && docker compose up -d --build asistente"
  ```
- Deploy status: `ssh root@69.55.55.179 "cd /root/iguana-asistente && docker compose ps"`
- Health check: que `bridge` siga con días de uptime (si se reinició, hay que re-escanear el QR)

### Reglas duras
1. **El rsync SIEMPRE con esos `--exclude`.** Sin excluir `.env`, `docker-compose*.yml`, `qr` y las
   llaves, se pisa la configuración del servidor y se tumba producción.
2. **Levantá SÓLO el servicio `asistente`.** Si recreás `bridge`, WhatsApp pierde la sesión y hay
   que re-escanear el QR desde el teléfono de Eduardo — o sea, el bot queda mudo hasta que él pueda.
3. **Verificá que el código nuevo esté ADENTRO** del contenedor, no sólo que levantó:
   `docker compose exec -T asistente grep -c <algo_nuevo> /app/asistente/tools.py`.
4. **El bot lee con un token de servicio GLOBAL.** El recorte por persona lo hace ÉL, del lado
   cliente, con `_banca_permitida`. Toda herramienta nueva que devuelva datos de una banca TIENE que
   pasar por ahí, o un encargado acotado ve lo ajeno.
