FROM node:20-slim

# git + node-gyp toolchain: Baileys jala libsignal-node desde git (con addon nativo).
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates git python3 make g++ \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY package.json .
# libsignal-node se referencia por SSH (ssh://git@github.com/...) que falla sin llave/ssh. Reescribimos
# a HTTPS (repo público) vía las env vars GIT_CONFIG_*: git las aplica SIEMPRE al resolver la dep, sin
# importar que npm le ponga GIT_CONFIG_NOSYSTEM o un HOME propio al git hijo (por eso --global/--system
# no bastan). A prueba de balas.
ENV GIT_CONFIG_COUNT=1 \
    GIT_CONFIG_KEY_0="url.https://github.com/.insteadOf" \
    GIT_CONFIG_VALUE_0="ssh://git@github.com/"
RUN npm install --omit=dev --no-audit --no-fund

COPY index.js .

# La sesión de WhatsApp se persiste acá (volumen en docker-compose).
RUN mkdir -p /app/auth

CMD ["node", "index.js"]
