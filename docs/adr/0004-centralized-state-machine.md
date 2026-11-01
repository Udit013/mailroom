# 4. Centralized state machine with a conditional update

**Status:** Accepted

**Decision:** State changes go through `advance()`, which runs one
`UPDATE ... WHERE state = expected` and writes its audit event in the caller's transaction. A
forbidden transition raises, and a stale one returns `False`. A test fails if other code writes
state.

**Alternatives:** Reading the state and then writing it, which races when a message arrives twice.

**Consequences:** The check and the write are one atomic statement. The guarantee relies on READ
COMMITTED, under which a blocked update re-checks its condition.

TODO(me): Losing a race is normal with at-least-once delivery, so it returns a result rather than
raising an error.
