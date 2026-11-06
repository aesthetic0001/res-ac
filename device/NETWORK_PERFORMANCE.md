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
