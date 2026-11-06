# Native UART Bridge

The native service handles the sensor sketch's UART protocol and retained
in-memory datastore. Its local binary stream removes Python packet parsing and
JSON work from command dispatch and completion. It runs as the exclusive owner
of the existing UART, launched by `Bridge.begin()` through `/usr/bin/run-bridge`.
It requires no added GPIO, periodic polling, or MQTT broker.

## Build and installation

Build the static big-endian MIPS helper with the verified toolchain cache:

```sh
python3 scripts/build_native_bridge.py --cache-dir private/http-build
```

The installer opts in with
`--native-bridge-helper private/http-build/res-ac-bridge-mips`. It checks the
static ELF architecture and protocol version before stopping the agent, saves
`run-bridge.res-ac-original`, installs the small compressed helper and binary
protocol module, and sets `native_bridge: true` in the private agent config.
An existing native config requires a supplied helper; it cannot silently switch
transports. Normal installation without that option restores the saved launcher.
Restart the AVR after switching transport so it launches the matching service.

The launcher expands the helper into RAM atomically and executes it. UART stdout
contains protocol frames only; service errors go to `/tmp/res-ac-bridge.log`.
A deployment must preserve the current firmware, runtime files, private config,
and launcher as one rollback unit. The full installer also manages network files;
a scoped trial must preserve independently updated live Wi-Fi recovery code.

## UART and recovery

Frames keep the Yun layout: `ff, index, length-be16, payload, crc-be16`, using
the reflected CCITT CRC with initial value `ffff`. Index rollover and the exact
previous-index response cache suppress duplicate execution. CRC failures,
unexpected indexes, and partial-frame expiration produce no datastore change.
A partial frame retains the existing 50 ms byte deadline. Idle waits use socket
and UART readiness, without a periodic timer.

The service supports reset `XX100`, shutdown `XXXXX`, datastore `d` / `D`, and
legacy `M` publications. Its capability is `resac_native_bridge=v1`. The sketch
checks this capability at startup. `B<key>fe<value>` atomically retains and
publishes a value, replying with byte `01`; this replaces separate `D` and `M`
exchanges. A failed capability/publication falls back to the legacy publication
path. Existing event firmware can use its original get/put/Mailbox frames with
the native service; older agents require the restored Python launcher.

The agent still waits for the matching datastore-put confirmation before GPIO
assertion. Reset/reconnect cancels pending notifications and rotates the fan
session. Heartbeat freshness, calibration, physical dwell, and durable journals
remain enforced by the same agent/sketch code. Switching services resets the
in-memory store and AVR calibration. Uncertain IR actions are never replayed.

## Local stream

The service binds only `127.0.0.1:5701`. Each frame starts with its payload length
as an unsigned big-endian 32-bit integer. Entries are key length u8, value length
u16 big-endian, key bytes, then UTF-8 value bytes.

| Direction | Opcode | Payload after opcode |
| --- | --- | --- |
| Agent → service | `s` | Empty; request the retained snapshot |
| Agent → service | `p` | One entry; retain a value |
| Service → agent | `S` | All retained entries |
| Service → agents | `P` | Exact entry confirming a completed put |
| Service → agents | `V` | One published entry |

Limits are 32 retained keys, 64 bytes per key, 512 bytes per value, 8 clients,
32 KiB per-client input/output queues, and a 512-byte UART payload. Slow or
malformed clients are disconnected independently. The parser validates length
and field bounds before allocation/copy. Neither sockets nor UART writes block;
UART/ACK readiness takes priority. The service exposes no process, file, or IR
execution operation. Shutdown restores UART termios and file status flags.

Native builds can be tested on a real subprocess with pipes and local sockets:

```sh
RESAC_HTTP_HELPER=private/http-build/res-ac-http-native \
  python3 -m unittest discover -s device/test
```

Tests cover protocol bytes and CRCs, atomic retention/publication, duplicate and
wrong indexes, rollover, partial frames, concurrent socket input, snapshot
recovery, malformed limits, native agent GPIO confirmation, and the actual sketch.
The C service also runs under address/undefined-behavior sanitizers. Live latency
and fault/recovery acceptance are required before enabling a permanent rollout.
