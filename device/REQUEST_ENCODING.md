# Cooperative binary telemetry requests

High-rate ping bursts saturated the device CPU and overflowed the bounded
diagnostic queue. Encoding a representative 32-event batch on the actual
Python 2.7.3/MIPS CPU used 472/467 ms CPU with cooperative JSON and 302/301 ms
with the binary encoder, in alternating runs. Binary decoding on the backend
reproduced the original payload exactly, including its double-precision values.
These are encoder measurements; end-to-end improvement is still unproven.

The agent uses binary encoding for negotiated telemetry batches. Small reading,
result, and fan-state bodies keep the existing vendor JSON writer. Earlier
profiling measured that writer at about 2.5 ms per reply; the cooperative binary
reply path measured about 5.5 ms. The batch optimization does not justify changing
the small-body path.

## Compatibility and recovery

The backend continues accepting JSON. Authenticated device POST routes also
accept `application/vnd.res-ac.value.v1`, and advertise support with
`X-Resac-Request-Body: binary-v1`. Browser/admin bodies remain JSON.
`BINARY_REQUESTS=false` suppresses advertisement while accepting already queued
binary bodies, allowing a JSON control or a gradual downgrade.

The helper retains protocol 1 and sends valid completion frames with reserved
request ID zero to advertise features. Older agents already ignore unknown IDs.
The seven-byte capability body is `RACF`, version byte `1`, helper-support byte
`0|1`, and server-support byte `0|1|2`; `2` means no HTTP capability observation.
The helper sends one after initialization and before each ordinary completion.
It recognizes only the fixed backend response header value and never forwards
arbitrary headers, addresses, credentials, or raw error messages.

The new agent enables binary telemetry only when both peers advertise support.
Older helpers or servers keep JSON. A helper restart clears negotiated state.
If support disappears while a batch is prepared, the agent re-encodes its
original payload as JSON. An in-flight 415 retains the same batch ID on retry.
Result-upload retries retain the durable command journal and never dispatch
the physical action again. Agent setting `binary_requests: false` forces JSON.

Helper request opcode `B` has the same fields as `Q`, but accepts a binary POST
body, keeps its explicit length (including NUL bytes), and sets the binary media
type. `Q` keeps the original JSON semantics. Completion metadata, event-driven
socket handling, connection reuse, four request slots, and queue bounds remain
the same. Server spans/logs record bounded `http.request.body.format` values
(`json`, `binary-v1`, or `other`) to verify the actual path during trials.

## Value format and bounds

Bodies start with `RAC1`, using the existing command-response value tags:
`n/t/f` for null/booleans, `s` plus unsigned 16-bit byte count and strict UTF-8,
`i` plus signed 32-bit integer, `d` plus finite IEEE-754 double, and `a/o` plus
unsigned 16-bit element/property count followed by values or string-key/value
pairs. Integers, counts and doubles use network byte order. Negative zero is
preserved. Objects reject duplicate keys; decoding defines own data properties
so a key named `__proto__` cannot change a prototype.

Requests permit at most 32,768 bytes, depth 16 and 8,192 value/key nodes.
The larger node budget supports a full telemetry batch; the existing command
response budget remains 1,024. The cooperative encoder retains the 512-byte/
5-ms active-work budget and bounded 64-key cache. It adds no wake-up timer.
Authentication, payload validation, telemetry deduplication, WAL `FULL`
durability, command/result ordering, watchdogs and IR journals are unchanged.

## Trial gate

Compare repeated JSON/binary controls using the same agent/helper and backend
image, changing only advertisement. Use five bursts of 24 harmless pings per
mode, matching the load that exposed CPU saturation. Require at least 20% lower
median burst completion and p95 completion gap, no additional reading or
diagnostic drops in binary mode, and no more than 5% regression in ordinary
100-ping durable p95. Check restart/downgrade recovery and fresh reports before
merging or permanently deploying. Encoder speed alone cannot satisfy this gate
or establish that the system is network bound.


## Live trial outcome (2026-11-06)

Rejected as a latency optimization; do not permanently deploy this experiment.
The repeated comparison used the same RAM-only agent/helper and backend image,
changing only negotiated format advertisement. Each mode ran two sets of five
24-ping bursts (240 pings per mode), plus 100 ordinary pings. Every measured
command succeeded on its first attempt. Server logs verified the intended body
format and successful telemetry responses. No readings were dropped.

| Measure | JSON control | Binary trial |
| --- | ---: | ---: |
| Median 24-ping burst completion, 10 bursts | 4,616.5 ms | 4,472 ms |
| p95 gap between completions, 230 gaps | 243 ms | 221 ms |
| Diagnostic events dropped during measured bursts | 144 | 0 |
| Ordinary durable completion median, 100 pings | 183.87 ms | 180.72 ms |
| Ordinary durable completion p95, 100 pings | 240.16 ms | 241.91 ms |

Median burst completion improved 3.13% and p95 completion gap improved 9.05%,
below the required 20% each. Ordinary durable p95 increased 0.73%, within its
5% regression limit. The first burst pair's 21.5% p95-gap improvement did not
repeat; pooled results govern the decision. Burst statistics use server
completion timestamps. Ordinary durable figures use paired traces through the
end of the backend result response after transaction commit; neither is a
measurement of dashboard rendering or receipt of the result acknowledgment on
the device. Tail percentiles are calculated from samples, not added together.

The on-board encoder CPU improvement and elimination of diagnostic drops are
real, but they did not translate into a qualifying command-latency reduction.
Global CPU still reached saturation briefly during binary bursts. The final
binary repeat grew the already warm WAL by 8,240 bytes; the other measured
phases retained its allocation. This is not a cold-WAL comparison.

Paired ordinary traces put warm HTTP transport, excluding server processing,
at about 48–49 ms median per round trip in both modes. Combined SQLite
transaction time remained about 50–51 ms median per command; agent command
execution remained about 40 ms median, including the nested Bridge round trip
of about 20 ms. These measurements overlap and must not be summed as independent
phases. They do not establish that the application is network bound.

During rollback maintenance, isolated Python 2.7.3 measurements on the board
found network diagnostics at 2.45 ms per call (200 samples), the small vendor
JSON writer at 2.79 ms (200), and trace-parent creation at 0.69 ms (1,000).
The coarse live 10-ms prepare spans alone overstated the likely benefit of
moving any one of those operations into C.

Backend format switches and a downgrade to the accepted backend recovered fresh
reports and commands. The accepted backend image, installed device runtime,
and exact original AVR application were restored; trial files were removed
from device RAM. The latest command journal was retained, Wi-Fi recovery files
were preserved, and calibration recovery sent no physical IR. The draft is
closed without merging. The protocol and tests remain on the experiment branch
as evidence for future work.
