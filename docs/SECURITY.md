# Security notes

Mailroom is a learning project using synthetic data. This document states what it defends
against and what it does not. It is not a claim that the system is secure.

It can run locally, or as a public demo on free hosted services (Vercel, Render, Neon and
CloudAMQP), as described in [DEPLOY.md](DEPLOY.md). **A public deployment is for synthetic data
only.**

## Threat model

**In scope:**

- A tenant, holding a valid API key, trying to reach another tenant's data
- An unauthenticated client on the API port, or on the public internet in a deployment
- Hostile or malformed uploads
- Malformed queue messages, and worker, relay or broker failures

**Out of scope:** a compromised host, a malicious operator, anyone with database or broker
credentials, denial of service, and real or sensitive documents.

## Deployment exposure

The application has no TLS of its own. Locally, Compose binds the API to `127.0.0.1:8000`.
In the hosted demo, Render and Vercel terminate HTTPS.

| Surface | Local | Hosted demo |
|---|---|---|
| Review page | `127.0.0.1:5173` | HTTPS on Vercel |
| API | `127.0.0.1:8000` | HTTPS on Render, directly and through Vercel under `/api` |
| `/healthz` | Loopback | Public, and reveals nothing |
| `/readyz` | Loopback | Public. Shares one result for 5 seconds and names no hosts |
| PostgreSQL | Docker network | Neon, TLS required, reachable from the internet with its password |
| RabbitMQ | Docker network | CloudAMQP, `amqps://` with the certificate and host name checked |

No CORS is configured: the page calls `/api` on its own origin, and Vercel forwards it. Every
API response carries `Cache-Control: no-store`, so the forwarding proxy keeps no copy.

## What protects what

| Risk | Protection | Tested |
|---|---|---|
| Cross-tenant access | The tenant comes from the API key, API queries are scoped to it, and composite foreign keys reject cross-tenant rows. Workers look documents up by id and take the tenant from the document | Yes |
| Stolen keys in the database | Only SHA-256 hashes of long random tokens are stored | Yes |
| Oversized uploads | Bytes are counted as they arrive, so `Content-Length` isn't trusted. The hosted demo lowers the limit to 1 MB | Yes |
| Disguised file types | The type comes from the content, not the extension or the declared type | Yes |
| Path traversal | Bytes are stored in Postgres under the tenant id and content hash. The client's filename is display text only | Yes |
| SQL injection | All queries use bound parameters | By construction |
| Hostile PDFs | pypdf's decompression limits, page and text caps, the stage time limit, and memory recycling of workers. Parser failures become `CORRUPT_DOCUMENT` | Malformed files only |
| Poison documents | Characters Postgres rejects are removed. Unexpected errors fail once, and attempts are limited | Yes |
| Secrets in logs | An allowlist of log fields, and exception text is dropped | Yes |
| Sensitive data in the audit trail | Events store codes and field names, not extracted values | Yes |
| Document text sent to an outside model | Off by default. Refuses to start unless synthetic data only is declared. Text is cut to 8,000 characters. The key is sent in a header and never logged | Yes |
| A document that manipulates the model | The model can only send a document to review, never file it, and only an allowed type is accepted | Yes |
| Lost reviewer updates | Version check under a row lock, returning 409 | Yes |
| Double filing | A stored idempotency key per step, deduplicated by the receiver | Yes, against the fake |

## Accepted risks

These are real, and accepted for a local project or a synthetic-data demo. They would not be
acceptable with real documents:

- **The application connects as the database owner** (a superuser locally), so the
  append-only audit trigger and any future row-level security can be bypassed by
  application-level SQL. A separate low-privilege role would be needed before handling
  anything real. The hosted database is reachable from the internet, protected by its password
  and TLS.
- **Tenant isolation is enforced by application code**, with composite foreign keys behind
  it. Row-level security is designed, not built.
- **No rate limits or per-tenant quotas.** A public deployment can be flooded, and a valid key
  can fill the database, up to the upload limit per file. `MAX_DATABASE_BYTES` refuses uploads
  with 507 before a hosted plan's hard cap, which protects processing, not fairness.
  The API's concurrency limit bounds work in progress, not abuse.
- **No monitoring or failover.** One free instance runs everything. Free tiers sleep, can be
  restarted at any time, and can be suspended when a monthly quota runs out. Neon keeps only a
  short restore window, so backups are taken with `pg_dump` from your own machine.
- **API keys never expire**, and `last_used_at` is not recorded. Revocation works.
- **Files, extracted text and extracted fields are stored unencrypted**, including names in
  `stage_results` and full text in `document_texts`.
- **PDFs are not scanned.** Only the signature is checked at upload, and they are parsed in
  the worker, not in a sandbox.
- **`/readyz` is unauthenticated.** Callers share one result for 5 seconds
  (`READINESS_CACHE_SECONDS`), so a flood of requests can't open a connection each, but it
  still reveals whether Postgres and RabbitMQ are reachable.
- **The model advisor uses Gemini's free tier**, whose inputs may be used to improve Google's
  products. It must only ever see synthetic documents; the setting that enables it is a
  declaration, not a check.
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
