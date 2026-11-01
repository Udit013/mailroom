# 7. Step-by-step idempotent filing

**Status:** Accepted

**Decision:** Filing runs as separate steps, each with a stored idempotency key. Progress commits
after each step, and no lock is held during an external call.

**Alternatives:** One call inside the stage's locked transaction, which holds a row lock across a
network call and can't resume part-way.

**Consequences:** A retry resumes at the first incomplete step. An external call can repeat, so the
guarantee depends on the receiver deduplicating on the key. Without that, a timeout after success
can't be told apart from a failure.

TODO(me): A timeout tells you nothing about whether the work happened, so the only safe retry is one
the other side can recognise.
