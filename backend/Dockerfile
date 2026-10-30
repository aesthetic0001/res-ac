FROM node:24-bookworm-slim
ENV NODE_ENV=production PORT=3000 DATA_DIR=/data TLS_DIR=/run/sensor-tls
WORKDIR /app
COPY --chown=node:node package.json ./
COPY --chown=node:node src ./src
COPY --chown=node:node public ./public
COPY --chown=node:node deploy ./deploy
RUN mkdir -p /data /run/sensor-tls && chown node:node /data /run/sensor-tls
USER node
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=5m --retries=3 CMD ["node", "deploy/healthcheck.mjs"]
CMD ["node", "src/server.mjs"]
