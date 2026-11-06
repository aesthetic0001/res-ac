# Result acknowledgment and next-command delivery

The opt-in result route accepts `?next=1`; adding `format=binary-v1` negotiates
the existing bounded binary response format. A successful response includes
`ok`, `duplicate`, `resultId`, and `commands` (zero or one command). Legacy
result requests retain their existing JSON response and ordinary polling.

The result, next lease, and a persisted `command_handoffs` association commit
together with WAL `synchronous=FULL`. The server sends the response only after
successful COMMIT. This removes a subsequent GET exchange and separate claim
commit when another command is already queued. If a poll is already waiting,
that poll receives the next command after the same commit; the result response
then has an empty command list.

A lost response must not strand the next lease or select a different action.
Repeating the exact result returns the same mapped, still-active command without
incrementing attempts while its lease remains valid. An expired lease can renew
only that mapped command, within its original command TTL. A completed or expired
mapped command is never returned. A committed final-result row also seals an
empty initial decision, so ordinary commands need no extra handoff-table write.
Empty initial decisions remain empty on retry;
a duplicate result received without a handoff association also remains empty.
Conflicting results and cross-device requests retain their existing rejection.
Associations persist across backend restart and are removed if their commands
are later deleted. Rollback to an older backend can ignore the additive table.

The native-mode agent checks the acknowledgment's result ID and response shape
before accepting a handoff. Malformed responses retain the previous durable
result for retry. Older JSON acknowledgments fall back to the existing poll.
Normal command validation, execution exclusivity, session checks and deadlines
still apply. The next IR action requires its own durable uncertainty journal
before dispatch. A restart reports that journal rather than replaying the action.
No new timer, HTTP connection, or GPIO operation is added by the handoff itself.

The accepted `6f8041a` release completed a 12-ping burst in 3074 ms, with
successive completions 204–314 ms apart. Eleven queued commands required an
additional claim transaction (17.41 ms median, 73.44 ms maximum). The ordinary
100-ping baseline remains 154.5/220 ms stored median/p95, or 176.68/246.11 ms
through durable server completion, versus roughly 48 ms network RTT.

## Production acceptance, 2026-11-06

The matched burst baseline is release `6f8041a`; the final candidate backend is
`518b6ef` with device runtime `c002b36`. Each burst run contains five rounds of
twelve pings. Every command succeeded on its first attempt.

| Measurement | Baseline | Candidate |
| --- | ---: | ---: |
| Median burst completion | 3142 ms | 2226 ms |
| Largest of five burst times | 3252 ms | 2474 ms |
| Completion-gap median | 256 ms | 170 ms |
| Completion-gap p95 | 352 ms | 240 ms |
| Ordinary ping stored median/p95, 100 commands | 154.5/220 ms | 164/235 ms |
| Ordinary ping durable median/p95 | 176.68/246.11 ms | 197.18/262.60 ms |

Burst median improves 29.2% and completion-gap p95 improves 31.8%, exceeding the
20% acceptance gate. Ordinary-command improvement is not claimed. In those
captures, the median sum of each command's measured outer transaction intervals
rose from 50.34 to 63.07 ms. Subtracting those paired intervals left a median
124.74 versus 126.26 ms and p95 180.51 versus 173.39 ms. This suggests storage
variation accounts for much of the ordinary-command difference; it does not
prove causality. Device command handling remained 40/60 ms median/p95.

Persisted records confirm 55 of 60 burst commands used result handoffs and that
100 ordinary commands created no empty handoff rows. Five calibrations followed
by queued levels 1, 2, 1, off all succeeded once; each level recorded one IR press
and the three inter-level handoffs were persisted. Final off state, calibration
and session were retained, and stale-session submission was rejected.

A live backend restart during a separate twelve-ping burst recovered all pings
on their first attempt and resumed fresh reports. A retry of a completed result
did not advance the queue. The live active-lease retry probe raced completion;
active-lease discarded-response recovery and backend reopen are covered by the
deterministic tests. Test sessions were removed. Final reading/telemetry drop
counts and queue depths were zero.

Validation: 194 device tests and 59 backend tests pass. They cover commit
rollback, discarded-response retry, backend reopen, lease/TTL boundaries, poll
coexistence, trace contexts, older-server fallback, malformed frames and durable
pre-IR journaling. Delivery traces identify `poll`, `result` and `result_retry`;
`db.transaction` records the actual outer commit rather than a nested no-commit
wrapper. Network RTT remains about 48 ms; network-bound completion is not claimed.
