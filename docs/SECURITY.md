# Security notes

Mailroom is a learning project using synthetic data. This document states what it defends
against and what it does not. It is not a claim that the system is secure.

It can run locally, or as a public demo on one host behind Caddy, as described in
[DEPLOY.md](DEPLOY.md). **A public deployment is for synthetic data only.** No real Oracle
deployment has been tested.

## Threat model

**In scope:**

- A tenant, holding a valid API key, trying to reach another tenant's data
- An unauthenticated client on the API port, or on the public internet in a deployment
- Hostile or malformed uploads
- Malformed queue messages, and worker, relay or broker failures

**Out of scope:** a compromised host, a malicious operator, anyone with database or broker
credentials, denial of service, and real or sensitive documents.

## Deployment exposure

The application has no TLS of its own, so uvicorn must never be published directly. Locally,
and in the documented deployment, Compose binds the API to `127.0.0.1:8000`. In a deployment,
Caddy terminates HTTPS and is the only public entry point.

| Surface | Local | Public deployment |
|---|---|---|
| Review page | `127.0.0.1:5173` | HTTPS through Caddy |
| API | `127.0.0.1:8000` | HTTPS through Caddy, under `/api` |
| `/healthz` | Loopback | Public, and reveals nothing |
| `/readyz` | Loopback | Blocked by Caddy, available on the host |
| PostgreSQL and RabbitMQ | Docker network | Docker network, never published |
| SSH | Not applicable | Restricted to one IP address, with key login |

Traffic between Caddy and the API, and between containers, is unencrypted, but stays on one
host. No CORS is configured, because the page and API share an origin in both setups.

## What protects what

| Risk | Protection | Tested |
|---|---|---|
| Cross-tenant access | The tenant comes from the API key, API queries are scoped to it, and composite foreign keys reject cross-tenant rows. Workers look documents up by id and take the tenant from the document | Yes |
| Stolen keys in the database | Only SHA-256 hashes of long random tokens are stored | Yes |
| Oversized uploads | Bytes are counted as they arrive, so `Content-Length` isn't trusted. Caddy caps request bodies in a deployment | Yes, Caddy's cap in a local rehearsal |
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

These are real, and accepted for a local project or a synthetic-data demo. They would not be
acceptable with real documents:

- **The application connects as a database superuser**, so the append-only audit trigger and
  any future row-level security can be bypassed by application-level SQL. The database isn't
  published, which limits this, but a separate low-privilege role would be needed before
  handling anything real.
- **Tenant isolation is enforced by application code**, with composite foreign keys behind
  it. Row-level security is designed, not built.
- **No rate limits or storage quotas.** Caddy's standard build has no rate limiter, so a
  public deployment can be flooded, and a valid key can fill the disk, up to 10 MB per file.
  The API's concurrency limit bounds work in progress, not abuse.
- **One host, with no monitoring or failover.** Oracle may reclaim idle Always Free resources,
  so a deployment can disappear. Backups must be kept off the VM.
- **API keys never expire**, and `last_used_at` is not recorded. Revocation works.
- **Files, extracted text and extracted fields are stored unencrypted**, including names in
  `stage_results` and full text in `document_texts`.
- **PDFs are not scanned.** Only the signature is checked at upload, and they are parsed in
  the worker, not in a sandbox.
- **`/readyz` is unauthenticated.** Callers share one result for 5 seconds
  (`READINESS_CACHE_SECONDS`), so a flood of requests can't open a connection each, but it
  still reveals whether Postgres and RabbitMQ are reachable.
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
  session or logout on the server. Treat it as a password, and never put it in a build
  variable or in the repository.
- **Reviewer corrections are recorded, not re-run.** A corrected case number is stored on the
  review item, but the document is not matched again.
