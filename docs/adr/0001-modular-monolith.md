# 1. Modular monolith

**Status:** Accepted

**Decision:** One Python package with separate API, relay and worker processes, sharing one
database.

**Alternatives:** Microservices.

**Consequences:** One deployable image and one schema. A stage can't scale or fail in its own
process, though one queue per stage leaves room to split workers later.

TODO(me): Microservices add network failures and operating cost, and nothing at this size needs
independent deployment.
