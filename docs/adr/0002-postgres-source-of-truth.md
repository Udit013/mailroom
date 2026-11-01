# 2. PostgreSQL is the source of truth

**Status:** Accepted

**Decision:** All state lives in PostgreSQL. A message carries only a document id and a stage.

**Alternatives:** Carrying state or document content in messages.

**Consequences:** A message can be lost, repeated or reordered without losing information, and
the sweeper can rebuild any missing message from the database. Every delivery needs a database
read.

TODO(me): If state lives in messages, recovery means trusting whatever the queue still holds, and
nobody can inspect where a document really is.
