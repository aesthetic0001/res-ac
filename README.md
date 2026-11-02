# Res-AC

Seeeduino Cloud temperature/humidity monitoring and a portable Node.js backend
for sensor readings and queued hardware commands.

## Backend deployment

With Docker Engine and Docker Compose installed on a publicly reachable Linux
host:

```sh
cd backend
./deploy.sh
```

The Docker setup wizard detects candidate public IPs, prompts when a choice is
needed, and asks for certificate account details and web admin credentials.
Node serves the dashboard and HTTPS API directly. A Certbot companion handles
IP-certificate issuance and renewal; no reverse proxy or domain is required.
TCP port 80 and the chosen HTTPS port must be routed to the host.

[Backend documentation](backend/README.md) describes configuration, Docker
volumes, renewal, authentication, and the device-facing endpoints:

- `POST /api/devices/:id/readings`
- `GET /api/devices/:id/commands` (30-second long poll)
- `POST /api/devices/:id/commands/:commandId/result`
- `POST /api/devices/:id/fan-state` (live estimate and calibration session)

The minimal web dashboard has a separate authenticated command-creation API.
SQLite persists readings, command delivery/results, and web sessions. Devices
use their own tokens; browser logins use server-side cookie sessions.

## Device status

`src/main.cpp` reads a DHT11 on digital pin 3 and publishes temperature/humidity
through the internal Arduino Bridge. A Linux-side agent posts readings over
verified HTTPS every 10 seconds, buffers samples during short outages, and
retries with stable IDs to avoid duplicate records. Device credentials and the
backend address are configured privately at installation time.

[Device setup and reporting](device/README.md) covers installation, diagnostics,
and retry behavior. The original USB-to-Linux terminal is preserved as a
[separate recovery project](recovery/serial-terminal/README.md). The agent also
long polls commands and supports `device.ping`, named `fan.command` buttons, and
numeric NEC `ir.send` requests. The Arduino acknowledges completed transmissions;
this does not confirm the fan received them. [Woozoo IR setup](device/IR.md)
covers the emitter wiring, button profile, and USB serial bench test. Remote
control requires an AVR-confirmed calibration after restarting the fan to
factory position: off; the dashboard displays the AVR's estimated power/speed.
Target levels use the shortest route around the five-speed cycle. Editable
temperature presets run locally on the Seeeduino, with hysteresis, fresh-sensor
checks, and a minimum adjustment interval; manual control disables automatic mode.

[Eduroam tooling](EDUROAM.md) describes the credential/checking helpers without
including a deployed network profile or institution-specific settings.

## Development

```sh
cd backend
npm test
```

Requires Node.js 22.15+ and OpenSSL for the TLS certificate-rotation test. The
Docker application image uses Node.js 24. Test credentials are generated at run
time. Tests cover authentication, token isolation, polling, command delivery,
expiry, persistence, and certificate reloads.

## Public-repository boundaries

Generate deployment values at setup time. Do not add server IPs, institution
identities, passwords, tokens, password hashes, private keys, certificates,
databases, network dumps, logs, or private notes to this public repository.
`backend/config/server.env`, runtime data, and `private/` are ignored. The
tracked example file contains placeholders only.

Git ignore rules do not remove files already staged or committed; inspect the
staged diff before publishing. Treat a secret that has entered public Git
history as exposed and rotate it.

## Observability

[Observability design](observability.md) covers firmware, agent, and backend
traces and metrics. The [optional Grafana stack](backend/observability/README.md)
provides Prometheus metrics, Tempo traces, Loki logs, dashboards, and alert rules.
