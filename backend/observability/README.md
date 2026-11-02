# Run the optional observability stack

From `backend/`, after the normal backend deployment is configured:

```sh
docker compose --env-file config/server.env -f compose.yaml -f compose.observability.yaml up -d --build
```

Use both `-f` arguments for subsequent stack operations. Existing database and
certificate volumes are reused. Nothing is flashed or installed on the device by
this command. Install the updated agent using the normal device installer and
flash `src/main.cpp` using PlatformIO. The diagnostics use the existing device
credentials; no separate credentials belong on the board.

Open `https://YOUR_PUBLIC_IP/grafana/` from any computer and sign in with the
Grafana administrator account. The backend forwards only `/grafana/` requests to
the internal Grafana container, including Live WebSockets. Grafana requires its
own login; anonymous access and sign-up remain disabled. The existing backend
HTTPS certificate covers this path, and Grafana session cookies require HTTPS.

Set `GRAFANA_PUBLIC_URL` in private `config/server.env` if the external address
is different from `https://VPS_IP/grafana/` (including non-default HTTPS ports and IPv6 addresses,
which need brackets). Set `GRAFANA_ADMIN_PASSWORD` before first startup to choose
the initial password. Otherwise Grafana starts with `admin` / `admin` and prompts
for a change. Changing that environment variable later does not reset an account
already stored in the Grafana volume.

The direct Grafana listener stays on server loopback port 3001 for diagnostics.
Browser access uses the public HTTPS path and does not need an SSH tunnel.
Grafana provisions Prometheus, Tempo, Loki, and **Res-AC Observability**. Allow a
minute for initial metrics and about 15 seconds for device diagnostic uploads.

Open a trace from the dashboard or Explore → Tempo. Search for command spans
with `{ span.command.id = "COMMAND_UUID" }`, or samples with
`{ span.reading.id = "READING_ID" }`. Explore → Loki shows JSON logs; expand an
entry's TraceID to open its trace. Trace-to-log links use trace IDs. Device spans
are reconstructed on the server and have `timing.approximate=true` and
`device.id`; do not use them to infer synchronized wall clocks or fan reception.
Device spans are reconstructed by the backend but retain distinct resource
identities: `res-ac-agent` for the Linux agent, `res-ac-avr` for firmware work,
and `res-ac-backend` for API/database work. All share trace IDs across boundaries.

Prometheus evaluates the provisioned alerts; the dashboard shows firing alerts.
Notifications are not sent until you configure contact points/rules in Grafana
(or an Alertmanager integration). Set those destinations privately. The supplied
Prometheus alerts do not automatically become Grafana-managed notification rules.

Metrics and OTLP ports are available only on the Docker network. For debugging:

```sh
docker compose -f compose.yaml -f compose.observability.yaml logs --tail=100 otel-collector tempo loki prometheus
```

Traces and logs retain 7 days; metrics retain 15 days or 2 GiB, whichever comes
first. Tempo/Loki local volumes need disk monitoring; their retention is not a
hard byte quota. Grafana and all three stores use named volumes. Avoid `down -v`
when you want to preserve data. The overlay is intended for a small single-host
deployment; use managed storage or separate hosts for larger installations.

Backend logs go to bounded Docker logs and, when configured, a bounded Loki
sender. Loki failure increments export/drop counters rather than holding API
requests. Agent queues are RAM-only and bounded (128 waiting records plus a
32-record in-flight batch). Reconnection cannot restore events lost to power
loss or queue overflow. Device request success metrics describe delivered
telemetry events; drops can make them incomplete. Interpret them with heartbeat
and drop metrics. Current-session cumulative device values are gauges, not
Prometheus counters; reset on restart. HTTP/device event counters in the backend
reset with its process and use `rate`/`increase`.

Validation: `npm test`, `python3 -m unittest discover -s device/test`, and
`~/.platformio/penv/bin/pio run` from the appropriate project directory. Run the
Docker integration smoke test with `npm run test:observability` from `backend`.
The hardware scenarios and accepted limitations are in ../../observability.md.

Diagnostic batch retry deduplication is bounded and RAM-only on the backend.
A backend restart may produce duplicate diagnostic spans; command execution
and reading insertion retain their existing durable idempotency guarantees.
Device spans include `component=agent` or `component=avr`; backend spans use
`component=backend`. Use these attributes to distinguish the processors even
though all three services export through the backend's shared bounded OTel
processor. Service names describe the source of work, not the exporter location.

When copying source into a restricted server directory, preserve directory
permissions for the Grafana bind mounts. The Grafana image uses UID 472 and
group 0; its provisioning directories and dashboard directory must be readable
and traversable by that identity. Keep private deployment configuration separate.

Each HTTP attempt allocates its own span ID before curl starts and propagates
that ID in `traceparent`. Its diagnostic event reports the same `spanId` and
its collection/command parent, so backend server spans nest under the device
client span. Retries receive new client span IDs within the original trace.
Older agents omit `spanId` and remain compatible, with their older nesting.
Existing stored traces retain their original identities and parentage. Timing
positions remain approximate for reconstructed device spans.
