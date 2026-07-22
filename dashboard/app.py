"""
FastAPI dashboard application.

Serves RCA results via a REST API and receives analyst feedback.
All API responses use the serialized dict from output/schema.py.
Feedback submitted through the dashboard is written back to the stored result.
"""

from __future__ import annotations

import logging
import time
import os
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ingestion.trace_ingester import ingest_batch as ingest_traces
from ingestion.metric_ingester import ingest_batch as ingest_metrics
from ingestion.change_ingester import ingest_batch as ingest_changes
from context.graph_builder import build as build_graph
from context.baseline_calculator import compute_baselines, get_anomalous_nodes
from output.schema import from_feedback
import reasoning.engine as reasoning_engine
from ingestion.simulator.failure_injector import run_scenario, get_scenario
from ingestion.simulator.topology import get_nodes as get_topology_nodes
from core.nodes import ServiceNode

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Hyperion RCA API",
    description="AI-powered Root Cause Analysis for microservices",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# GitHub webhook integration lives entirely in webhooks/github/ and only
# ever calls into ingestion/*; it is never imported by anything else in
# Hyperion. Wiring it in is guarded so a misconfigured or absent webhooks
# package can never prevent the rest of the dashboard from starting.
try:
    from webhooks.github.router import router as _github_webhook_router
    app.include_router(_github_webhook_router)
except Exception:
    logger.warning("GitHub webhook router not mounted (webhooks/github unavailable)", exc_info=True)

_STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

# In-memory result store — keyed by incident_id
_results: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------

class AnalyzeRequest(BaseModel):
    incident_id: str | None = None
    traces: list[dict] = []
    metrics: list[dict] = []
    deploy_events: list[dict] = []
    config_change_events: list[dict] = []
    feature_flag_events: list[dict] = []
    incident_start: str | None = None
    use_llm: bool = False


class FeedbackRequest(BaseModel):
    correct: bool | None = None
    notes: str | None = None


# ---------------------------------------------------------------------------
# HTML dashboard
# ---------------------------------------------------------------------------

_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Hyperion RCA</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0d1117; color: #c9d1d9; font-family: 'Courier New', monospace; padding: 2rem; }
  h1 { color: #58a6ff; font-size: 1.6rem; margin-bottom: 0.25rem; }
  .subtitle { color: #8b949e; font-size: 0.9rem; margin-bottom: 2rem; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 6px; padding: 1.5rem; margin-bottom: 1.5rem; }
  label { display: block; color: #8b949e; font-size: 0.85rem; margin-bottom: 0.4rem; }
  select {
    background: #0d1117; border: 1px solid #30363d; color: #c9d1d9;
    padding: 0.5rem 0.75rem; border-radius: 4px; font-family: inherit;
    font-size: 0.9rem; width: 100%; margin-bottom: 1rem;
  }
  .checkbox-row { display: flex; align-items: center; gap: 0.5rem; margin-bottom: 1rem; }
  input[type="checkbox"] { width: 16px; height: 16px; }
  button {
    background: #238636; color: #fff; border: none; padding: 0.6rem 1.4rem;
    border-radius: 4px; cursor: pointer; font-family: inherit; font-size: 0.9rem;
  }
  button:hover { background: #2ea043; }
  button:disabled { background: #21262d; color: #8b949e; cursor: not-allowed; }
  #status { color: #8b949e; font-size: 0.85rem; margin-top: 0.75rem; min-height: 1.2rem; }
  #results { display: none; }
  .badge { display: inline-block; padding: 0.2rem 0.6rem; border-radius: 12px; font-size: 0.8rem; font-weight: bold; margin-left: 0.5rem; }
  .badge-green { background: #1f6feb33; color: #3fb950; border: 1px solid #3fb95066; }
  .badge-yellow { background: #b0893133; color: #d29922; border: 1px solid #d2992266; }
  .badge-red { background: #58121266; color: #f85149; border: 1px solid #f8514966; }
  h2 { color: #58a6ff; font-size: 1.1rem; margin-bottom: 1rem; }
  .meta { color: #8b949e; font-size: 0.82rem; margin-bottom: 1rem; }
  .candidate { background: #0d1117; border: 1px solid #30363d; border-radius: 4px; padding: 1rem; margin-bottom: 0.75rem; }
  .candidate-title { font-weight: bold; color: #c9d1d9; margin-bottom: 0.5rem; }
  .evidence-item { font-size: 0.82rem; color: #8b949e; padding: 0.2rem 0 0.2rem 1rem; }
  .propagation { color: #58a6ff; font-size: 0.85rem; letter-spacing: 0.03em; }
  pre { background: #0d1117; border: 1px solid #30363d; border-radius: 4px; padding: 1rem; overflow: auto; font-size: 0.78rem; max-height: 400px; }
</style>
</head>
<body>
<h1>&#9889; Hyperion RCA</h1>
<p class="subtitle">AI-powered Root Cause Analysis for microservices</p>
<div class="card">
  <h2>Run Demo Scenario</h2>
  <label>Scenario</label>
  <select id="scenario">
    <option value="sc001">SC-001 — Hero deploy (fraud-service)</option>
    <option value="sc002">SC-002 — DB slow query (postgres-payments)</option>
  </select>
  <div class="checkbox-row">
    <input type="checkbox" id="use_llm">
    <label style="margin:0">Use LLM narrative (requires HYPERION_LLM_* env config)</label>
  </div>
  <button id="analyzeBtn" onclick="runDemo()">Analyze</button>
  <div id="status"></div>
</div>
<div class="card" id="results">
  <h2>Results <span id="resultBadge" class="badge"></span></h2>
  <div class="meta" id="resultMeta"></div>
  <div id="candidateList"></div>
  <div id="propagationSection"></div>
  <details style="margin-top:1rem">
    <summary style="cursor:pointer;color:#8b949e;font-size:0.85rem">Raw JSON</summary>
    <pre id="rawJson"></pre>
  </details>
</div>
<script>
async function runDemo() {
  const scenario = document.getElementById('scenario').value;
  const btn = document.getElementById('analyzeBtn');
  const status = document.getElementById('status');
  btn.disabled = true;
  status.textContent = 'Running analysis (~10s)...';
  document.getElementById('results').style.display = 'none';
  try {
    const useLlm = document.getElementById('use_llm').checked;
    const r = await fetch(
        '/demo/' + scenario + '?use_llm=' + useLlm,
        { method: 'POST' }
    );
    if (!r.ok) { throw new Error('HTTP ' + r.status + ': ' + await r.text()); }
    const data = await r.json();
    status.textContent = 'Done in ' + data.wall_time_ms.toFixed(0) + 'ms';
    renderResults(data);
  } catch(e) {
    status.textContent = 'Error: ' + e.message;
  } finally {
    btn.disabled = false;
  }
}
function renderResults(data) {
  const badge = document.getElementById('resultBadge');
  if (data.has_root_cause) {
    badge.textContent = 'ROOT CAUSE IDENTIFIED';
    badge.className = 'badge badge-green';
  } else if (data.root_causes.length > 0) {
    badge.textContent = 'TOP HYPOTHESES';
    badge.className = 'badge badge-yellow';
  } else {
    badge.textContent = 'NO ROOT CAUSE';
    badge.className = 'badge badge-red';
  }
  document.getElementById('resultMeta').textContent =
    'Incident: ' + data.incident_id + '  |  Confidence: ' + (data.confidence*100).toFixed(1) + '%  |  ' +
    'Iterations: ' + (data.iterations_run || data.reasoning_iterations || 0) + '  |  Spans: ' + data.span_count + '  |  Quality: ' + data.data_quality;
  const list = document.getElementById('candidateList');
  list.innerHTML = data.root_causes.map(c =>
    '<div class="candidate">' +
    '<div class="candidate-title">#' + c.rank + ' ' + c.node_id + ' [' + c.node_type + '] &mdash; ' + c.domain + ' &mdash; confidence: ' + ((c.domain_confidence || c.confidence || 0)*100).toFixed(1) + '%</div>' +
    c.evidence.map(e => '<div class="evidence-item">&#9679; ' + e.signal + ': ' + (e.finding || e.value || '') + '</div>').join('') +
    '</div>'
  ).join('');
  const propSection = document.getElementById('propagationSection');
  if (data.propagation_path && data.propagation_path.length > 0) {
    propSection.innerHTML = '<div style="margin-top:0.75rem"><label>Propagation path</label><div class="propagation">' + data.propagation_path.join(' &rarr; ') + '</div></div>';
  } else {
    propSection.innerHTML = '';
  }
  document.getElementById('rawJson').textContent = JSON.stringify(data, null, 2);
  document.getElementById('results').style.display = 'block';
}
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Helpers — serialize scenario change-event dataclasses to raw ingest dicts
# ---------------------------------------------------------------------------

def _build_raw_deploy(deploy) -> dict:
    return {
        "event_type":   "deploy",
        "event_id":     deploy.event_id,
        "service_id":   deploy.service_id,
        "timestamp":    deploy.timestamp.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "version":      deploy.version,
        "author":       deploy.author or "",
        "diff_summary": deploy.diff_summary or "",
        "deploy_scope": deploy.deploy_scope,
        "tags":         deploy.tags,
    }


def _build_raw_config_change(evt) -> dict:
    return {
        "event_type":  "config_change",
        "event_id":    evt.event_id,
        "service_id":  evt.service_id,
        "timestamp":   evt.timestamp.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "change_type": getattr(evt, "change_type", "update"),
        "key":         evt.key,
        "old_value":   evt.old_value,
        "new_value":   evt.new_value,
        "author":      evt.author or "",
        "tags":        evt.tags,
    }


def _build_raw_feature_flag(evt) -> dict:
    return {
        "event_type": "feature_flag",
        "event_id":   evt.event_id,
        "service_id": evt.service_id,
        "timestamp":  evt.timestamp.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "flag_key":   evt.flag_key,
        "old_value":  evt.old_value,
        "new_value":  evt.new_value,
        "author":     evt.author or "",
        "tags":       evt.tags,
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def root():
    index = _STATIC_DIR / "index.html"
    if index.exists():
        return HTMLResponse(content=index.read_text())
    return HTMLResponse(content=_HTML)


@app.get("/health")
async def health():
    return {"status": "ok", "version": "0.1.0"}


@app.post("/analyze")
async def analyze_incident(request: AnalyzeRequest):
    """
    Run full RCA pipeline on provided telemetry data.
    Ingests raw traces, metrics, and change events; runs the reasoning engine;
    stores and returns the serialized RCAResult.
    """
    try:
        from core.change_event import DeployEvent, ConfigChangeEvent, FeatureFlagChangeEvent

        incident_id = request.incident_id or str(uuid.uuid4())[:8]

        spans = ingest_traces(request.traces)
        metrics_normalized = ingest_metrics(request.metrics)

        raw_changes = (
            request.deploy_events
            + request.config_change_events
            + request.feature_flag_events
        )
        changes = ingest_changes(raw_changes)

        if request.incident_start:
            incident_start = datetime.fromisoformat(request.incident_start)
        elif metrics_normalized:
            max_ts = max(m.timestamp for m in metrics_normalized)
            incident_start = max_ts - timedelta(minutes=5)
        else:
            incident_start = datetime.now(timezone.utc) - timedelta(minutes=5)

        deploy_evts = [e for e in changes if isinstance(e, DeployEvent)]
        config_evts = [e for e in changes if isinstance(e, ConfigChangeEvent)]
        flag_evts = [e for e in changes if isinstance(e, FeatureFlagChangeEvent)]

        graph = build_graph(
            spans=spans,
            deploy_events=deploy_evts,
            config_change_events=config_evts,
            feature_flag_events=flag_evts,
            seed_topology=False,
        )

        baselines = compute_baselines(metrics_normalized, incident_start)
        anomalous_nodes = get_anomalous_nodes(metrics_normalized, baselines, incident_start)

        if request.use_llm:
            from openai import OpenAI
            llm_client = OpenAI(
                base_url=os.environ.get("HYPERION_LLM_BASE_URL", "http://localhost:11434/v1"),
                api_key=os.environ.get("HYPERION_LLM_API_KEY", "ollama"),
                default_query={"api-version": "preview"},
            )
        else:
            llm_client = None

        rca = reasoning_engine.analyze(
            graph=graph,
            spans=spans,
            metrics=metrics_normalized,
            baselines=baselines,
            anomalous_nodes=anomalous_nodes,
            node_timelines={},
            incident_start=incident_start,
            incident_id=incident_id,
            code_change_event=None,
            llm_client=llm_client,
        )

        serialized = _serialize_result(rca, spans)
        _results[incident_id] = serialized
        return serialized

    except Exception as exc:
        logger.error("analyze_incident failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/results/{incident_id}")
async def get_result(incident_id: str):
    """Retrieve a stored RCA result by incident_id."""
    if incident_id not in _results:
        raise HTTPException(status_code=404, detail=f"Incident '{incident_id}' not found")
    return _results[incident_id]


@app.post("/results/{incident_id}/feedback")
async def submit_feedback(incident_id: str, request: FeedbackRequest):
    """Submit human feedback on an RCA result."""
    if incident_id not in _results:
        raise HTTPException(status_code=404, detail=f"Incident '{incident_id}' not found")
    fb = from_feedback(incident_id, request.correct, request.notes)
    _results[incident_id]["feedback"] = fb["feedback"]
    return {"status": "ok", "incident_id": incident_id}


@app.get("/results")
async def list_results():
    """
    List all stored RCA results (summary only), sorted by analyzed_at descending.
    """
    summaries = []
    for r in _results.values():
        top = r["root_causes"][0] if r.get("root_causes") else None
        summaries.append({
            "incident_id":    r["incident_id"],
            "analyzed_at":    r.get("analyzed_at", ""),
            "has_root_cause": r.get("has_root_cause", False),
            "confidence":     r.get("confidence", 0.0),
            "top_candidate":  top["node_id"] if top else None,
            "domain":         top["domain"] if top else None,
        })
    summaries.sort(key=lambda x: x["analyzed_at"], reverse=True)
    return summaries


# ---------------------------------------------------------------------------
# Demo endpoints
# ---------------------------------------------------------------------------

def _serialize_result(rca, spans) -> dict:
    """Serialize a reasoning RCAResult to a JSON-serializable response dict."""
    top = rca.root_causes[0] if rca.root_causes else None

    all_candidates = [
        {
            "rank": c.rank,
            "node_id": c.node_id,
            "node_type": c.node_type.value,
            "domain": c.domain.value,
            "failure_type": c.failure_type.value,
            "domain_confidence": round(c.domain_confidence, 4),
            "causal_confidence": round(c.causal_confidence, 4),
            "confidence": round(c.domain_confidence, 4),  # backward-compat alias
            "iteration_found": c.iteration_found,
            "evidence": [
                {
                    "signal": e.signal,
                    "finding": e.finding,
                    "value": e.finding,  # backward-compat alias
                    "strength": e.strength.value,
                }
                for e in c.evidence
            ],
        }
        for c in rca.all_candidates
    ]

    return {
        "incident_id": rca.incident_id,
        "analyzed_at": rca.analyzed_at.isoformat(),
        "verdict": rca.verdict.value,
        "has_root_cause": rca.verdict.value == "root_cause_found",
        "confidence": top.domain_confidence if top else 0.0,
        "root_cause_node": top.node_id if top else None,
        "root_cause_domain": top.domain.value if top else None,
        "root_causes": all_candidates[:3],
        "all_candidates": all_candidates,
        "narrative": rca.narrative,
        "fix_suggestion": rca.fix_suggestion,
        "iterations_run": rca.iterations_run,
        "reasoning_iterations": rca.iterations_run,  # backward-compat alias
        "data_quality": rca.data_quality.value,
        "span_count": len(spans),
        "anomalous_node_count": 0,
        "propagation_path": [],
        "pipeline": "reasoning",
    }


def _apply_topology_service_config(graph) -> None:
    """
    Backfill code_access_enabled/repo_url onto span-discovered ServiceNodes
    from the reference topology (demo-only — real /analyze graphs have no
    matching topology to draw from).
    """
    for node_id, topo_node in get_topology_nodes().items():
        if not isinstance(topo_node, ServiceNode) or node_id not in graph.nodes:
            continue
        node = graph.nodes[node_id]["data"]
        if isinstance(node, ServiceNode):
            node.code_access_enabled = topo_node.code_access_enabled
            node.repo_url = topo_node.repo_url


async def _run_demo(scenario_name: str, use_llm: bool = False) -> dict:
    """Run a named simulator scenario through the reasoning pipeline."""
    from core.change_event import DeployEvent, ConfigChangeEvent, FeatureFlagChangeEvent

    t_start = time.monotonic()

    base_time = datetime.now(timezone.utc)
    scenario = get_scenario(scenario_name, base_time=base_time)
    result = run_scenario(scenario, base_time=base_time)

    incident_start = result["incident_start"]

    # Ingest traces and metrics
    spans = ingest_traces(result["traces"])
    metrics_raw = ingest_metrics(result["metrics"])

    # Ingest change events
    raw_deploy = [_build_raw_deploy(d) for d in result["deploy_events"]]
    raw_config = [_build_raw_config_change(c) for c in result.get("config_change_events", [])]
    raw_flags = [_build_raw_feature_flag(f) for f in result.get("feature_flag_events", [])]

    raw_changes = raw_deploy + raw_config + raw_flags
    changes = ingest_changes(raw_changes)

    deploy_evts = [e for e in changes if isinstance(e, DeployEvent)]
    config_evts = [e for e in changes if isinstance(e, ConfigChangeEvent)]
    flag_evts = [e for e in changes if isinstance(e, FeatureFlagChangeEvent)]

    # Build graph from spans only — the reference topology is never seeded
    # into the graph itself (that would leak demo-only nodes into what's
    # meant to mirror a real /analyze graph). code_access_enabled/repo_url
    # aren't derivable from spans, so backfill them from the topology
    # definitions onto matching discovered ServiceNodes below.
    graph = build_graph(
        spans=spans,
        deploy_events=deploy_evts,
        config_change_events=config_evts,
        feature_flag_events=flag_evts,
        seed_topology=False,
    )
    _apply_topology_service_config(graph)

    # Compute baselines and anomalous nodes — required by reasoning
    baselines = compute_baselines(metrics_raw, incident_start)
    anomalous_nodes = get_anomalous_nodes(metrics_raw, baselines, incident_start)

    # Run reasoning pipeline
    incident_id = (
        f"v2-{scenario_name.lower().replace(' ', '-')}-"
        f"{datetime.now(timezone.utc).strftime('%H%M%S')}"
    )

    if use_llm:
        from openai import OpenAI
        llm_client = OpenAI(
            base_url=os.environ.get("HYPERION_LLM_BASE_URL", "http://localhost:11434/v1"),
            api_key=os.environ.get("HYPERION_LLM_API_KEY", "ollama"),
            default_query={"api-version": "preview"},
        )
    else:
        llm_client = None

    rca = reasoning_engine.analyze(
        graph=graph,
        spans=spans,
        metrics=metrics_raw,
        baselines=baselines,
        anomalous_nodes=anomalous_nodes,
        node_timelines={},
        incident_start=incident_start,
        incident_id=incident_id,
        code_change_event=result.get("code_change_event"),
        llm_client=llm_client,
    )

    wall_time_ms = (time.monotonic() - t_start) * 1000.0
    serialized = _serialize_result(rca, spans)
    serialized["wall_time_ms"] = wall_time_ms
    serialized["anomalous_node_count"] = len(anomalous_nodes)
    return serialized


@app.post("/demo/sc001")
async def demo_sc001(use_llm: bool = False):
    """Run SC-001 hero scenario (fraud-service bad deploy) through the reasoning pipeline."""
    try:
        return await _run_demo("SC-001-hero-deploy-fraud-service", use_llm=use_llm)
    except Exception as exc:
        logger.error("demo_sc001 failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/demo/sc002")
async def demo_sc002(use_llm: bool = False):
    """Run SC-002 database slow-query scenario (postgres-payments) through the reasoning pipeline."""
    try:
        return await _run_demo("SC-002-db-slow-query-postgres-payments", use_llm=use_llm)
    except Exception as exc:
        logger.error("demo_sc002 failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/demo/sc003")
async def demo_sc003(use_llm: bool = False):
    """Run SC-003 external dependency scenario (stripe-api 503s) through the reasoning pipeline."""
    try:
        return await _run_demo("SC-003-external-dep-stripe-api-503", use_llm=use_llm)
    except Exception as exc:
        logger.error("demo_sc003 failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/demo/sc004")
async def demo_sc004(use_llm: bool = False):
    """Run SC-004 configuration scenario (feature flag on fraud-service) through the reasoning pipeline."""
    try:
        return await _run_demo("SC-004-config-feature-flag-fraud-scoring", use_llm=use_llm)
    except Exception as exc:
        logger.error("demo_sc004 failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=True)
