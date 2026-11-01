# 6. Rules and an explainable matcher

**Status:** Accepted

**Decision:** A rule-based classifier behind an interface, and a pure weighted matcher that returns
a per-feature breakdown. Uncertainty goes to a person.

**Alternatives:** An LLM classifier or matcher.

**Consequences:** Results are deterministic, testable and explainable. Rules are brittle on real
documents, and the thresholds are unvalidated.

TODO(me): A misfiled legal document costs a firm money, so I would rather send too many to a person
than guess between similar clients.
