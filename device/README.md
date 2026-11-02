# Seeeduino Cloud telemetry and fan control

[Woozoo IR setup](IR.md) covers digital pin 9, emitter wiring, named fan buttons,
raw NEC commands, and local serial testing. The DHT11 stays on digital pin 3.

For Linux OS upgrades, see the [firmware compatibility assessment](../FIRMWARE.md).

The AVR sketch in `src/main.cpp` reads a **DHT11 on digital pin 3**. It uses the
internal Arduino Bridge to publish a single sample containing temperature,
humidity, a sequence number, and a Linux-provided boot/session epoch. Invalid
sensor reads are reported as errors rather than uploaded as zero or stale data.

`res_ac_agent.py` runs on the Linux processor. A small event loop collects Bridge
samples while a curl subprocess handles HTTPS asynchronously. This works with
the board's minimal Python 2.7 without installing threading or other packages.
Both legacy JSON APIs used by Yun firmware are supported.

## Configure and install

The backend must already be reachable via trusted HTTPS. From the project root:

```sh
~/.platformio/penv/bin/python scripts/configure_device.py
~/.platformio/penv/bin/python scripts/install_device.py --modern-curl
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
venv). The board needs its existing Bridge, Python, curl, and gzip components.
`--modern-curl` installs a separate static MIPS curl from
[stunnel/static-curl](https://github.com/stunnel/static-curl/releases/tag/8.23.0-1),
downloaded over verified HTTPS and checked against a pinned SHA-256. This handles
IP-address certificates that the original PolarSSL client rejects. The system
curl is preserved; a compressed copy uses about 4.2 MiB of flash and unpacks to
about 9.9 MiB in RAM at service startup. Serial transfer takes several minutes.
Subsequent installs reuse an identical compressed copy. Omit this flag on firmware
whose system curl already validates the backend certificate correctly.

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

The POST route is `/api/devices/<device-id>/readings` with a device bearer token.
Curl reads the URL/token from a private temporary config rather than command-line
arguments. Temporary request files are removed afterward. Logs exclude tokens,
server URLs, and response bodies; `/tmp/res-ac-agent.log` is bounded in size.
The AVR USB serial monitor shows sensor values and the latest uploader and
command status.

## Ping commands

In the backend dashboard, queue command type `device.ping` with payload `{}`.
Linux maintains one 30-second long poll on `GET /api/devices/<device-id>/commands`
with a 35-second curl timeout. Polling runs alongside sensor uploads. On delivery,
it writes `v1,<command-UUID>,ping` to the Bridge `resac_command` key. The AVR
checks this key every 250 ms and returns `v1,<command-UUID>,pong,<uptime-ms>` in
`resac_command_result`. Linux requires a matching UUID and Arduino response;
it does not answer ping on the Arduino's behalf.
Polling starts only after the Arduino advertises its handler through Bridge.
The cyclic-level sketch advertises `v4`; `v3` supports calibrated raw IR,
while old `v1`/`v2` sketches support ping only
with the updated agent. IR requires a current session and calibrated estimate.

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
python3 -m unittest discover -s device/test -v
~/.platformio/penv/bin/pio run
~/.platformio/penv/bin/pio run -d recovery/serial-terminal
```

Tests cover sensor frame validation, legacy JSON variants, fragmented Bridge
responses, epoch initialization, bounded buffering, in-flight sampling, retry
IDs, HTTPS trust verification, and private request-file cleanup. Ping tests cover
the HTTPS poll/result exchange with a simulated Arduino, stale acknowledgments,
execution timeout, unsupported commands, retry identity, and restart recovery.
IR tests cover button codes, payload validation, expiry, matching AVR responses,
and preventing replay after interrupted transmission or AVR reset.
Curve tests cover all 25 cyclic routes, threshold/hysteresis behavior, fresh
sensor requirements, adjustment dwell, manual override, settings persistence,
and sharing the command mailbox. Native estimate tests require a host C++ compiler.

## Observability

The firmware publishes versioned `resac_sensor_diagnostic` and
`resac_command_diagnostic` Bridge frames with elapsed milliseconds and outcomes.
The agent forwards bounded diagnostics using the existing HTTPS device token,
including network failures, sample drops, CPU/RAM/storage, and command/read
correlation IDs. Install the updated agent and flash the updated sketch for
complete instrumentation. Older firmware can still upload samples and execute
commands but cannot report internal AVR timings.

Diagnostics use RAM-only queues (128 waiting events plus 32 in flight), send
every 15 seconds while healthy, and back off up to 60 seconds after errors.
They do not repeat hardware actions and do not write to flash. Linux memory/CPU
metrics describe the Linux processor; AVR timings describe hardware operations.
See the [design](../observability.md) and
[Grafana deployment guide](../backend/observability/README.md).
