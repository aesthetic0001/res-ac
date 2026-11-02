# Observability

## Purpose and architecture

Explain failures across AVR firmware, the Linux agent, HTTPS, command delivery,
and SQLite. Metrics describe rates and resource pressure; traces describe each
operation; structured logs describe outcomes. Grafana queries Prometheus
(metrics), Tempo (traces), and Loki (logs). An OpenTelemetry Collector receives
backend OTLP traces. A bounded backend sender forwards structured operation
logs to Loki. All components are optional at runtime: failed telemetry
must not prevent collection or control.

The AVR reports compact Bridge diagnostics, not OTLP. The Python 2.7-compatible
agent buffers bounded diagnostic records in RAM and submits them through the
existing authenticated HTTPS API. The backend validates them and creates spans.
Device telemetry is never exposed as a public OTLP ingestion endpoint.

## Traces and correlation

Commands retain trace context in SQLite and carry it in their delivery envelope.
Enqueue, queue wait/delivery, agent execution, AVR execution, result upload, and
result persistence are separate spans. Redelivery keeps the command ID and trace
context; each attempt is separate. Saved command results retain trace context
across agent restart. A timeout can mean uncertain physical execution. IR success
means transmission completed, never that the fan acknowledged it.

Each unique sensor sample receives trace context when collected by the agent.
Read diagnostics, Bridge collection, buffer wait, upload attempts, and database
insertion share it. Retries preserve the reading ID and trace context. Failed
reads and Bridge failures also generate diagnostics without requiring a valid
sample. AVR durations use millis() subtraction, safe across timer rollover.

With event-capable firmware, `bridge.collect` measures local time from decoding
the pushed sample event (or recovery snapshot) to processing that sample. It
includes time waiting in the agent's event queue, but does not measure AVR-to-Linux
transport time before reception. This replaces timing several synchronous
datastore reads, so its duration is not directly comparable to the old collector.
The event queue retains the sensor diagnostic paired with each sample. Sensor
execution still has its own `sensor.read` duration. Reads by all agent components
use one shared Bridge cache populated by an initial snapshot and Mailbox events.
Span resource identities are `res-ac-backend`, `res-ac-agent`, and `res-ac-avr`.
Three fixed tracer providers share one bounded processor/exporter. Device IDs
and agent sessions remain attributes rather than creating a provider per device.
The agent allocates a distinct HTTP client span ID before each attempt, sends
it in `traceparent`, and includes `spanId` in the diagnostic event. Backend
server spans use that client span as their parent. Result persistence retains
this HTTP ancestry when it belongs to the saved command trace; old clients
still fall back to the persisted command context.

The agent generates explicit root IDs for sample collection, local commands,
and standalone HTTP requests; the backend reconstructs those root spans with
the same IDs. Device event timestamps are reconstructed from elapsed monotonic time at upload;
they are approximate, and exclude network transit. No distributed clock accuracy
is implied. A record lost during a device power failure cannot be recovered.

Device events and HTTP request logs carry trace IDs, operation names, outcome,
and IDs when appropriate. Command spans and corresponding logs also include
`command.type` and `command.source` (`manual` for backend-queued commands,
`automatic` for device-local fan adjustments). Supported commands also include
`command.payload`: a JSON object in logs and JSON text in span attributes. It
contains the known command parameters (button, target level, calibration or curve
settings, IR parameters, and fan session ID); unrelated fields and invalid values
are omitted. Logs have explicit severity levels so
Loki does not infer an error from a field name such as `error.kind`.
Never record tokens, headers, cookies, passwords,
SQL values, unrecognized command fields, URLs, or raw exceptions. IDs belong in traces/logs,
not metric labels. Known operation names and route templates bound cardinality.

## Metrics

- HTTP request counts by route/method/status, latency histograms, aborted requests.
  Grafana derives successful-request percentages from counters; no traffic is no
  data. Long polls are separate from ordinary request latency.
- Device request attempts, successes, latency and curl failures (DNS, connection,
  TLS, timeout, other), including failures that never reached the server.
- Command backlog, terminal outcomes, delivery attempts and queue/execute timing.
- SQLite main and WAL bytes, rows by table, operation durations and errors.
  Collect expensive statistics periodically, not on every scrape.
- Backend process CPU/RAM, event-loop delay, filesystem capacity, TLS expiry.
- Linux device CPU utilization, available/total RAM, agent RSS, uptime and free
  storage. AVR instrumentation reports hardware operation timings and outcomes;
  it does not pretend Linux CPU/RAM describes the AVR.
- Reading freshness, sensor failures, pending samples, dropped samples, telemetry
  queue depth and dropped diagnostics, and diagnostic heartbeat age.

Device cumulative counters are session-scoped: agent restart resets them. Export
only the latest session for each configured device. Stale resource values must be
interpreted alongside telemetry heartbeat age. Last contact and last successful
sample are distinct: command polls can succeed while the sensor fails.

## Failure handling and retention

Bound agent diagnostics in RAM, independently of the reading queue. Send small
batches asynchronously with backoff and retain failed batches for retry. A
collector failure must not block API responses. Export queues and request timeouts
are bounded. Command outcomes remain durable in SQLite regardless of tracing.
Telemetry is best effort, not a forensic guarantee; report drops and heartbeat
loss. Start with full tracing at this deployment's small volume. Limit history to
7 days for traces/logs and 15 days for metrics; adjust disk budgets as needed.

### Diagnosing device HTTP failures

Each HTTP attempt retains curl's cumulative DNS, TCP-connect, TLS, first-byte,
and total times as `http.timing.*_ms`. These are milestones from request start,
not independent durations to add together. Failed attempts also include
`error.stage` and an allowlisted `error.reason` extracted from curl's error
output (for example `connection_refused`, `network_unreachable`,
`host_unreachable`, or `tls_certificate`). Raw stderr, URLs, headers and response
bodies are never exported. A generic curl message remains `connect_failed`;
missing detail is not replaced with an inferred cause.

Failures include `network.start.*` and `network.*` snapshots at request start and
completion: IPv4 default-route presence, interface operational state, and wireless
quality/signal when available. They use bounded local procfs/sysfs reads without
network probes or subprocesses. Missing or unreadable state is unknown, not proof
that a route/interface was down. IPv4 route presence is contextual evidence, not
a check of the selected route or IPv6 reachability. Snapshots cannot capture every
brief state change, and an interface reported up does not prove upstream access.

Diagnostic upload attempts report themselves into the same bounded queue and
appear in a subsequent scheduled batch. These events never trigger additional
uploads or change the heartbeat/backoff. Request metrics identify these as kind
`telemetry`, including successes so its success percentage has a denominator.
Device event/request counters are initialized
at zero so the first isolated failure can be measured against an earlier scrape.
Failures before that first scrape still require logs for an exact count.

Inspect failed HTTP events in Loki, expand their fields, and follow TraceID:

```logql
{service_name="res-ac"} | json | operation=~"http.request|http.device" | outcome!="ok"
```

Deploy the backend first so validation accepts these optional fields, then the
agent. Older agents remain compatible. Historical events cannot gain evidence
that was never captured. These changes improve device transport diagnostics;
VPS disk latency, filesystem stalls, and external network failures still need
host or network-side evidence for attribution.

## Deployment and access

See backend/observability/README.md for the optional Compose overlay, Grafana
access, provisioning, alerts, and checks. Metrics and OTLP listeners stay inside
the Docker network. Grafana has its own login and is served through the backend at `/grafana/`
over the existing public HTTPS listener. Its direct port binds to loopback. Device diagnostic uploads use the existing device token and TLS.
The stack does not require a public telemetry port or changes to browser auth.

## Validation

Run backend and agent tests and compile the firmware. Cover auth/validation,
retry identity, bounded diagnostic buffers, failed HTTP attempts, command context
persistence and redelivery, and telemetry sink outages. Validate Compose/configs
and, when Docker is available, run an ingestion smoke test. On hardware, verify
DHT11 disconnection, Bridge unavailability, Wi-Fi loss/recovery, AVR timeout/reset,
and command redelivery. Missing device reports should alert during an outage;
local failure details appear after reconnection if the bounded RAM queue survives.
