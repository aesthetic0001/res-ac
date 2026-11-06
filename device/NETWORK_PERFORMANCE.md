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
