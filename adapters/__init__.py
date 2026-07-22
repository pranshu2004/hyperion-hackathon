"""
adapters/ — external observability-source adapters.

Each subpackage (e.g. adapters/signoz/) implements TraceSourceAdapter and/or
MetricSourceAdapter from adapters/base.py, translating a vendor's API into
Hyperion's canonical core.span.Span / core.metric.Metric types.

This package has no dependency on reasoning/, context/, output/, or
dashboard/ — and nothing in those packages depends on this one. Adapters are
purely an alternate front door into the same ingestion contract the
simulator-based ingesters already satisfy.
"""
