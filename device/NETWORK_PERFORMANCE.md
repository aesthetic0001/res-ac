# Software latency objective

The target is to make the avoidable software cost smaller than the measured
network contribution on the deployed device, under both ordinary operation and
command bursts. A percentage improvement alone does not establish that target.
Report individual command phases, warm/cold connections, failures, queue drops,
and tail latency. Required physical IR transmission/dwell and DHT sampling time
must remain visible separately; removing those requirements is not an optimization.

The previous deployed release is `38c125f`. Its matched acceptance run recorded
40 pings, five calibrations, and four level commands. Ping median/p95 was
537/866 ms; the complete 49-command run was 558/874 ms. The backend's active TCP
connections measured 47–69 ms smoothed RTT with observed minima of 44–45 ms.
One client TCP connection measured 47 ms to establish, corroborating that order.

The correlated 40 ping traces show remaining median software costs:

| Measurement | Median | p95 |
| --- | ---: | ---: |
| Agent command execution | 280 ms | 620 ms |
| Datastore put → GPIO assertion, included above | 60 ms | 160 ms |
| GPIO → consumed completion, included above | 210 ms | 520 ms |
| AVR command fetch, included in round trip | 31 ms | 49 ms |
| Actual AVR ping action, included in round trip | 0 ms | 1 ms |
| SQLite enqueue/claim/seen/result durations per command | 89 ms | 183 ms |
| Agent result HTTP duration minus libcurl duration | 34 ms | 301 ms |

These overlap and their percentiles cannot be added. Result POSTs took a median
98 ms inside curl including backend work. Long-poll waiting is not network RTT.
The reported end-to-end measure starts at backend enqueue and ends at the stored
result timestamp; browser/dashboard latency and the final response's return to
the device are outside that measure. Device telemetry uses a coarse monotonic
clock on the old board, so detailed captures must report clock resolution.

The next candidate adds the opt-in [native Bridge](NATIVE_BRIDGE.md). It replaces
Python UART/JSON processing on the command path with bounded native framing and
one atomic retained-publication exchange. Live acceptance and recovery evidence
are pending. Further work must follow measured residuals, including agent
serialization/scheduling and backend durable writes; network-bound completion
has not been demonstrated.

HTTP events carry bounded `http.agent.prepare_ms`, `http.agent.ipc_wait_ms`, and
`http.agent.consume_ms` fields. They separate constructor work, helper/selector
queueing outside libcurl time, and consumption after the IPC frame is read.
`command.final_journal_ms` measures the final durable journal write before result
submission; it is outside the HTTP request duration. These are compact attributes
on existing events. The local command interval includes delivery-body decoding.
Grouped SQLite work is measured by `db.transaction`, including durable commit;
inner SQL spans do not include that commit. Compare durable server completion as
well as the stored result timestamp, which precedes its commit.

Presence observations stay exact in the live backend API. Their disk copy is
coalesced to one write per five seconds of authenticated traffic and flushed on
clean shutdown; after a crash that noncritical timestamp can lag by up to five
seconds. Command enqueue/claim/result durability and fan-state freshness are
unchanged. Native HTTP completions retain their integer timing fields rather than
formatting them as curl output and parsing them back to floats.

The 100-ping run on the presence-coalescing backend and instrumented device
recorded 199/279 ms median/p95 to the stored completion timestamp, or
224.74/322.05 ms through the final server response (including commit). The
paired result curl duration minus server processing was 50.80/95.55 ms. This is
a transport estimate, not an exact packet measurement. Command handling,
including body decoding, measured 80/120 ms; earlier execution-only captures
excluded that decoding and are not directly comparable. Each of the two durable
backend transactions measured 21.69/60.07 ms. A separate syscall capture measured
22 WAL fsyncs at 23.87 ms median, 66.94 ms maximum. FULL durability is preserved.

Native-mode command polls request `commands?format=binary-v1`. The optional
`application/vnd.res-ac.command.v1` representation starts with `RAC1`, then a
bounded typed value: null/true/false (`n/t/f`), signed big-endian int32 (`i`),
finite big-endian float64 (`d`), UTF-8 string (`s` + u16 byte length), array
(`a` + u16 item count), or object (`o` + u16 pair count, string keys). Limits
are 32768 bytes, depth 16, and 1024 values including keys. Duplicate keys,
invalid UTF-8, truncation, unknown types and trailing data fail closed. This
only replaces response decoding; the existing command envelope and physical
safety validation still run. Legacy clients receive JSON. Older servers may
ignore the format request and return JSON, which the native client still
accepts. No HTTP helper protocol change, polling timer or new connection is
required. `command.timing.decode_ms` measures both decoding paths on the
existing command event. The board benchmark measured 33.4 ms per JSON decode
versus 6.0 ms per binary decode over 100 iterations. A subsequent live 100-ping
run recorded 169/268 ms to the stored timestamp and 206.67/309.00 ms through
durable server completion. Its paired transport estimate was 49.44/95.75 ms.
All pings succeeded on the first attempt. Five calibration and four level
commands also succeeded once each, rejected a stale session, reported state
and ended with the fan off. This remains above the network-bound target.

An on-board experiment found that replacing the vendor writer for whole bodies
regresses encoding time: a small result took 2.6 ms with the vendor writer versus
6.0 ms with the generic encoder; a 32-event batch took 115 versus 265 ms.
The next candidate retains the vendor writer for requests and journals and
measures fast scalar encoding only within cooperative telemetry. Native Bridge
clients also disable Nagle buffering, matching the server side. Live validation
of these changes is pending.
