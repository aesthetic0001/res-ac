#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
command -v docker >/dev/null || { echo 'Install Docker Engine and Docker Compose first.' >&2; exit 1; }
docker compose version >/dev/null
mkdir -p config
chmod 700 config
if [ ! -s config/server.env ] || [ "${1:-}" = '--configure' ]; then
  if [ ! -t 0 ] && { [ -z "${VPS_IP:-}" ] || [ -z "${ACME_EMAIL:-}" ] || [ -z "${HTTPS_PORT:-}" ] || [ "${ACME_AGREE_TOS:-}" != true ] || [ -z "${ADMIN_USERNAME:-}" ] || [ -z "${ADMIN_PASSWORD:-}" ] || [ -z "${DEVICE_ID:-}" ]; }; then
    echo 'First setup needs an interactive terminal, or all documented unattended setup variables.' >&2
    exit 1
  fi
  # The wizard writes files as the invoking host user, not as root.
  docker compose --profile setup run --rm --build --user "$(id -u):$(id -g)" \
    -e VPS_IP -e ACME_EMAIL -e HTTPS_PORT -e ACME_AGREE_TOS \
    -e ADMIN_USERNAME -e ADMIN_PASSWORD -e DEVICE_ID -e DEVICE_TOKEN setup
fi
docker compose --env-file config/server.env up -d --build backend certbot
echo 'Started. First certificate issuance can take a few minutes.'
echo 'Follow startup and renewal logs with:'
echo '  docker compose --env-file config/server.env logs -f backend certbot'
echo 'Back up the database and certificate volumes; never use down -v unless you intend to delete them.'
