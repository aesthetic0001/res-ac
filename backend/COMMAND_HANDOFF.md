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

Candidate validation covers commit rollback, discarded-response retry, backend
reopen, lease/TTL boundaries, poll coexistence, trace contexts, older-server
fallback, malformed frames and pre-IR journaling. Production performance and
rollout acceptance are pending. Network-bound completion is not claimed.
