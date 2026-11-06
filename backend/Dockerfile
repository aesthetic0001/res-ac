FROM node:24.22.2-bookworm-slim AS sqlite-build
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends g++ make python3 \
    && rm -rf /var/lib/apt/lists/*
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts
COPY deploy/build-sqlite.mjs ./deploy/build-sqlite.mjs
RUN npm run build:sqlite && npm prune --omit=dev --ignore-scripts \
    && rm -rf node_modules/better-sqlite3/build node_modules/better-sqlite3/deps \
       node_modules/better-sqlite3/src node_modules/better-sqlite3/prebuilds

FROM node:24.22.2-bookworm-slim
ENV NODE_ENV=production PORT=3000 DATA_DIR=/data TLS_DIR=/run/sensor-tls
ENV RESAC_SQLITE_SYNC=data
WORKDIR /app
COPY --chown=node:node package.json package-lock.json ./
COPY --from=sqlite-build --chown=node:node /build/node_modules ./node_modules
COPY --from=sqlite-build --chown=node:node /build/native/sqlite.node /build/native/build.json ./native/
COPY --chown=node:node src ./src
COPY --chown=node:node public ./public
COPY --chown=node:node deploy ./deploy
RUN mkdir -p /data /run/sensor-tls && chown node:node /data /run/sensor-tls
USER node
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=5m --retries=3 CMD ["node", "deploy/healthcheck.mjs"]
CMD ["node", "src/server.mjs"]
