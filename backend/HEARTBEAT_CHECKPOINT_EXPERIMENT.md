# Fan heartbeat checkpoint experiment

Unchanged five-second fan reports currently rewrite both the saved fan state
and device presence, forcing a fully durable SQLite commit for no new state.
The experiment keeps exact live freshness in process memory and checkpoints
unchanged fan timestamps at 60 seconds. `FAN_HEARTBEAT_CHECKPOINT_MS=0` retains
individual writes and is the default; `60000` enables the experiment.

Every change to the complete validated state, including session, revision,
firmware, control source and automatic settings/status, still commits before
HTTP 200 under `synchronous=FULL`. Lower revisions in the same session remain
rejected. Cache changes from a transaction publish only after successful commit;
reads inside a transaction see its uncommitted writes rather than an old cache.
Ordinary readings and command writes still persist presence, and graceful
shutdown flushes deferred timestamps using the last actual report time.

After process death, an unchanged fan heartbeat can recover with a timestamp up
to 60 seconds older. The live API remains exact during the process lifetime.
Recovery therefore errs toward reporting stale state; it cannot manufacture
freshness or change the last durable session/revision/state. This is an explicit
change to heartbeat timestamp persistence, not to command durability. No schema,
SQLite synchronization mode, device firmware or network configuration changes.

Additional `db.begin`, `db.commit` and `db.rollback` spans distinguish lock
acquisition from durable commit without needing a syscall tracer.

## Evidence and decision gate

Fresh accepted-production measurements, November7 UTC:

- 90 reading uploads: 100ms median / 160ms p95; transaction 27ms / 71ms.
- 100 fan uploads: 100ms median / 180ms p95; transaction 26ms / 81ms.
- 100 harmless pings: durable completion 185ms median / 293ms p95;
  two transactions together 50ms / 120ms.
- Metadata-only packet/syscall capture: 103 matched incoming packets,
  receive-to-userspace delay 0.16ms median / 1.59ms p95. No observed external
  socket retransmit-counter increase during that 65-second capture. This
  capture does not explain an earlier isolated multi-second network outlier.

On the same fan samples, a 60-second checkpoint counterfactual gives about
72–74ms median and 131–149ms p95 across checkpoint offsets. This assumes state
is unchanged and no separate presence-only commit; it is a ranking estimate,
not a measured effect. The expected p95 opportunity is uncertain (17–27%).

Production canary plan, declared before deployment:

1. Use the same candidate image with checkpoint disabled/enabled, in A/B/A/B
   order. Preserve the current WAL allocation across restarts with an idle
   read-only connection, without holding a read transaction.
2. Exclude 30 seconds for warmup/reconnect after each switch, then collect four
   minutes of ordinary traffic per window. Use paired device/backend traces
   whose backend start falls in that window; retain all tails and failures.
3. Primary endpoint: unchanged fan-report device HTTP median AND p95 improve
   at least20% in pooled candidate windows versus controls. Require consistent
   direction in both repeats. Readings, real state durability and harmless
   command success must remain healthy; investigate material regressions.
4. Keep FULL durability, revision/freshness safety, actual state changes and
   credentials unchanged. No physical IR test is needed for heartbeat reports.
5. If the gate fails, restore the accepted image/source, close the draft as an
   experiment and retain results. If it passes, publish results, merge and
   permanently deploy under the existing user authorization.

Validation: 67 backend tests pass, including independent SQLite reads, real
SIGKILL recovery, failed deferred-constraint COMMIT, rollback/nested revision
checks, clock rollback, shutdown timestamp accuracy and HTTP-route checks.

Production results: pending.
