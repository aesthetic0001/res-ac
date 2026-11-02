# Sensor backend

A single Node.js application serves a minimal authenticated HTML dashboard and
the device API. Node handles HTTPS itself on container port 3000. There is no
reverse proxy. A Certbot companion container owns public port 80 only for ACME
HTTP-01 validation and automatically renews the public-IP certificate.

Requires Docker Engine with Docker Compose v2.24+ on a reachable Linux server.
The application image uses Node 24 and its built-in SQLite module, with OpenTelemetry and Prometheus
instrumentation dependencies. The Seeeduino runs neither Docker nor this Node server.

## Automatic setup and deployment

Copy this `backend` directory to the VPS, then run:

```sh
./deploy.sh
```

The Docker setup wizard discovers candidate public IPs (using network interfaces,
AWS checkip, and ipify), lets you select one or enter another, and asks for:

- Certificate account email and acceptance of Let's Encrypt's terms.
- Public HTTPS port (443 by default; Node still listens internally on 3000).
- Web username and a password, entered without echo.
- Device ID (defaults to `seeeduino-cloud`).

The wizard hashes the password, generates a device token, and writes
`config/server.env` with mode 600. This file and runtime data are ignored by Git
and excluded from Docker builds. Do not commit or share the private configuration.
After setup, the script starts the backend and Certbot. Run `./deploy.sh` again to
restart/update them without repeating the wizard; `./deploy.sh --configure`
changes deployment settings or the administrator credentials. It preserves the
existing device token unless you explicitly supply a replacement `DEVICE_TOKEN`.
Existing browser sessions are invalidated when the admin credentials change.

The selected public IP **must route to this server**. Allow inbound TCP port 80
and your chosen HTTPS port in the VPS firewall/cloud firewall. If this is a home
machine behind a router, forward those ports to it; merely detecting the router's
public IP does not create port forwarding. Private IPs and documentation addresses
cannot obtain public IP certificates. If several public addresses exist, choose
one actually routed to this server. Certificates do not follow an IP change;
rerun `--configure` after changing public IP.

Open `https://YOUR_PUBLIC_IP/` (or include the chosen non-default port). IPv6
addresses use brackets, e.g. `https://[YOUR_IPV6]/`. Sign in with the credentials
entered in the wizard. The page shows current readings, last contact, and recent
commands. Submit a command type, a JSON payload object, and its expiry duration.
The page refreshes every five seconds. Commands are data for a device agent;
the backend does not run shell commands or implement IR transmission itself.

The **Fan remote** provides power on/off (toggle), speed down, and speed up for
the selected device. Each press queues a `fan.command` with a 30-second expiry;
status refreshes more frequently after a press. Controls stay disabled until the
AVR accepts calibration after the user restarts the fan to **factory position:
off** and confirms it. The page shows the AVR's estimated on/off state and speed,
and locks again after a device restart, loss of tracking, or stale state.
A successful IR result means transmission completed, not that the fan received
it. The command form below the remote
remains available for other buttons, ping, and numeric NEC requests.

**Set level** selects Off or levels 1–5. The AVR uses the shortest path around
the five-level cycle. **Automatic temperature curve** provides Quiet, Balanced,
Cooling, and Custom thresholds, a graph with mouse/touch dragging and keyboard
editing, and cooling hysteresis. Thresholds may be equal; only decreasing
temperatures are rejected, and the highest matching level wins at a shared
temperature. Graph edits update the number fields and remain drafts until applied.
The Seeeduino evaluates the curve locally against fresh readings, with at least
30 seconds between automatic adjustments. Manual control disables automatic mode.
See [target levels and temperature curves](../device/IR.md#target-levels-and-temperature-curves)
for preset values, persistence, and the `fan.level`/`fan.curve` payloads.

Check deployment:

```sh
docker compose --env-file config/server.env ps
docker compose --env-file config/server.env logs -f backend certbot
```

Initial certificate issuance can take a few minutes. Node waits for a valid
certificate before opening its HTTPS listener. If issuance fails, Certbot logs the
failure and retries in five minutes; verify port 80/public-IP reachability first.
Stop services with `docker compose --env-file config/server.env down`. Volumes
survive container replacement; **`down -v` deletes persisted data/certificates**.

### Unattended setup

The wizard also accepts these environment variables when provided to `deploy.sh`:
`VPS_IP`, `ACME_EMAIL`, `HTTPS_PORT`, `ACME_AGREE_TOS=true`, `ADMIN_USERNAME`,
`ADMIN_PASSWORD`, `DEVICE_ID`, and optionally `DEVICE_TOKEN`. Supply secrets
through your deployment system's secret environment, not in committed files or
command-line arguments. Interactive setup is preferred for a manual deployment.

## Certificates and automatic renewal

Let's Encrypt IP certificates use the `shortlived` profile (about six days).
Certbot 5.9 requests an RSA certificate using `--ip-address` and the standalone
HTTP-01 challenge. The companion checks for renewal every hour; Certbot only
renews when due. Failed checks are retried on the next loop. The named
`letsencrypt` volume holds the ACME account and renewal configuration.

A deploy hook atomically publishes certificate and private-key copies to the
shared `sensor-tls` volume, readable only by the Node user (UID 1000). The backend
mounts that volume read-only and checks it every two seconds. It validates a
matching, currently valid pair before calling `server.setSecureContext()`;
partial/broken updates retain the previous context and retry. No backend restart
is required and open long polls are not interrupted. The private key is never
placed in the image, dashboard, API responses, or logs.

Test renewal on the actual VPS after initial production issuance:

```sh
docker compose --env-file config/server.env exec certbot certbot renew --dry-run
```

That validates the renewal path against staging without replacing the production
certificate. TLS reload behavior is also covered by an automated test using two
local test certificates. Real issuance/renewal requires the actual publicly
reachable deployment; local tests cannot prove the CA can reach your VPS.

The IP certificate chain and HTTPS TLS client on the Seeeduino still need a
compatibility check against the deployed server. Node requires TLS 1.2 or newer;
the eduroam TLS implementation is separate from the board's HTTPS client.

References:
- https://letsencrypt.org/2026/03/11/shorter-certs-certbot/
- https://eff-certbot.readthedocs.io/en/stable/using.html#renewing-certificates

## Device API

All device endpoints require `Authorization: Bearer DEVICE_TOKEN`, scoped
to the configured device ID. Retrieve the token locally from the private
`config/server.env` and put it in the Linux device agent's private configuration.
Browser sessions do not authenticate device requests, and device tokens do not
authenticate administrative requests. All requests and responses use HTTPS.

### Post a reading

`POST /api/devices/seeeduino-cloud/readings`

```json
{
  "readingId": "unique-sample-id",
  "temperatureC": 22.5,
  "humidity": 48,
  "recordedAt": "2026-10-30T16:00:00Z"
}
```

`readingId` and `recordedAt` are optional; supplying a stable `readingId` makes
upload retries idempotent per device. Temperature and humidity must be finite
numbers; malformed/failed sensor readings should not be posted as valid samples.

### Long-poll commands

`GET /api/devices/seeeduino-cloud/commands`

Returns immediately when a command is ready; otherwise holds the request for up
to **30 seconds**, then returns `204 No Content`. Open another request immediately
after a normal response. Use a client timeout of about 40 seconds and back off
on network/server errors. Only one command poll may be open per device.
Sensor uploads should run independently of the Linux agent's poller.

```json
{
  "commands": [{
    "id": "command-uuid",
    "type": "ir.send",
    "payload": {"protocol": "NEC", "address": 0, "command": 16},
    "createdAt": "2026-10-30T16:00:00.000Z",
    "expiresAt": "2026-10-30T16:05:00.000Z"
  }]
}
```

Delivery leases a command for 60 seconds. A lost/unacknowledged command can be
redelivered with the **same ID** after the lease, until its expiry. Commands for a
device execute in queue order; later commands wait for the first one's final
result or expiry. Default expiry is five minutes, configurable per command.

The device must persist handled command IDs/results and retry results instead of
repeating completed actions. It must check expiry before starting an action.
This is at-least-once delivery, not guaranteed exactly-once IR transmission:
a power loss between a physical action and recording its result remains ambiguous.
Actions expected to take longer than 60 seconds need a longer lease/progress
extension before being supported. Expired commands reject later final results.

### Post a final command result

The Seeeduino agent supports `device.ping`, session-bound `fan.calibrate`,
`fan.command`, and NEC `ir.send`. See the [Woozoo setup](../device/IR.md)
for wiring, the initial button profile, and delivery limitations. IR success
confirms transmission only; the fan provides no acknowledgment.

The device also posts its live estimate to `POST /api/devices/:id/fan-state`,
authenticated with the same device token. Fields are `sessionId` (32 hex digits
or null during initialization), `calibrated` (boolean), `power` (`on`, `off`,
or `unknown`), `speed` (1–5 while on, 0 while off, null when uncalibrated), and
`revision` (unsigned 32-bit integer). State is stored separately from readings.
The admin snapshot exposes it as `device.fanState`, including server
`receivedAt` in epoch milliseconds. Remote/calibration requests require the
current `sessionId` and telemetry no older than 30 seconds. `fan.calibrate`
additionally requires `confirmOff: true`. An old revision within the same
session is rejected. Repeated telemetry refreshes freshness without adding
historical readings.
New firmware reports `firmwareVersion: "v4"`, `controlSource`, and an `automatic`
object with `enabled`, `thresholdsC`, `hysteresisC`, `status`, `targetLevel`, and
`minAdjustmentSeconds`. The dashboard uses this device-reported state to show
whether a saved curve is active, cooling down, or waiting for calibration/readings.

`POST /api/devices/seeeduino-cloud/commands/COMMAND_ID/result`

```json
{"status": "succeeded", "result": {"message": "IR sent"}}
```

Use `failed` for an execution failure or unsupported command. Identical result
retries return 200; a conflicting final result returns 409. Retrieving a command
does not mark it complete. Results are accepted only after delivery and before
expiry. The agent must not report successful IR execution until the AVR has
actually completed it.

## Browser/API authentication

The dashboard uses a password hash and durable server-side sessions, with
HttpOnly/SameSite=Strict/Secure cookies in HTTPS mode. Administrative mutations
require both the session's CSRF token and a matching Origin header. Login is
rate-limited. The dashboard's command creation endpoint is
`POST /api/admin/devices/:id/commands`; the endpoints above are device-facing.

SQLite in the `sensor-data` volume stores readings, commands, results, and
sessions. Back up that volume consistently (stop the backend before copying all
its files, or use a SQLite-aware backup), plus `config/server.env` and the
`letsencrypt` volume. For the initial small deployment there is no automatic
reading retention; plan pruning/backups as the history grows. This image runs a
single Node process; multiple replicas need shared storage and poll notifications.

## Development and verification

```sh
npm test
```

For the Docker-specific Certbot mount and publication regression check:

```sh
npm run test:docker
```

This uses a disposable Compose project, performs no certificate issuance, and
removes its test volumes afterward. The hook scripts mount at
`/opt/sensor-backend-hooks`; `/opt/certbot` belongs to Certbot's Python package
installation and must not be shadowed by a bind mount.

Tests cover authentication/CSRF, device isolation, readings and duplicate uploads,
long-poll timeout/wakeup/disconnect, ordered leased delivery and result retries,
command expiry, persistence, credential rotation, public-IP selection, and TLS
certificate reload without restarting the server.

For local-only HTTP development, explicitly set `HTTP_DEV=true` and provide
`ADMIN_USERNAME`, `ADMIN_PASSWORD_HASH`, `DEVICE_ID`, `DEVICE_TOKEN`, and `DATA_DIR`,
then run `npm start`. Do not use that mode for a public deployment. The production
wizard/Compose setup uses direct HTTPS and does not enable HTTP development mode.

## Reading history graphs

The authenticated dashboard includes temperature and relative humidity charts.
Select a device and the last hour, 6 hours, 24 hours (default), or 7 days. Charts
refresh every 5 seconds alongside the current readings. Hover/touch a chart or
focus it and use the arrow keys (Home/End also work) to inspect a plotted value.
Empty ranges and loading failures are shown explicitly; missing intervals break
the line rather than implying continuous readings.

History comes from the existing SQLite readings table; no device firmware or
database migration is needed. The admin-session-only endpoint is
`GET /api/admin/devices/:id/readings?hours=24`. It returns the time bounds,
`bucketSeconds`, total `sampleCount`, and up to 600 chronological points with
`receivedAt` (Unix milliseconds), `temperatureC`, `humidity`, and `sampleCount`.
Each point averages all samples in its interval. Raw readings remain in SQLite.
The indexed query uses server receipt time, displayed in the browser's local
timezone, so delayed uploads appear when they arrive and device clock errors do
not hide readings. Humidity uses a fixed 0–100% scale; temperature scales to the
selected data.

Deploy the updated backend image to see the charts; existing data volumes and
private configuration can be reused:

```sh
docker compose up -d --build backend
```

## Observability

The [design](../observability.md) and [deployment guide](observability/README.md)
describe the optional Compose overlay for Grafana, Prometheus, Tempo, Loki, and
the OpenTelemetry Collector. It instruments request/SQLite operations, persists
command trace context, and accepts bounded authenticated device diagnostics at
`POST /api/devices/:id/telemetry`. No monitoring service port is public. The
separate metrics listener is enabled only when `METRICS_PORT` is configured.

Use Node.js 22.15+ for local development. Run `npm ci` before tests/start.
