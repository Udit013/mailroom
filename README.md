# Mailroom

Document intake and case matching for a fictional law firm. All documents and
data are synthetic. This is a three-day learning project and is not
production-ready.

Workflow: ingestion, validation, extraction, classification, case matching,
decision, then filing or human review, with an audit trail throughout.

PostgreSQL is the source of truth. RabbitMQ carries only a document id and a
stage. Delivery is at-least-once, so every stage is idempotent, and a
transactional outbox records each dispatch in the same transaction as the state
change that caused it.

## Current status

Working: authenticated upload of PDF and plain text, a transactional outbox with
a relay and sweeper, and four Celery stages using deterministic stub logic. An
uploaded document ends in `READY_TO_FILE`, `NEEDS_REVIEW`, `UNMATCHED` or `FAILED`.

Not built yet: real extraction and matching, filing and the review page. Stubs
read uncompressed text only, so a real PDF ends in review rather than being
guessed at.

## Setup

You need Docker with Compose v2, and `make`. Python does not need to be
installed on your machine, because everything runs in containers.

1. Create your local configuration, then change both passwords in `.env`. The
   file is gitignored. Use letters, digits and hyphens only, because passwords
   are placed inside connection URLs.

   ```bash
   cp .env.example .env
   ```

2. Build and start everything. This waits until services are up and applies
   migrations.

   ```bash
   make up
   ```

3. Load the fictional demo data. The API key is printed once and only its hash
   is stored.

   ```bash
   make seed
   ```

4. Check the API. Both calls should return JSON with HTTP 200. If you changed
   `API_PORT` in `.env`, use that port.

   ```bash
   curl -i http://127.0.0.1:8000/healthz
   ```

   ```bash
   curl -i http://127.0.0.1:8000/readyz
   ```

5. Upload a document. Replace the key with the one `make seed` printed. A
   repeat upload of the same bytes returns the existing document with HTTP 200.

   ```bash
   curl -i -H "X-API-Key: YOUR_KEY" -F file=@README.md http://127.0.0.1:8000/documents
   ```

## Commands

| Command | What it does |
|---|---|
| `make up` | Build and start the API, relay, worker and their dependencies |
| `make down` | Stop everything and keep the data |
| `make migrate` | Apply database migrations |
| `make seed` | Load fictional demo data, safe to repeat |
| `make demo` | Start the stack, seed it, call both health endpoints, and upload samples |
| `make lint` | Run Ruff and strict mypy |
| `make test` | Run all tests against real PostgreSQL and RabbitMQ |

`make test` uses a separate Compose project, because the suite truncates tables
and purges queues. It can't touch your development data. Pass pytest options
with `ARGS`:

```bash
make test ARGS='tests/unit -q'
```

To delete local data as well:

```bash
docker compose down -v
```

## Health endpoints

| Endpoint | Meaning | Result |
|---|---|---|
| `/healthz` | The process is alive. Checks no dependency. | Always 200 while the process runs |
| `/readyz` | PostgreSQL and RabbitMQ respond within 2 seconds. | 200, or 503 naming the failed check |

## Reliability

Nothing here is exactly-once, because no broker can offer that across a database
and a queue. Delivery is at-least-once, and the outcome happens once:

1. **A committed dispatch survives.** A document and its outbox row commit in one
   transaction, so neither exists without the other. The row stays pending until
   the broker confirms it, however long that takes.
2. **A dispatch may be sent twice.** The relay marks a row published only after
   the broker confirms it. A crash between the confirm and that commit sends the
   message again. This is deliberate: duplicates are safe and lost messages are
   not.
3. **A duplicate does nothing.** Each delivery locks the document and checks it
   against Postgres. A message that is no longer current is a logged no-op.
4. **A stage is all or nothing.** Its results, its state change and the next
   outbox row commit together. If a worker dies, Postgres rolls back, the broker
   redelivers, and the attempt count stops a poison document from looping forever.
5. **A lost message is recovered.** The sweeper re-dispatches any `QUEUED` or
   `PROCESSING` document that has stopped moving.
6. **A stuck relay restarts.** Each relay pass has a hard deadline. If one hangs,
   the process exits, its locks are released, and Compose starts it again.

## Configuration

Compose reads `.env`, and `.env.example` lists the values you must set. Optional
tuning values and their defaults are in `src/mailroom/config.py`. No secrets
belong in the repository.

Application logs are one JSON object per line. Extra fields pass through an
allowlist, and exceptions keep only their type and location, so document contents
and credentials stay out of logs. A few lines are plain text and carry no document
data: Celery's shutdown notice, and the stack dump when a relay pass hits its
deadline.
