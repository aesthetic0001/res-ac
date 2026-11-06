# Software latency objective

The target is to make avoidable software cost smaller than the measured network
contribution on the deployed device, under ordinary operation and command bursts.
Percentage improvement alone does not establish that target. Physical IR dwell
and DHT sampling must remain visible separately. Network-bound completion has
not yet been demonstrated; burst profiling and further residual work remain.

## Production trial, 2026-11-06

The previous release is `38c125f`. The accepted candidate runtime is `86e27a9`
with backend code from `5def7b8`. The following independent runs use real
production commands and sensor uploads. Each command succeeded on its first
attempt. The fan runs contain five calibrations and levels 1, 2, 1, off.

| Stored completion time | Commands | Median | p95 |
| --- | ---: | ---: | ---: |
| Previous release, sequential pings | 100 | 543.5 ms | 856 ms |
| Native Bridge + cooperative telemetry | 100 | 246.5 ms | 327 ms |
| Grouped durable transactions | 100 | 198.5 ms | 276 ms |
| Binary command responses | 100 | 169 ms | 268 ms |
| Bounded telemetry + immediate HTTP submission | 100 | 154.5 ms | 220 ms |
| Previous release, calibration/levels | 9 | 747 ms | 1096 ms |
| Final candidate, calibration/levels | 9 | 243 ms | 431 ms |

Final ping p95 is 74.3% lower than the matched previous-release run, exceeding
the deployment's 20% acceptance threshold. This is an intermediate improvement,
not proof that the software is network-bound. Network jitter and storage latency
vary between runs, so intermediate differences cannot all be attributed to code.

For the final 100 pings, completion through the final durable backend commit
measured **176.68 ms median / 246.11 ms p95**. The paired result curl duration
minus server processing was **48.47 / 78.86 ms**. This is a transport estimate,
not an exact packet measurement. TCP's observed minimum RTT was about 44–47 ms;
reused connections avoid repeated DNS, TCP and TLS setup.

| Final candidate phase | Median | p95 |
| --- | ---: | ---: |
| Device command handling, including body decoding | 40 ms | 60 ms |
| Datastore put to GPIO assertion, included above | 10 ms | 20 ms |
| GPIO to consumed completion, included above | 20 ms | 30 ms |
| AVR fetch, included in round trip | 3 ms | 4 ms |
| Actual AVR ping action, included in round trip | 0 ms | 1 ms |
| Each backend durable transaction, two per command | 22.09 ms | 58.93 ms |
| Final device journal | 10 ms | 20 ms |
| HTTP agent preparation | 10 ms | 10 ms |
| HTTP helper/selector work outside curl | 0.60 ms | 9.85 ms |

These phases overlap; their medians and percentiles must not be summed. The
old board's Python monotonic fallback has 10 ms resolution: a reported zero
is not proof of zero work. A separate on-board 100-iteration benchmark measured
JSON command decoding at 32–33.4 ms and binary decoding at 4.3–6.0 ms.

A live idle AVR reset, without rewriting application flash, produced a fresh
sensor epoch and rotated fan session, invalidated calibration and recovered
production uploads. Six post-reset pings and the final nine fan commands
passed. Stale-session commands were rejected without enqueueing; the fan ended
off. The unrelated deployed Wi-Fi files and private configuration were preserved.
Earlier whole-runtime rollback and native restart paths were also exercised.

## Implementation and durability

The opt-in [native Bridge](NATIVE_BRIDGE.md) replaces Python UART/JSON handling
with bounded native framing and atomic retained publication. Existing CRC,
duplicate suppression, exact put-confirmation-before-GPIO, session checks,
heartbeat, physical dwell and pre-IR journaling remain enforced.

Telemetry uses direct scalar encoding, caches at most 64 short keys per batch,
and yields after a five-millisecond work budget or 512 bytes. This is active-work
budgeting, not a new wake-up timer. A scalar may finish before yielding, and the
byte limit still applies after a backwards wall-clock adjustment. The board's
paired benchmark measured 306 ms for a 32-event cooperative batch with vendor
scalars versus 263 ms with the candidate, and maximum observed steps of 30 versus
10 ms at its coarse clock resolution. Whole request and journal bodies retain
the vendor writer, which proved faster than a generic replacement.

HTTP submission attempts one nonblocking helper write immediately so curl can
start before background work. Short writes and backpressure retain the remainder
for the readiness selector. Fatal helper errors complete locally without
replaying queued work. Libcurl retains its existing process and connection pool.

SQLite groups enqueue/claim and presence/write operations into durable
transactions. Device delivery occurs only after successful COMMIT. `db.transaction`
includes fsync; inner SQL spans exclude commit. WAL `synchronous=FULL` remains
enabled. A direct trace of 22 production WAL fsyncs measured 23.87 ms median and
66.94 ms maximum, so storage remains a significant residual cost.

Presence stays exact in the live API. Its disk copy coalesces to one update per
five seconds of authenticated traffic and flushes on clean shutdown; a crash can
leave that noncritical timestamp up to five seconds behind. Command/result
persistence and fan-state freshness retain their durability guarantees.

## Command response format

Native-mode polls request `commands?format=binary-v1`. The optional content type
is `application/vnd.res-ac.command.v1`; its body starts with `RAC1`, followed by
a bounded typed value: null/true/false (`n/t/f`), signed big-endian int32 (`i`),
finite big-endian float64 (`d`), UTF-8 string (`s` + u16 byte length), array
(`a` + u16 item count), or object (`o` + u16 pair count, string keys). Limits are
32768 bytes, depth 16, and 1024 values including keys. Duplicate keys, invalid
UTF-8, truncation, unknown types and trailing data fail closed.

This only replaces response decoding. Existing envelope and physical safety
validation still run. Legacy clients receive JSON; an older server can ignore
the format request and return JSON, which the native client accepts. The HTTP
helper protocol and connections are unchanged. Shared Node/Python vectors cover
all six command types, booleans, integers, floats and Unicode.

## Measurement and validation

Stored completion starts at backend enqueue and ends at the stored result
timestamp, which precedes the final COMMIT. Durable completion instead uses the
result-upload server span end. Neither includes dashboard rendering or the final
response's return to the device. Long-poll waiting is not network RTT. Device
span placement is approximate; cross-host absolute timestamps are not used to
subtract device phases.

Compact attributes on existing events report `command.timing.decode_ms`,
`command.final_journal_ms`, `http.agent.prepare_ms`, `http.agent.ipc_wait_ms`, and
`http.agent.consume_ms`. Backend expansion adds phase spans without increasing
the device event count. Native HTTP timing fields remain binary rather than
being formatted to text and reparsed.

Validation: 188 device tests, 54 backend tests, native Bridge address/undefined
behavior sanitizer tests, actual-sketch tests, and live command/upload/recovery
checks. The AVR build uses 1391 bytes static SRAM and 22204 bytes flash. Native
Bridge is a 66 KB static big-endian MIPS executable. No storage-sync substitution
or weaker synchronization mode is included in this release.
