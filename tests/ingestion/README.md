# Ingestion Layer Test Suite

This folder contains test cases for Hyperion's **ingestion layer**:
`ingestion/metric_ingester.py`, `ingestion/trace_ingester.py`, and
`ingestion/change_ingester.py`.

These three modules are the entry point for all telemetry into Hyperion —
metrics scrapes, OTel traces, and deploy/config/feature-flag webhooks. If
ingestion silently drops or mis-normalizes data, the reasoning engine
downstream gets a corrupted picture of "what happened," so this layer is
tested for **resilience under realistic production garbage**, not just
happy-path correctness.

`ingestion/code_ingester.py` (the tree-sitter based diff parser) is **not**
covered here — it has different dependencies (tree-sitter grammars) and a
different failure surface (parsing source code diffs vs. parsing
telemetry JSON). It's a good candidate for its own test module later.

## Running the tests

```bash
# from the repo root
pip install -r requirements.txt
python3 -m pytest tests/ingestion/ -v
```

Run a single module:

```bash
python3 -m pytest tests/ingestion/test_metric_ingester.py -v
python3 -m pytest tests/ingestion/test_trace_ingester.py -v
python3 -m pytest tests/ingestion/test_change_ingester.py -v
```

## Structure

```
tests/ingestion/
├── README.md
├── __init__.py
├── test_metric_ingester.py
├── test_trace_ingester.py
├── test_change_ingester.py
└── fixtures/
    ├── metrics_valid_batch.json
    ├── metrics_partial_corruption_batch.json
    ├── metrics_truncated_write.json
    ├── trace_cascading_db_pool_exhaustion.json
    ├── trace_malformed_collector_batch.json
    ├── trace_invalid_structure.json
    ├── changes_canary_rollout_batch.json
    └── changes_malformed_webhook_batch.json
```

## What each test module covers

### `test_metric_ingester.py`

| Scenario | What it simulates |
|---|---|
| `metrics_valid_batch.json` | A healthy scrape: latency, error rate, and DB saturation for a single service. Baseline happy-path. |
| `metrics_partial_corruption_batch.json` | A single scrape batch where one exporter is healthy and others are emitting garbage: `value: "NaN"` from a broken instrumentation library, an unrecognized `metric_type` from a newer exporter version (version skew), an empty `node_id`, a negative value from a counter reset, a non-ISO timestamp format from a different collector version, `window_seconds: 0` and a missing `window_seconds` field from misconfigured scrape intervals, and an unrecognized `node_type` (`vm_instance`). |
| `metrics_truncated_write.json` | A metrics exporter that crashed mid-write, leaving an invalid/truncated JSON file on disk for `ingest_from_file` to pick up on its next poll. |

**NaN and non-finite values are now rejected.** `ingest()` uses
`math.isfinite()` to catch NaN, +inf, and -inf after the `float()` cast
(since `nan < 0` is `False`, the old negative-value guard missed them).
`test_nan_value_is_rejected` pins this behavior: the NaN string record
(fixture index 1) is dropped, leaving 2 survivors (checkout-service and
billing-queue). `validate()` also now rejects boolean `value` and
`window_seconds` fields — `isinstance(True, int)` is `True` in Python, so
explicit bool checks are needed before the numeric type checks. Both gaps
are covered by `TestBoolCoercionInIngest` and `TestValidateFunction`.

### `test_trace_ingester.py`

| Scenario | What it simulates |
|---|---|
| `trace_cascading_db_pool_exhaustion.json` | **The canonical RCA scenario.** `checkout-service`'s `POST /checkout` returns `504 Gateway Timeout` because its call into `payments-service` blocks on a Postgres query that times out with `ConnectionPoolTimeout: pool exhausted (40/40 in use)`. This is a realistic two-service cascading failure — the kind of trace Hyperion's reasoning engine needs to localize back to the DB pool, not the symptom in `checkout-service`. Tests verify span parent/child linkage across service boundaries, exception-event normalization, duration computation, and resource-attribute extraction (`service.version`, `deployment.environment`). |
| `trace_malformed_collector_batch.json` | A batch of two traces from a degraded OTel collector: a span with an **empty `spanId`**, a span **missing `endTimeUnixNano`** (an in-flight span flushed on crash before it completed), a span with an **unrecognized `kind` integer** (e.g. a newer OTel SDK using a kind value this ingester doesn't know about — must default to `INTERNAL`), an **orphaned span with an empty `traceId`**, and a `resourceSpans` entry with **no resource attributes at all** (sidecar misconfiguration — service ID must fall back to `"unknown-service"`). |
| `trace_invalid_structure.json` | A non-OTel JSON payload (e.g. a generic `{"status": "ok"}` ack) hitting the trace ingestion endpoint — simulates a misrouted/misconfigured agent sending the wrong payload shape. |

### `test_change_ingester.py`

| Scenario | What it simulates |
|---|---|
| `changes_canary_rollout_batch.json` | A correlated sequence of change events around an incident window: a **canary deploy** to `checkout-service` that reduces a DB pool timeout from 8000ms→5000ms, a **config change** on `payments-service` that shrinks `DB_POOL_MAX_CONNECTIONS` from 60→40, and a **feature-flag flip** enabling a new payment retry path. This is the change-event context an analyst would expect Hyperion to surface alongside the trace failure in `trace_cascading_db_pool_exhaustion.json` — together they tell the real incident story (pool size was cut right before the pool got exhausted). |
| `changes_malformed_webhook_batch.json` | A webhook relay forwarding a mixed batch: a healthy deploy (control), a deploy with an **empty `service_id`**, a deploy **missing `version`**, a deploy with a **non-ISO timestamp** (`"02/11/2025 14:23:00"`), a deploy whose `deploy_scope` is a **string instead of a list**, an **unmodeled `infra_scaling` event type** (common when autoscaling events get forwarded to the same webhook before Hyperion has a parser for them), a `config_change` **missing its required `key`**, an event with **no `event_type`** at all, and a **raw string** (non-dict) entry simulating a webhook relay bug. Only the one fully-valid deploy should survive ingestion.

## Adding new scenarios

1. Drop a new fixture JSON into `fixtures/`.
2. Add a test class/method in the relevant `test_*_ingester.py` describing
   the **real-world failure mode** the fixture represents (not just "bad
   input #7") — the docstrings are meant to be readable by anyone debugging
   a production incident later.
3. Run `python3 -m pytest tests/ingestion/ -v` and confirm it passes (or, if
   you're documenting a known bug like the NaN case above, that it passes
   *and* clearly explains why, with a recommended fix).
