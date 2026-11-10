# Deploying a synthetic-data demo on free services

This guide runs Mailroom on free tiers, with no card needed on most of them:

| Part | Service | Free plan |
|---|---|---|
| Review page | Vercel | Hobby |
| API, relay and worker, in one container | Render | Free web service |
| PostgreSQL, including document bytes | Neon | Free |
| RabbitMQ | CloudAMQP | Little Lemur |
| Optional model advisor | Gemini API | Free tier |
| Tests on every push | GitHub Actions | Free for public repositories |

**Synthetic data only.** Free tiers are not built for real documents, and Gemini's free tier
may use what it is sent. Security assumptions are in [SECURITY.md](SECURITY.md).

**Status:** the code paths were tested locally: the supervisor, Postgres storage, TLS options
and a 512 MB, 0.1 CPU container. The services themselves have not been verified yet. Treat
each step as untested until you have done it once, and record what differed.

## How it fits together

```mermaid
flowchart LR
  browser[Browser] --> vercel["Vercel: review page"]
  vercel -- "/api/* forwarded" --> render
  subgraph render["Render free web service"]
    sup["mailroom.supervisor: migrates, then runs"] --> api[API]
    sup --> relay[Relay and sweeper]
    sup --> worker[Celery worker]
  end
  api & relay & worker --> neon[("Neon Postgres")]
  relay --> amqp[["CloudAMQP"]]
  amqp --> worker
  worker -. optional .-> gemini[Gemini]
```

Render's free plan has no background workers, so one process,
[`mailroom.supervisor`](../src/mailroom/supervisor.py), runs all three and restarts the relay
or worker if one exits. Render has no persistent disk, so document bytes are stored in Postgres.

## What free means here

Check each provider's current terms before you rely on these. They were read in November 2026.

| Service | Limit that matters | When it runs out |
|---|---|---|
| Render | Sleeps after 15 minutes without inbound requests and takes about a minute to wake. 750 instance hours a month. 512 MB, 0.1 CPU. May restart a free service at any time. No shell, no one-off jobs | Hours: suspended until next month. Bandwidth or build minutes: **billed if a payment method is on file**, otherwise suspended or builds disabled |
| Neon | 100 compute hours a month, 1 GB storage, 5 GB transfer. Sleeps after 5 minutes idle | Compute or transfer: suspended until next month, data kept. Storage: inserts, updates and deletes fail |
| CloudAMQP | 1 million messages a month (published plus delivered), 20 connections, 10,000 queued messages. Queues idle for 28 days may be removed; an instance unused for two months is deleted | Publishing is blocked until next month. Uploads still work and wait in the outbox |
| Vercel | Personal, non-commercial use | Paused, not billed |
| Gemini | A small daily request limit, shown in AI Studio | 429 errors, so no opinion. The rules carry on |

**Avoiding charges:** don't add a card to Render unless it insists. If it does, keep an eye on
bandwidth in the Render dashboard; this demo moves very little. Never enable billing on the
Google project that holds the Gemini key.

**While the service sleeps nothing is processed.** An upload is a request, so it wakes the
service, and work finishes well within the 15 minutes before it sleeps again. Anything left
mid-way when it stops is recovered on the next start: Postgres rolls back, the broker
redelivers, and the sweeper re-sends anything idle for two minutes.

## 1. Neon: the database

1. Sign up at [neon.com](https://neon.com) and create a project. Choose **Postgres 16**, the
   version used locally, and the region nearest you. Note the region; Render should be near it.
2. In the project's compute settings, set the maximum size to **0.25 CU**. That stretches the
   100 free compute hours to about 400 awake hours a month.
3. Open **Connect**, turn **connection pooling off**, and copy the connection string. It looks
   like `postgresql://USER:PASSWORD@ep-NAME.REGION.aws.neon.tech/neondb?sslmode=require`.
   The pooled string has `-pooler` in its host; don't use it, because the pooler rejects the
   statement and idle-transaction timeouts Mailroom sets on each connection.
4. Change the start from `postgresql://` to `postgresql+psycopg://`. Keep the result private:
   this is your `DATABASE_URL`.

## 2. CloudAMQP: the broker

1. Sign up at [cloudamqp.com](https://www.cloudamqp.com) and create an instance on the
   **Little Lemur** (free) plan, in a region near Neon's.
2. Open the instance and copy the **AMQP URL**. Change `amqp://` to `amqps://` if needed, and
   add the TLS port, so it looks like `amqps://USER:PASSWORD@HOST:5671/VHOST`. This is your
   `BROKER_URL`. Mailroom checks the broker's certificate and host name.

## 3. Load fictional data, from your own machine

Render's free plan has no shell, so seeding runs locally against Neon. Docker must be running.
`read -rs` keeps the values out of your shell history and off the screen; paste each and press
Enter.

```bash
read -rs DATABASE_URL && export DATABASE_URL
```

```bash
read -rs BROKER_URL && export BROKER_URL
```

```bash
docker compose run --rm --no-deps --build -e DATABASE_URL -e BROKER_URL api alembic upgrade head
```

```bash
docker compose run --rm --no-deps -e DATABASE_URL -e BROKER_URL api python -m mailroom.seed
```

The seed prints an API key once. Keep it somewhere safe: only its hash is stored. Running the
seed again won't print a new one.

## 4. Render: the API, relay and worker

1. Sign up at [render.com](https://render.com) with GitHub, and allow it to read the
   `mailroom` repository.
2. Choose **New**, then **Blueprint**, and select the repository. Render reads
   [`render.yaml`](../render.yaml): one free web service called `mailroom-api`.
3. It asks for the secret values. Paste `DATABASE_URL` and `BROKER_URL`. Leave
   `GEMINI_API_KEY` empty unless you set up step 6.
4. If you changed regions, edit `region` in `render.yaml` first. Render's free services must
   be in the same region as nothing else here, but a nearby region keeps every stage faster.
5. Deploy. The first build takes several minutes. In the logs, expect migrations, then three
   `process started` lines for `api`, `relay` and `worker`.
6. Note the service's address, like `https://mailroom-api-XXXX.onrender.com`, and check it:

```bash
curl -s https://YOUR-RENDER-HOST/healthz
```

## 5. Vercel: the review page

1. In [`web/vercel.json`](../web/vercel.json), replace
   `REPLACE-WITH-YOUR-RENDER-HOST.onrender.com` with your Render host, and commit it.
2. Sign up at [vercel.com](https://vercel.com) with GitHub and import the repository.
3. Set **Root Directory** to `web`. Vercel detects Vite. Deploy.
4. Open the address Vercel gives you and enter the API key from step 3.

The page calls `/api/...` on its own address, and Vercel forwards it to Render, so the browser
never needs CORS. The API sends `Cache-Control: no-store` on every response, so Vercel keeps no
copy.

## 6. Optional: the model advisor

Read the Model advisor section of the [README](../README.md#model-advisor) first.

1. In [Google AI Studio](https://aistudio.google.com), create an API key in a project with
   **no billing account**. With billing, usage becomes paid.
2. In Render, set `GEMINI_API_KEY`, `ADVISOR=gemini` and `ADVISOR_SYNTHETIC_DATA_ONLY=true`,
   then redeploy. The worker refuses to start with the first two and not the third.

To measure the advisor on the synthetic evaluation set, locally, with the key in your shell:

```bash
read -rs GEMINI_API_KEY && export GEMINI_API_KEY
```

```bash
make evaluate-advisor
```

It writes `docs/ADVISOR_EVALUATION.md`. If the daily limit stops it, run it again the next day:
answers already received are cached in `.cache/`.

## 7. Verify end to end

Use the Vercel address as `SITE` and your key as `KEY`:

```bash
read -r SITE && read -rs KEY && export SITE KEY
```

| Check | How | Expected |
|---|---|---|
| Page loads | Open `SITE` | The review page |
| Page reaches the API | Enter the key | The review queue loads |
| Wrong key | Enter any other text | Refused, nothing listed |
| API reaches Neon and CloudAMQP | `curl -s $SITE/api/readyz` | `"status":"ready"` |
| Migrations ran | Render logs | `alembic` output before `process started` |
| Upload is stored | The upload command below | `202` and a document id |
| Background work runs | Poll the document | It leaves `QUEUED` and ends in `FILED` or `NEEDS_REVIEW` |
| Results reach the page | Refresh the page | A review item appears for a doubtful document |
| Data survives a restart | In Render, **Manual Deploy**, then poll again | The same document, same state |
| Recovery after a restart mid-work | Upload several, restart at once, poll | All reach a final state; none `FAILED` |
| Retries and failures | Upload a damaged PDF | `NEEDS_REVIEW` with `CORRUPT_DOCUMENT` |

```bash
printf 'Complaint for damages.\nCase No. FIC-2026-001\nClient: Aldermere Holdings\nRef %s\n' "$(date +%s)" > /tmp/mailroom-demo.txt
```

```bash
curl -s -H "X-API-Key: $KEY" -F file=@/tmp/mailroom-demo.txt $SITE/api/documents
```

```bash
curl -s -H "X-API-Key: $KEY" $SITE/api/documents/DOCUMENT_ID
```

The first request after the service has slept takes about a minute. Vercel waits up to 120
seconds for Render, so it should arrive; if it doesn't, retry.

**What this can't show on free tiers:** killing the worker by hand (no shell), the broker or
database going down (managed services), and behaviour over a whole month of quotas. Those are
covered locally by `make test` and `make drills`.

## Updating

Pushing to `main` runs CI on GitHub and redeploys Render and Vercel. Render starts the new
version, runs migrations under an advisory lock, then stops the old one. Old code runs briefly
against the new schema, so a migration must only add things; never drop or rename a column in
the same release that stops using it.

## Back up and restore

Neon keeps a short restore window only. Back up with `pg_dump` from your machine; the document
bytes are in the database, so one dump holds everything. RabbitMQ needs no backup: the sweeper
re-dispatches active documents from Postgres. Use the Postgres 16 image so the client matches
the server.

```bash
read -rs PGURL && export PGURL
```

Paste the Neon string from step 1 in its original `postgresql://` form.

```bash
mkdir -p mailroom-backups && docker run --rm -e PGURL postgres:16-alpine sh -c 'pg_dump "$PGURL" -Fc' > mailroom-backups/$(date +%Y-%m-%d-%H%M).dump
```

To restore, suspend the Render service in its settings, so nothing writes during the restore,
then replace the database. This deletes its current contents, so check you have the right file.

```bash
docker run --rm -i -e PGURL postgres:16-alpine sh -c 'pg_restore --clean --if-exists --no-owner -d "$PGURL"' < mailroom-backups/BACKUP_NAME.dump
```

Resume the service; it migrates on start. `--clean` leaves behind any table newer than the
backup, so restore only a backup taken on the same or a later version of Mailroom, or restore
into a new, empty Neon branch instead.

## If something runs out

- **Render hours or Neon compute:** everything waits until the next month. Nothing is lost.
- **CloudAMQP messages:** uploads keep working and wait in the outbox, and are sent next month.
- **Neon storage:** uploads are refused with 507 at `MAX_DATABASE_BYTES`, before Neon's hard cap.
  Delete old synthetic documents with SQL to make room.
- **CloudAMQP deleted an idle instance:** create a new one, update `BROKER_URL` in Render and
  redeploy. Postgres holds all state, and the sweeper re-sends active documents.
