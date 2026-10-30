#!/bin/sh
set -u
: "${VPS_IP:?Run ./deploy.sh to configure the public IP first}"
: "${ACME_EMAIL:?Certificate account email is missing}"
[ "${ACME_AGREE_TOS:-}" = true ] || { echo 'Certificate terms must be accepted during setup.' >&2; exit 1; }
trap 'exit 0' TERM INT
issue() {
  certbot certonly --non-interactive --agree-tos --email "$ACME_EMAIL" \
    --standalone --preferred-profile shortlived --ip-address "$VPS_IP" \
    --cert-name sensor-backend --key-type rsa --rsa-key-size 2048 \
    --keep-until-expiring --renew-with-new-domains --deploy-hook 'python /opt/sensor-backend-hooks/publish.py'
}
# Initial issuance is retried if DNS/routing/firewall or CA connectivity is unavailable.
until issue; do
  echo 'Certificate issuance failed. Verify inbound port 80, public IP routing, and outbound HTTPS. Retrying in 5 minutes.' >&2
  sleep 300 & wait $! || exit 0
done
# Publish an existing valid certificate too, e.g. when recreating only the shared TLS volume.
RENEWED_LINEAGE=/etc/letsencrypt/live/sensor-backend python /opt/sensor-backend-hooks/publish.py
while :; do
  # Short-lived certificates need frequent renewal checks. Certbot renews only when due.
  if ! certbot renew --non-interactive --deploy-hook 'python /opt/sensor-backend-hooks/publish.py'; then
    echo 'Renewal failed; keeping the existing certificate and retrying in one hour.' >&2
  fi
  sleep 3600 & wait $! || exit 0
done
