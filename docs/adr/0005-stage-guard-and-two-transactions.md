# 5. A stage guard and two transactions

**Status:** Accepted

**Decision:** Each delivery runs two transactions. The first locks the document, checks the message
is current, and commits an attempt count. The second locks it again, checks again, and commits the
stage's result, state change and next outbox row together. Locks block rather than use `NOWAIT`.

**Alternatives:** Leases with a reaper. One transaction, which would lose the attempt count on a
crash. `NOWAIT`, which was tried and stalled documents, because a lock holder may be a stale
message that does no work.

**Consequences:** A worker that dies needs no cleanup, because Postgres releases its locks. A
duplicate holds a worker slot while it waits. Slow stage work would need leases instead.

TODO(me): The attempt must commit first, or a document that kills its worker would loop forever
with a count that always rolls back.
