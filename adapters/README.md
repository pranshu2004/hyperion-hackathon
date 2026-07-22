# adapters/

External observability-source adapters. Each one translates a vendor's API
into Hyperion's own canonical types (`core.span.Span`, `core.metric.Metric`)
so the reasoning engine never has to know or care where the data came from.

```
adapters/
  base.py              TraceSourceAdapter / MetricSourceAdapter contracts
  signoz/
    config.py          connection config + metric-name/node-type mapping tables
    client.py           thin HTTP client for SigNoz's /api/v5/query_range
    trace_adapter.py    SigNoz raw span rows  -> core.span.Span
    metric_adapter.py   SigNoz time-series     -> core.metric.Metric
                         (routes through ingestion.metric_ingester for validation)
```

## Design principles

1. **One-way dependency.** `adapters/` depends on `core/` and (for metrics)
   `ingestion/`. Nothing in `reasoning/`, `context/`, `output/`, or
   `dashboard/` imports from `adapters/`. Swapping or adding a source never
   touches the reasoning engine.
2. **Reuse validation, don't duplicate it.** The metric adapter converts
   SigNoz responses into the same flat dict shape your simulator already
   produces, then calls the existing, already-hardened
   `ingestion.metric_ingester.ingest_batch()`. A malformed SigNoz metric is
   rejected by the exact same guards (NaN, negative values, bad window,
   bool-vs-int) as a malformed simulator metric — one validation path, two
   sources.
3. **Never raise on bad upstream data.** Same contract as your existing
   ingesters: log a warning and skip, don't crash a batch over one bad row.
4. **Config, not code, for mapping.** Which SigNoz metric names map to which
   `MetricType`, and which resource attribute identifies a node, live in
   `SignozMappingConfig` — edit that, not the adapter logic, when your
   metric names differ.

## Adding a new source later (Datadog, Prometheus, etc.)

Implement `TraceSourceAdapter` and/or `MetricSourceAdapter` from `base.py` in
a new `adapters/<vendor>/` package, following the same shape. The rest of
the codebase doesn't change.

## Usage

```python
from datetime import datetime, timedelta, timezone
from adapters.signoz import (
    SignozClient, SignozConnectionConfig, SignozMappingConfig,
    SignozTraceAdapter, SignozMetricAdapter,
)

conn = SignozConnectionConfig.from_env()  # SIGNOZ_BASE_URL, SIGNOZ_API_KEY
end = datetime.now(timezone.utc)
start = end - timedelta(minutes=15)

with SignozClient(conn) as client:
    spans = SignozTraceAdapter(client).fetch_spans(start, end, service_name="checkout-service")
    metrics = SignozMetricAdapter(client, SignozMappingConfig()).fetch_metrics(start, end)

# spans / metrics are now ordinary core.span.Span / core.metric.Metric objects —
# feed them into context/graph_builder.py, reasoning/engine.py, etc. exactly
# like simulator-sourced data.
```

## ⚠️ Before production use

I built the field mappings (`_ROW_FIELD_ALIASES` in `trace_adapter.py`,
`DEFAULT_METRIC_NAME_MAP` in `config.py`) against SigNoz's *documented*
`/api/v5/query_range` shape as of mid-2026. **Partially validated** against a
live instance (SigNoz v0.130.1, 2026-07): the response envelope is one level
deeper than originally assumed — `data.data.results[]`, not `data.results[]`
— and the metrics per-result field is `aggregations`, not `series`. Both are
now handled in `client.py` / `metric_adapter.py`. That instance had zero
ingested telemetry at verification time, so only the wrapper/envelope shape
was confirmed — the *leaf* shape (individual row fields in
`_ROW_FIELD_ALIASES`, and whether populated `aggregations` entries carry
`labels`/`values` as `metric_adapter.py` assumes) is still unverified.
Before wiring this into the real pipeline:

1. Once real telemetry is flowing, call `client.query_traces_raw(...)` and
   `client.query_metric_timeseries(...)` again and print the raw JSON with
   actual rows/aggregations present (not just the empty-result envelope).
2. Confirm the field names in `_ROW_FIELD_ALIASES` and the metric names in
   `DEFAULT_METRIC_NAME_MAP` actually match what you get back — your metric
   names depend entirely on what your OTel collector/instrumentation emits.
3. Only then point it at a real incident window.

This is flagged rather than silently assumed correct — better to catch a
field-name mismatch in a 5-minute dry run than in the middle of an incident.
