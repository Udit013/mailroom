# Failure modes

What happens when each part fails. "Drill" means [DRILLS.md](DRILLS.md) exercises it on a real
stack. "Test" means the automated suite covers it. "Reasoned" means it follows from the design
but has not been exercised.

## Processes and crashes

| Failure | Behaviour | Recovery | Evidence |
|---|---|---|---|
| Worker child killed mid-stage | Postgres rolls back, the message is redelivered, and the attempt stays counted | Automatic | Test, at every stage |
| Whole worker killed | The same, and work resumes when a worker returns | `restart: unless-stopped`, or `docker compose start worker` after `docker kill` | Drill, test |
| A document that keeps killing workers | Fails after 5 attempts with `ATTEMPTS_EXHAUSTED` | Manual: no retry route | Test |
| Stage runs over 60 seconds | The child is killed and the sweeper re-dispatches it | Automatic, up to 5 attempts | Test |
| Worker killed just after an external call | The call is repeated with the same key, and the receiver deduplicates | Automatic | Test, for each step |
| Relay killed after publishing, before commit | The row stays unpublished, so the message is sent twice and the duplicate does nothing | Automatic | Test |
| Relay stuck on a broker that stopped mid-frame | The pass deadline ends the process, and rows stay unpublished | Automatic restart | Test for the exit, and separately for restart. Not both together |
| API process exits | Uploads fail until it restarts, and nothing committed is lost | `restart: unless-stopped` | Reasoned |

## RabbitMQ

| Failure | Behaviour | Recovery | Evidence |
|---|---|---|---|
| Broker down | Uploads still work, dispatches wait in the outbox, and `/readyz` returns 503 | Automatic when it returns | Drill |
| Broker stopped with messages queued | Durable messages survive | Automatic | Drill |
| Broker container recreated | Messages survive on the `rabbitmq-data` volume | Automatic | Checked by hand |
| Broker volume deleted | Queued messages are lost, and the sweeper re-dispatches active documents | Automatic, after about two minutes | Test, by purging queues |
| Broker withholds a confirm | The relay times out and the row stays unpublished | Automatic | Test |
| Unroutable message | Publishing raises, and the row stays unpublished | Needs the queue to exist | Test |
| Network black hole or blocked write | Should be ended by the socket timeouts or the pass deadline | Unknown | **Not verified** |

## PostgreSQL

| Failure | Behaviour | Recovery | Evidence |
|---|---|---|---|
| Statement or lock wait over 30 seconds | Cancelled by the server | A stage retries it, and the API returns 500 | Test |
| Abandoned transaction | Ended after 2 minutes idle | Automatic | Reasoned |
| Down while idle | `/readyz` returns 503 | Automatic when it returns | Checked by hand |
| Down during processing | Stages should retry, then dead-letter, then be recovered by the sweeper | Should be automatic | **Not verified** |

## Bad input

| Failure | Behaviour | Evidence |
|---|---|---|
| Wrong file type, or oversized | Rejected with 415 or 413 before a row exists | Test |
| Corrupt, encrypted, empty or no-text PDF | Goes to review with a specific reason, and is never classified | Test |
| Hostile PDF built to exhaust memory or CPU | Should be stopped by pypdf's limits, the page and text caps, and the stage time limit | **Not verified** |
| Characters Postgres rejects | Removed during extraction | Test |
| Malformed or undecodable message | Dead-lettered, recorded in the logs, and other work continues | Drill, test |
| Bug or unexpected error in a stage | `FAILED` with `UNEXPECTED_ERROR`, one dead letter, no retries | Test |

## Filing

| Failure | Behaviour | Evidence |
|---|---|---|
| Timeout before the work | Retried, and no record exists until it succeeds | Test |
| Timeout after the work | Retried with the same key, so there is no second record | Drill, test |
| Temporary failures that clear | Resumes at the first incomplete step | Drill, test |
| Failures that don't clear | `FAILED` with `RETRIES_EXHAUSTED`, and one dead letter | Test |
| Rejected by the system | `FAILED` with `FILING_REJECTED`, with no retries | Test |
| Filing message lost | The sweeper re-dispatches `READY_TO_FILE` documents | Test |

## Two people or messages at once

| Failure | Behaviour | Evidence |
|---|---|---|
| Same message delivered repeatedly | One outcome | Drill, test |
| Two workers on one document | The row lock serializes them, and the second does nothing | Test |
| Two reviewers on one document | One succeeds and the other gets 409 | Test, and checked in the browser |
| Identical uploads at the same time | One document and one workflow | Test |

## What recovery can't do yet

- `FAILED` documents stay failed, because no route retries them.
- Dead-lettered messages are not replayed.
- During a long worker outage the sweeper keeps adding duplicates. They do nothing, but the queue
  grows until workers return.
