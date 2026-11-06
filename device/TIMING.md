# Command timing capture

Timing capture is opt-in and separate from telemetry. Normal firmware allocates
no trace buffer, the normal Bridge launcher is unchanged, and the agent does not
create a trace unless started with `--timing-trace`. No additional polling timer
or periodic datastore reads are introduced.

Use a controlled deployment of the matching agent/sketch and backend telemetry
support. Do not launch a second Bridge alongside the running service. Preserve
the current firmware, agent, and `run-bridge` launcher for restoration. The
diagnostic build is for temporary captures only. Start with ping-only commands;
an uncertain IR action must be physically checked before further fan tests.

## AVR capture

Build with `PLATFORMIO_BUILD_FLAGS=-DRESAC_COMMAND_TIMING=1 pio run`. This is a
diagnostic firmware image; rebuild without that flag before a normal release.
The build records sixteen fixed-size rows in SRAM with no dynamic allocation.
Serial output occurs only on an explicit dump, outside command execution.

1. After startup, send `timing arm` over USB serial. It resets both Linux RAM
   captures through an explicit diagnostic marker and arms the AVR buffer.
2. Submit one normal backend `device.ping` command and wait for its result.
3. Send `timing dump`. It freezes both Linux captures and prints the AVR rows.
   Export is once per process/output path; restart the diagnostic processes with
   new paths before another full capture.

The header identifies the FNV-1a 32-bit hash of the accepted command UUID, count,
and dropped trace rows. Rows are `T <event> <tag> <micros32>`. A hash is a capture
correlation aid, not a replacement for validating the original command UUID.

| Event | Meaning |
| --- | --- |
| 1 | GPIO ISR observed the low input |
| 2 | Main-loop command handler entered |
| 3 / 4 | Bridge API transaction began / returned |
| 5 / 6 | Action began / ended |
| 7 | Handler finished, after completion publication returned |

Transaction tags are 0 other, 1 command fetch, 2 safety snapshot, 3 retained
result, 4 retained diagnostic, and 5 Mailbox push. API durations include request
construction, UART transport, ACK waiting, and internal retries. They do not
separate individual retransmission attempts.

Compute AVR intervals as `(end - start) & 0xffffffff` to handle the approximately
71-minute `micros()` rollover. Resolution is 4 microseconds on this AVR. The ISR
timestamp marks interrupt service, not an electrical edge measured externally.
If the main loop catches a held input without observing an ISR, event 1 is absent;
do not invent an IRQ-to-handler duration. Overflow increments a trace-drop count
without changing command handling. Missing rows mean incomplete evidence.

## Linux Bridge capture

The diagnostic files are installed beside the agent under `/usr/lib/res-ac`.
For a coordinated capture, substitute this launcher for the existing `python
bridge.py` invocation in `run-bridge`, then let the normal AVR startup launch it:

```sh
python /usr/lib/res-ac/run_bridge_timing.py --output /tmp/bridge-timing.json
```

It uses the same vendor packet reader, handlers, CRC, UART settings, runners, and
socket code. Wrappers observe their boundaries without replacing packet bytes.
It does not preload the agent/system JSON module over the vendor's local JSON
module. The original launcher must be restored after the diagnostic run.

Capture events include first UART byte, parsed/CRC-validated packet, handler
return, UART ACK generation/flush, failed CRC, runner work, incoming JSON decode,
command storage, outgoing JSON encoding/queueing, and TCP buffer flush.

Send `SIGUSR1` to the diagnostic Bridge process after the probe completes. The
signal handler only freezes collection; export waits until a packet boundary.
Export also happens on a normal Bridge exit. Nothing is written to UART by the
tracer. Do not trigger a dump in the middle of a latency test: serialization is
outside capture but can still delay other work while the file is written.

The capture defaults to a rolling 1,024-record buffer; `--limit` accepts 16–4,096.
In-flight completion tracking is also bounded. All overwritten/omitted records
are reported as trace drops, not sensor/telemetry drops. TCP flush distinguishes
put confirmation (tag 1) from completion (tag 2), handles partial writes, and
does not report a closed client as a successful flush.

`bridge.socket_flushed` is recorded after the server flush pass observes all
bytes accepted by the socket. It is an upper bound on that pass's last `send`,
not proof of agent receipt. Flush-pass timings can include socket receive work.
UART packet tags are `16 + key` for gets, `32 + key` for puts, and `48 + key`
for Mailbox messages. Key codes are 0 other, 1 command, 2 safety snapshot,
3 result, 4 diagnostic, 5 invalidation, 6 heartbeat, 7 session, 8 interval,
and 9 sample. No actual payloads or session values are retained.

## Agent capture and clock alignment

During that controlled run, add this option to the agent's existing invocation:

```sh
python -u /usr/lib/res-ac/res_ac_agent.py --config /etc/res-ac/agent.json \
  --timing-trace /tmp/agent-timing.json
```

Send `SIGUSR1` to that agent after the command result is accepted. Its main loop
defers export while a manual or automatic AVR command is active. Clean shutdown
also exports a frozen capture. Captures use new files under `/tmp`, mode 600;
existing paths and symlinks are refused. A repeated dump signal is harmless.

Agent events bracket command queueing, exact datastore confirmation, the GPIO
write, socket `recv`, JSON decode, completion arrival, and validated consumption.
Only fixed event names, numeric metadata, and correlation UUIDs are captured.
Credentials, URLs, request bodies, readings, and fan session values are excluded.

Both Linux captures record absolute monotonic microseconds. Python 2 uses
`clock_gettime(CLOCK_MONOTONIC)` through ctypes when available; a coarse elapsed-
tick fallback explicitly reports its 10,000-microsecond resolution. Align Linux
processes only when both report the monotonic clock family, or both use the same
elapsed-tick fallback. Never subtract an AVR timestamp from a Linux timestamp.

The resulting breakdown is:

- AVR ISR → handler: time the AVR spent reaching the handler after IRQ service.
- AVR transaction begin → end: complete synchronous exchange cost.
- Linux first byte → parsed → handler → ACK flush: receiver/parser/handler/response
  portions of that exchange; repeated ACK indexes without handler execution can
  help identify retransmissions in the known vendor protocol.
- Completion queued → socket flushed: Linux Bridge output queue delay.
- Socket flushed → agent `recv`: delivery and agent scheduling delay.
- Agent completion received → consumed: JSON/application processing delay.

Tracing itself adds clock and wrapper overhead. Compare a short ping-only run
with capture disabled before treating a traced duration as normal production
performance. A truncated capture or mixed clock families cannot explain the
entire residual reliably.
