# 3. Transactional outbox and at-least-once delivery

**Status:** Accepted

**Decision:** A state change and its dispatch commit in one transaction. A relay publishes from
the outbox with publisher confirms and `FOR UPDATE SKIP LOCKED`, and marks a row sent only after
the broker confirms it. Consumers treat every message as a possible duplicate.

**Alternatives:** Committing and then publishing, which loses the message after a crash in
between. Publishing and then committing, which can dispatch work for a change that rolled back.

**Consequences:** Messages can be duplicated, so every stage must be idempotent. Published rows
are never purged, so the table grows.

TODO(me): Exactly-once across a queue and a database isn't realistic, so I chose the failure I can
handle. A duplicate is a no-op, and a lost message is a lost document.
