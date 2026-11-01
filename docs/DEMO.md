# Seven-minute demo

Run this once before presenting. You need a clean stack, and step 1 prints the API key only the
first time.

```bash
docker compose down -v
```

## 1. The pipeline (90 seconds)

```bash
make demo
```

This starts the stack, checks health, and uploads six synthetic documents plus one repeat. Expect:

| Sample | Result | Why |
|---|---|---|
| Number and client agree | `FILED` | Clear match |
| Client has two matters | `NEEDS_REVIEW`, `AMBIGUOUS_MATCH` | A name alone can't decide between them |
| Two matters named | `NEEDS_REVIEW`, `CONFLICTING_MARKERS` | The document contradicts itself |
| No known matter | `UNMATCHED`, `NO_CANDIDATE` | Nothing plausible |
| Unclassifiable | `NEEDS_REVIEW`, `LOW_CONFIDENCE` | No evidence of a document type |
| Corrupt PDF | `NEEDS_REVIEW`, `CORRUPT_DOCUMENT` | Unreadable, so never classified |
| Same file again | `duplicate` | No second workflow |

Copy the key from the line starting `Demo API key, shown once`.

## 2. A person decides (2 minutes)

In a second terminal:

```bash
make web
```

1. Open <http://127.0.0.1:5173> and enter the key.
2. Open the `AMBIGUOUS_MATCH` document. Two candidates tie, each with an explanation.
3. Choose **Assign this matter** on either one.
4. The audit trail appears, from `CREATED` to `REVIEWER_ASSIGNED`.

Before you assign it, you can list the queue to get its document id. Replace `KEY` with your key:

```bash
curl -s -H "X-API-Key: KEY" http://127.0.0.1:8000/reviews
```

Afterwards, show that it was filed, replacing `KEY` and `ID`:

```bash
curl -s -H "X-API-Key: KEY" http://127.0.0.1:8000/documents/ID/events
```

The trail now ends with three `FILING_STEP_COMPLETED` events and `FILED`.

## 3. Failures (2 minutes)

Stop the demo stack, then run the drills, which take about two minutes:

```bash
make down
```

```bash
make drills
```

Point out that 100 documents survive a worker SIGKILL, and that uploads keep working with
RabbitMQ stopped. Then open [DRILLS.md](DRILLS.md) to show the recorded evidence, such as the
number of documents in flight at the kill.

## 4. The measured trade-off (90 seconds)

Open [EVALUATION.md](EVALUATION.md) and show:

- **Automation against error.** Lowering the threshold to 0.60 raises straight-through
  processing and produces wrong filings.
- **Calibration.** It is not clean, and that is reported as measured.
- **Limitations.** The data is synthetic, so real accuracy is unknown.

## If something breaks

Say what you expected to see, then look at the logs:

```bash
docker compose logs --tail=50 worker relay
```

Check whether a dependency is down:

```bash
curl -i http://127.0.0.1:8000/readyz
```
