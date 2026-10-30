# Seeeduino Cloud temperature and humidity reporting

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

## Reporting and retries

The default reporting interval is 10 seconds and can be changed in the private
configuration (`interval_seconds`, from 2 to 3600). The agent passes that interval
to the AVR through Bridge. A `millis()` timer keeps the AVR loop available for
future command handling. No network calls run on the AVR.

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
The AVR USB serial monitor shows sensor values and the latest uploader status.

Commands/IR execution are not implemented in this telemetry agent yet. Their
polling mechanism can be added independently without blocking the poster.

## Diagnosis and recovery

In the Linux console (via SSH, or after restoring the recovery sketch):

```sh
python /usr/lib/res-ac/res_ac_agent.py --probe
/etc/init.d/res-ac-agent restart
tail -n 20 /tmp/res-ac-agent.log
```

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
IDs, HTTPS trust verification, and private request-file cleanup.
