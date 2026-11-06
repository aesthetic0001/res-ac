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
