# Event-loop durable write batching experiment

The accepted backend spends roughly 22–24 ms in each WAL fsync on the current
host. Small SQL changes cannot remove that wait. Concurrent request mutations
can instead share a durable transaction when their bodies are already ready.

`WriteQueue` schedules one `setImmediate` to flush ready work. It never waits
for a timer or a minimum batch size, and no transaction spans an `await`.
Each transaction accepts at most 32 jobs; the pending queue holds at most 128.
Further requests receive 503. `WRITE_BATCHING=false` keeps the independent
synchronous commit path for a same-image control.

Enqueue/claim, initial polling, reading/presence, fan-state/presence, and
result/next-lease/presence operations use the queue. Session authentication,
CSRF, payload validation and command response formats remain enforced. Fan
session/freshness checks run inside the queued operation so a preceding fan
report cannot leave a stale request authorized. Enqueue timestamps and TTL start
before the queue wait. Global FIFO order preserves per-device ordering.

Every response and device delivery follows successful outer COMMIT. Multi-job
transactions use per-request savepoints. A normal 4xx rejection rolls back its
rows and commit observers while valid neighbors continue. A storage or
programming error aborts the entire shared transaction and rejects every job.
An isolated job retains the original direct transaction path. WAL FULL and all
device journals, heartbeat, session and physical IR safeguards remain enabled.
Shutdown drains accepted jobs before closing the database and refuses new jobs.

Async resources preserve each request's trace identity. The physical
`db.transaction` span belongs to the first request; other requests retain their
own SQL spans and a `db.write_wait` span through the shared commit. A batch must
not be counted as a separate physical commit per waiting request.
`resac_db_write_batch_size` records bounded batch-attempt sizes, including failed
attempts and one-job controls. Optional span/metric errors do not hide successful
queue results. Result spans now await asynchronous persistence.

## Validation and trial gate

The 76 backend tests include route-level concurrent enqueues, current-session
validation, aborted-poll replacement, rollback of failed commits, independent
request trace contexts, queue limits, shutdown, and real process death before
and after shared COMMIT. Those crash tests recover zero or all 24 rows,
respectively, without acknowledging a job before the barrier. Existing result
handoff/duplicate/restart tests remain in place.

Before permanent deployment, compare batching disabled/enabled using the same
image and the unchanged production device. Repeat five 24-ping bursts per mode,
and 100 ordinary pings per mode. Keep the WAL warm across image-control
restarts. Require at least 20% lower pooled median burst completion and pooled
p95 completion gap, no more than 5% ordinary durable p95 regression, no new
reading loss or command failures/retries, and no increase in diagnostic drops
per measured command. Verify actual shared commits via batch metrics, restart
and downgrade recovery, fresh reports, and production rollback. Reject and close
the draft if it fails the gate. Reduced sync count alone does not qualify or
prove the full application is network bound.


## Live outcome: rejected (2026-11-06)

The unchanged production device ran against one candidate backend image with
batching disabled/enabled. Each mode completed 240 burst pings (ten bursts of
24) and 100 ordinary pings, all successfully on the first attempt.

| Measure | Independent commits | Grouped commits |
| --- | ---: | ---: |
| Median 24-ping burst completion | 4,546 ms | 4,053 ms |
| p95 gap between completions | 244 ms | 227 ms |
| Ordinary durable median, 100 pings | 189.76 ms | 184.06 ms |
| Ordinary durable p95, 100 pings | 239.27 ms | 262.47 ms |
| Diagnostic events dropped during measured bursts | 145 | 160 |
| Reading drops | 0 | 0 |

The 10.84% median burst improvement and 6.97% p95-gap improvement are below the
20% gate. Ordinary durable p95 was 9.70% higher, exceeding the 5% regression
limit. These independent samples include network/storage variation, so the
entire p95 difference cannot be attributed to batching. They provide no basis
for claiming an ordinary-command improvement.

Metrics confirm the intended batching behavior: measured burst phases handled
556 one-job transaction attempts in the control versus 557 jobs in 355 attempts
with batching. However, all 360 observed write jobs in the grouped ordinary
phase still ran as single-job transactions. An ordinary command's enqueue/lease
and final-result commits remain separate. Reducing concurrent enqueue syncs
therefore saves burst setup cost without removing these two required durable
waits. The implementation should not have been prioritized as a likely large
ordinary-latency improvement without quantifying that ceiling first.

The idle read-only keeper preserved WAL allocation across control restarts,
but the first control phase still grew the WAL from 3,996,432 to 4,128,272 bytes;
later measured phases retained that size. This can favor the grouped comparison.
Even the favorable pooled comparison missed the gate. The fully warm repeat
pair improved median burst completion only 7.05%.

Durable ordinary figures use paired traces through the end of the result-upload
server span after COMMIT. Burst figures use stored completion timestamps and
exclude the final commit tail. Neither measures dashboard rendering or the
result acknowledgment's return to the device. Current paired ordinary traces
still show about 50–51 ms combined transaction time per command and about 50 ms
warm result-HTTP transport excluding backend processing. Agent execution is
about 40–50 ms, including a nested 20 ms Bridge round trip. These overlapping
phases and their percentiles cannot be added to infer a full critical path.

The accepted backend image/source were restored with the live database retained.
The temporary keeper was removed. Six post-rollback pings and fresh reading/fan
reports passed; the fan remained calibrated off and the database quick check
passed. Device firmware, runtime, calibration, Wi-Fi files, and IR journals were
unchanged throughout this backend-only experiment. The draft is closed without
merging. Future implementation should follow an attributed critical-path bound,
rather than another speculative SQLite variation.
