# Seeeduino Cloud telemetry and fan control

[Woozoo IR setup](IR.md) covers digital pin 9, emitter wiring, named fan buttons,
raw NEC commands, and local serial testing. The DHT11 stays on digital pin 3.

For Linux OS upgrades, see the [firmware compatibility assessment](../FIRMWARE.md).

The AVR sketch in `src/main.cpp` reads a **DHT11 on digital pin 3**. It uses the
internal Arduino Bridge to publish a single sample containing temperature,
humidity, a sequence number, and a Linux-provided boot/session epoch. Invalid
sensor reads are reported as errors rather than uploaded as zero or stale data.

`res_ac_agent.py` runs on the Linux processor. A small event loop receives pushed
Bridge Mailbox events while one persistent libcurl helper handles HTTPS asynchronously. This works with
the board's Python 2.7 without installing threading. GPIO ownership also requires
the standard `fcntl.flock` module, checked by the installer before replacement.
Both legacy JSON APIs used by Yun firmware are supported.

The agent and AVR firmware must be updated together: the new agent requires the
`resac_events=v1` capability and waits with reconnect backoff if it is absent.
When staging a later deployment, use the installer's `--no-start` option and
start the agent only after the matching reporting firmware is installed.

## Configure and install

The backend must already be reachable via trusted HTTPS. From the project root:

```sh
~/.platformio/penv/bin/python scripts/configure_device.py
python3 scripts/build_http_transport.py
~/.platformio/penv/bin/python scripts/install_device.py
~/.platformio/penv/bin/pio run -t upload
```

Close other serial monitors before installation or flashing. The configuration
wizard prompts for the actual backend HTTPS URL (including a non-default port),
device ID, and device token. It writes `private/device.json` with mode 600; the
entire `private/` directory is ignored by Git. Nothing deployment-specific belongs
in the firmware or public source. Never paste the token into public issues/logs.

The installer requires the recovery serial-terminal firmware to still be running.
It transfers files with per-block acknowledgments/checksums, fetches public
Let's Encrypt ISRG trust roots over the host's verified HTTPS connection, aligns
the Linux clock, and tests an authenticated POST with an intentionally invalid
empty reading. A 400 validation response confirms HTTPS and authentication
without inserting a fake sample. Failure stops installation before any AVR flash.
Certificate validation is never disabled. The private config remains staged
until preflight passes. The installer itself does not flash firmware.

On success, the agent is installed at `/usr/lib/res-ac/res_ac_agent.py`, its
private config at `/etc/res-ac/agent.json`, and the CA bundle at
`/etc/res-ac/ca-bundle.pem`. `/etc/init.d/res-ac-agent` starts it at Linux boot.
The installer normally starts the service; `--no-start` defers that. It waits for
the reporting firmware to initialize Bridge before collecting real samples.

The host installer needs PySerial (normally included in PlatformIO's Python
venv). The board needs its existing Bridge, Python, and gzip components.
`build_http_transport.py` uses Python 3.12+, downloads checksum-pinned MIPS
libcurl development archives from [stunnel/static-curl](https://github.com/stunnel/static-curl/releases/tag/8.23.0-1)
and a pinned [MIPS compiler](https://github.com/userdocs/qbt-musl-cross-make/releases/tag/2644),
and builds a static MIPS32 soft-float helper under `private/http-build/` with a
source/build checksum manifest. It only builds locally. The installer accepts
`--http-helper PATH` for a different build location and validates the ELF target.
The helper supplies modern certificate/IP-SAN verification independently of the
board's legacy PolarSSL curl.

The installer stages the new agent and helper in RAM and preflights that pair
before stopping the existing agent or replacing its transport. The helper reuses
`/usr/lib/res-ac/curl.gz`, the previous compressed TLS client's flash slot; it is
expanded as `/tmp/res-ac-http` at startup. The static helper is approximately
8.2 MiB, compressed to 3.6 MiB. The system curl is preserved. Serial transfer takes
several minutes. `--modern-curl` remains accepted for command-line compatibility;
the persistent helper is now always installed.

The installer also adds a small `/etc/udhcpc.user` hook, preserving other hooks,
to work around legacy firmware that loses its IPv4 default route on DHCP renewal.
`dhcp_route_repair.py` uses the current DHCP gateway and interface metric, and
honors `defaultroute=0`. It adds only a missing route, using netifd's static-route
protocol so normal lease cleanup can remove it. It never embeds or pins a campus
gateway, replaces another route, or resets Wi-Fi to recover from this bug.

An independent `/etc/init.d/res-ac-network` watchdog checks the local eduroam
uplink every 30 seconds. If it stays without an IPv4 lease/default route for
two minutes, it restarts the configured Wi-Fi radio. It first repairs a missing
route from netifd's current lease when possible. Repeated radio recovery attempts
wait 5, 10, 20, then 30 minutes; five minutes of a healthy uplink resets that
backoff. Short outages and backend/HTTPS failures alone do not restart Wi-Fi.
The watchdog respects disabled eduroam/radio settings, `ifdown wwan`, and
`auto=0`/`defaultroute=0`. It changes no credentials or persistent network settings
and never reboots the board. The radio restart affects every interface on that
radio. State changes/recovery attempts are logged with the `res-ac-network` tag.

## Reporting and retries

The default reporting interval is 10 seconds and can be changed in the private
configuration (`interval_seconds`, from 2 to 3600). The agent passes that interval
to the AVR through Bridge. A `millis()` timer keeps the AVR loop available for
command handling. No network calls run on the AVR.

Each sample has a stable `readingId` composed of its Bridge epoch and sequence.
Upload retries reuse that ID so the backend can deduplicate a request whose
response was lost. Captured UTC timestamps are included when the Linux clock is
valid. The queue is bounded to 100 samples by default: during long outages,
oldest samples are discarded and a counter is logged. The queue is RAM-only,
which avoids writes to the board's small flash but means buffered samples are
lost when the agent or Linux restarts. A retained latest Bridge frame keeps its
original ID and is safe to retry after an agent restart.

### Event-driven Bridge collection

The AVR retains its latest values with `Bridge.put()` and sends
`Mailbox.writeMessage()` notifications as `v1,<key>,<value>`. Notifications carry
sample frames, sensor status/diagnostics, fan-state changes, and command
acknowledgments/diagnostics. Fan state is published on changes; an unchanged
command acknowledgment is not rebroadcast on every AVR mailbox check.

Python maintains one nonblocking TCP connection to the existing local Bridge
service at `127.0.0.1:5700`. It requests one datastore snapshot after connecting,
then waits with `select()` for pushed changes. Collector, automation, command
handling, and fan reporting share the same cache; their reads make no repeated
TCP requests. Events bypass the former half-second collection/acknowledgment
waits and one-second fan-state/automation waits. Timers still service
upload retries, safety deadlines, fan heartbeat writes, and periodic
backend state refreshes. AVR sampling remains timed. The AVR checks the command
slot every 20 ms, independently of 250 ms configuration/watchdog housekeeping.
New control commands refresh pending invalidation and heartbeat/session state
before execution. Bridge round trips and physical IR dwell still contribute to
end-to-end latency; the 20 ms value is the scheduled check interval.

Bridge broadcasts both messages and request replies to all clients. The receiver
separates those envelopes and handles fragmented/concatenated JSON, including the
firmware's legacy JSON implementations. It ignores unrelated replies after the
initial snapshot. Incoming sample events are bounded to 128 entries before they
move into the normal upload buffer; overflow contributes to dropped-reading
counts. Each queued sample retains its matching sensor diagnostic.

Disconnects invalidate cached control state and discard unsent writes so an
uncertain physical command is never replayed by the transport. Already received
samples remain available for upload. Reconnection fetches a new snapshot and
recovers the latest retained reading; events missed while disconnected are not
replayed. Automatic control requires a live sample event after reconnect, since
the retained snapshot has no trustworthy sample age. AVR boot notifications also
invalidate the cache and trigger snapshot/epoch initialization.

The POST route is `/api/devices/<device-id>/readings` with a device bearer token.
The URL/token and request bodies reach the helper through private nonblocking IPC,
without per-request files or credentials in command-line arguments. Logs exclude tokens,
server URLs, and response bodies; `/tmp/res-ac-agent.log` is bounded in size.
The AVR USB serial monitor shows sensor values and the latest uploader and
command status.

### Persistent HTTP transport

One helper process owns a libcurl multi handle with a four-connection pool shared
by readings, command polls/results, fan state, and telemetry. Idle connections and
TLS sessions can be reused across requests. An open command long poll leaves
capacity for the other request types. The backend keeps idle connections for
60 seconds so they can survive the normal reporting intervals. Restarted or stale
server connections are replaced by libcurl under the existing request timeout.

`select()` waits for Bridge data, helper completion messages, writable IPC queues,
or the nearest real heartbeat/retry/safety deadline. There is no fixed 100 ms
subprocess-completion timer. Completions set request state directly; Python does
not periodically inspect curl process exit status. Commands run before reading
handoff, and a completed reading frees its slot and starts the next buffered
reading before background reporting. No batching delay is added.

Requests and responses remain limited to 32 KiB, with four requests in flight and
bounded IPC buffers. The binary IPC protocol prefixes each message with a
network-order 32-bit length. Initialization sends the fixed API base, CA path,
and token; requests send an ID, timeout, method, route, traceparent, and JSON body.
Completions contain the ID, curl/HTTP status, connection count, five timing
milestones, a safe error category, and response body. Raw libcurl error strings
never leave the helper. Redirects are not followed and TLS verification is enabled.

Helper EOF/crash fails unresolved attempts and wakes Python immediately. The next
ordinary retry starts a fresh helper and pool. The helper never automatically
replays application requests after a process restart; reading/batch IDs and the
existing command journal retain retry identity. It is killed/reaped during agent
shutdown. Standalone `--probe` also uses the helper when `http_helper` is configured;
legacy standalone probes retain their CLI adapter.

## Ping commands

In the backend dashboard, queue command type `device.ping` with payload `{}`.
Linux maintains one 30-second long poll on `GET /api/devices/<device-id>/commands`
with a 35-second curl timeout. Polling runs alongside sensor uploads. On delivery,
it writes `v1,<command-UUID>,ping` to the Bridge `resac_command` key. After Bridge
confirms that exact write, Linux asserts the existing GPIO21 → AVR PB2 connection.
The AVR handles the notification and returns `v1,<command-UUID>,pong,<uptime-ms>` in
`resac_command_result`. Linux requires a matching UUID and Arduino response;
it does not answer ping on the Arduino's behalf.
The Seeeduino Cloud GPIO sketch makes no periodic command reads. It keeps
250 ms configuration/watchdog housekeeping and the existing sensor schedule.
See [GPIO command notifications](GPIO.md) for wiring, ownership, recovery,
the Bridge socket-readiness update, and the outstanding hardware validation.
Polling starts only after the Arduino advertises its handler through Bridge.
The target-level sketch advertises `v4`; command-frame parsing also understands
the older `v1`/`v2` ping and `v3` calibrated-IR formats. Use an event-capable sketch
with this agent. IR requires a current session and calibrated estimate.

The agent posts the result to
`POST /api/devices/<device-id>/commands/<command-UUID>/result`:

```json
{"status":"succeeded","result":{"reply":"pong","uptimeMs":12345}}
```

Missing Arduino responses fail after 10 seconds. Unsupported command types
report `failed`; `fan.command`, `ir.send`, and `fan.calibrate` require `v3` or
`v4`, while `fan.level` and `fan.curve` require `v4`.
Network failures use bounded
exponential retries. The agent waits for result acceptance before polling the
next command. Expired or removed commands (HTTP 409/404) are abandoned.

The AVR caches its last command reply, and the agent saves its latest completed
result atomically in `/etc/res-ac/command-result.json` (mode 600). A lost result
acknowledgment or agent restart retries that same result without repeating the
Arduino request. Ping itself is harmless to repeat if power is lost before the
result is saved. For IR, the agent also saves an uncertain failure before dispatch;
after interruption it reports that uncertainty instead of automatically repeating
the action. See [IR delivery behavior](IR.md#delivery-behavior). Commands write
this small journal to flash; empty polls and sensor samples do not.

To upgrade an existing device through USB, upload the recovery project first,
run the installer above (the existing TLS binary is reused), then upload the
main project again. Both the Linux agent and AVR sketch need this upgrade.

## Diagnosis and recovery

In the Linux console (via SSH, or after restoring the recovery sketch):

```sh
python /usr/lib/res-ac/res_ac_agent.py --probe
/etc/init.d/res-ac-agent restart
tail -n 20 /tmp/res-ac-agent.log
python /usr/lib/res-ac/network_watchdog.py --check
logread | grep res-ac-network
```

The watchdog is enabled at boot by the installer. Stop it with
`/etc/init.d/res-ac-network stop`; run `/etc/init.d/res-ac-network disable` to
keep it off after a reboot.

Use `--probe` only to verify connectivity/authentication; it inserts no test
reading. Do not run a second full agent alongside the startup service. Use the
[serial-terminal recovery project](../recovery/serial-terminal/README.md) to
restore USB access to Linux. Recovery flashing preserves all Linux/Wi-Fi files.

Host-side verification:

```sh
python3 scripts/build_http_transport.py --target native
RESAC_HTTP_HELPER="$PWD/private/http-build/res-ac-http-native" python3 -m unittest discover -s device/test -v
~/.platformio/penv/bin/pio run
~/.platformio/penv/bin/pio run -d recovery/serial-terminal
```

Tests cover sensor frame validation, legacy JSON variants, fragmented Bridge
responses/events, shared-connection snapshot recovery, event ordering, immediate
sample/state/acknowledgment handling, epoch initialization, bounded buffering, in-flight sampling, retry
IDs, HTTPS trust verification, and private request-file cleanup. Ping tests cover
the HTTPS poll/result exchange with a simulated Arduino, stale acknowledgments,
execution timeout, unsupported commands, retry identity, and restart recovery.
IR tests cover button codes, payload validation, expiry, matching AVR responses,
and preventing replay after interrupted transmission or AVR reset.
Curve tests cover all 25 direct speed routes, clamped limits, off-state speed
blocking, remembered speed across power toggles, threshold/hysteresis behavior, fresh
sensor requirements, adjustment dwell, manual override, settings persistence,
and sharing the command mailbox. Native estimate tests require a host C++ compiler.
Native sketch tests also run the actual firmware with a virtual clock and
Bridge/IR doubles, checking independent command cadence, safety refreshes,
duplicate suppression, timer rollover, and housekeeping after IR dwell.
Transport integration tests use a local HTTPS server to verify process/connection
reuse, independent long polls, completion events, helper crash recovery,
cancellation, timeouts, bounded responses, and certificate rejection. They require
the native helper and are explicitly skipped if it is not available; building it
requires a Linux C compiler and shared libcurl 7.85+.

## Observability

The firmware publishes versioned `resac_sensor_diagnostic` and
`resac_command_diagnostic` Bridge frames with elapsed milliseconds and outcomes.
The agent forwards bounded diagnostics using the existing HTTPS device token,
including network failures, sample drops, CPU/RAM/storage, and command/read
correlation IDs. Install the updated agent and flash the updated sketch for
complete instrumentation. This agent requires the sketch's `resac_events=v1`
capability; earlier deployments can continue using their matched older agent/sketch.

Diagnostics use RAM-only queues (128 waiting events plus 32 in flight), send
every 15 seconds while healthy, and back off up to 60 seconds after errors.
They do not repeat hardware actions and do not write to flash. Linux memory/CPU
metrics describe the Linux processor; AVR timings describe hardware operations.
See the [design](../observability.md) and
[Grafana deployment guide](../backend/observability/README.md).
