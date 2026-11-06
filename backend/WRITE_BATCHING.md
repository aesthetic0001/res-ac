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
