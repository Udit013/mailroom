# Security notes

Mailroom is a local learning project using synthetic data. This document states what it
defends against and what it does not. It is not a claim that the system is secure.

## Threat model

**In scope:**

- A tenant, holding a valid API key, trying to reach another tenant's data
- An unauthenticated client on the API port
- Hostile or malformed uploads
- Malformed queue messages, and worker, relay or broker failures

**Out of scope:** a compromised host, a malicious operator, anyone with database or broker
credentials, and network attackers. There is no TLS, so the API must stay on loopback.

## What protects what

| Risk | Protection | Tested |
|---|---|---|
| Cross-tenant access | The tenant comes from the API key, API queries are scoped to it, and composite foreign keys reject cross-tenant rows. Workers look documents up by id and take the tenant from the document | Yes |
| Stolen keys in the database | Only SHA-256 hashes of long random tokens are stored | Yes |
| Oversized uploads | Bytes are counted as they arrive, so `Content-Length` isn't trusted | Yes |
| Disguised file types | The type comes from the content, not the extension or the declared type | Yes |
| Path traversal | Paths are built from the tenant id and hash, never from the filename | Yes |
| SQL injection | All queries use bound parameters | By construction |
| Hostile PDFs | pypdf's decompression limits, page and text caps, the stage time limit, and memory recycling of workers. Parser failures become `CORRUPT_DOCUMENT` | Malformed files only |
| Poison documents | Characters Postgres rejects are removed. Unexpected errors fail once, and attempts are limited | Yes |
| Secrets in logs | An allowlist of log fields, and exception text is dropped | Yes |
| Sensitive data in the audit trail | Events store codes and field names, not extracted values | Yes |
| Lost reviewer updates | Version check under a row lock, returning 409 | Yes |
| Double filing | A stored idempotency key per step, deduplicated by the receiver | Yes, against the fake |

## Accepted risks

These are real, and accepted for a local project:

- **The application connects as a database superuser**, so the append-only audit trigger and
  any future row-level security can be bypassed by application-level SQL. Use a separate
  low-privilege role before any shared deployment.
- **Tenant isolation is enforced by application code**, with composite foreign keys behind
  it. Row-level security is designed, not built.
- **No rate limits or storage quotas.** A valid key can fill the disk, up to 10 MB per file.
- **API keys never expire**, and `last_used_at` is not recorded. Revocation works.
- **Files and extracted fields are stored unencrypted**, including names in `stage_results`.
- **PDFs are not scanned.** Only the signature is checked at upload, and they are parsed in
  the worker, not in a sandbox.
- **`/readyz` is unauthenticated** and opens connections on each call. It relies on the API
  port staying on loopback.
- **Failpoints ship in the package.** They need an environment variable, so reaching them
  requires access to the host.
- **Concurrent duplicates can fail a healthy document early.** Each delivery counts an attempt
  before it knows it's a duplicate. In a probe of 10 runs per case, a document already at 4
  attempts failed every time with 2 concurrent copies, and one at 3 attempts with 3 copies.
  From 0 attempts it happened with 12 copies (3 of 10 runs), and not with 8 or fewer. Compose
  runs two worker processes. It can't cause a duplicate outcome, but `FAILED` has no retry
  route.
- **The sweeper adds duplicates during a long worker outage**, up to 100 documents every 30
  seconds. They do nothing when processed, but the queue grows until workers return.
- **One outbox row that can never be published would block the rows behind it.** The
  database restricts stages and every stage has a queue, so no such row can be created today.
- **Any valid key can resolve reviews.** There are no roles, and the audit trail records the
  key's name rather than a person.
- **The API key is typed into the review page.** It stays in memory, and there is no login,
  session or logout on the server.
- **Reviewer corrections are recorded, not re-run.** A corrected case number is stored on the
  review item, but the document is not matched again.
