# Hyperion, Causal Root Cause Analysis for Microservices

**Hyperion identifies *why* a distributed system failed, not just which service looks anomalous.**

Most RCA and observability tools (Datadog Watchdog, Dynatrace Davis AI, etc.) surface correlated anomalies across a service topology. Hyperion goes further: it runs **causal inference** over the service graph to distinguish the actual root cause from services that merely look anomalous because they're downstream of the real failure.

---

## The Core Idea

For every candidate node in an incident, Hyperion asks two questions:

1. **Forward simulation**, "If this node had failed, what impact should we observe across the graph?" (predicted vs. observed impact, fit score)
2. **Counterfactual**, "If this node had *not* failed, would the incident still have occurred?" (causal necessity, not correlation)

The two scores combine into a **causal confidence** used to prioritize investigation order, separating root cause from victim before any deep domain analysis even runs.

This is abductive causal reasoning over a service dependency graph, not threshold alerting and not pure correlation.

---

## How It Works

```
Ingestion (traces, metrics, deploys, code diffs)
        |
        v
Graph Builder --> typed service DAG + rolling baselines
        |
        v
+----------------------------------------------+
|              Reasoning Loop                  |
|                                              |
|  Localizer      --> wide-net candidate set   |
|       |                                      |
|  Causal Model   --> forward sim + counter-   |
|       |             factual --> ranked by    |
|       |             causal confidence        |
|       |                                      |
|  Domain RCA     --> deep investigation per   |
|       |             candidate (code diffs,   |
|       |             SQL, config changes, LLM)|
|       |                                      |
|  Scorer         --> final confidence [0,1]   |
|                                              |
|  Loop terminates when no new investigation   |
|  hints are surfaced or iteration cap hit     |
+----------------------------------------------+
        |
        v
Structured RCA output + plain-language narrative
```

**Domain-specific investigation** runs on each top-ranked candidate depending on node type:

| Domain | Node Type | What it checks |
|---|---|---|
| Application / Code | Service | Exception stack traces matched against deploy diffs, function-level attribution |
| Configuration | Service | Feature flag / config changes, temporal correlation to first error |
| Database | Database | Slow query detection, `db.statement` parsing, connection exhaustion patterns |
| Dependency | External API | HTTP error code patterns (503/429/timeout), caller-scope blast radius |

---

## Example: Root Cause Found

> `fraud-service` version `v1.8.1` was deployed at 14:23. The deploy removed a null check on `user_id` in `validate_transaction()`. At 14:31, `fraud-service` began throwing `NullPointerException` at line 47 on requests where `user_id` was absent, causing fraud checks to fail for 34% of payment requests. Errors propagated upstream through `payment-service` and `checkout-service`, resulting in checkout failures for end users.

Hyperion produces this by matching a deterministic stack-trace frame against the exact line changed in the deploy diff, corroborated by an LLM read of the old/new code, a "smoking gun" that overrides the causal-confidence prior entirely.

---

## Demo Scenarios

The repo ships with a 20-node simulated fintech/e-commerce topology and four scripted failure scenarios so the full pipeline can be exercised end-to-end without any live infrastructure:

| Scenario | Domain | Failure | Verdict |
|---|---|---|---|
| SC-001 | Application code | Bad deploy removes a null check → cascading timeouts | Root cause found, confidence 0.95 |
| SC-002 | Database | Slow query on `postgres-payments` | Root cause found, confidence 0.95 |
| SC-003 | Dependency | `stripe-api` returning 503s |, |
| SC-004 | Configuration | Feature flag flip causes degraded behavior | Root cause found, confidence 0.75 |

---

## Quickstart

```bash
git clone <this-repo>
cd hyperion
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in your values (optional — the app runs the full deterministic RCA pipeline without any LLM configured):

```bash
cp .env.example .env
```

**Important:** the `.env` file must be sourced into the shell before starting the server — the app does not auto-load it:

```bash
source .env && python -m uvicorn dashboard.app:app --reload
```

Then open `http://localhost:8000` in a browser.

> **Note:** the dashboard opens on the deterministic-only view by default. For the full experience — LLM-generated narrative, root-cause explanation, and fix suggestion — click **back to all incidents**, then open **"Fraud Service NullPointerException cascade – v2 pipeline"**. That's the scenario that exercises the complete pipeline end-to-end (causal ranking → code-diff attribution → LLM narrative).

Or hit the demo endpoints directly:

```
GET /demo/sc001                # deterministic evidence only
GET /demo/sc001?use_llm=true   # + LLM narrative and fix suggestion
GET /demo/sc002?use_llm=true
GET /demo/sc003?use_llm=true
GET /demo/sc004?use_llm=true
```

### LLM configuration

Hyperion is built and tuned against **gpt-5-mini**. This is a hard requirement, not a preference — the LLM calls use `max_completion_tokens` (not `max_tokens`) and deliberately do not set `temperature`, because that's what the gpt-5 reasoning-model family requires. Swapping to a different model family requires code changes in the client construction, not just an env var change.

Hyperion degrades gracefully with no LLM configured — deterministic evidence and scoring still run; only narrative generation and diff/SQL interpretation are skipped, and the demo falls back to a local default (`qwen2.5-coder:7b`) if `.env` isn't sourced or is missing.

## Tech Stack

Python 3.11+ · NetworkX (graph reasoning) · FastAPI · tree-sitter (AST-level code diff parsing) · OpenAI-compatible LLM client (gpt-5-mini) · scipy

---

## What's Next

- Production ingestion adapters for real observability backends (OTel Collector pipeline, Prometheus, vendor webhook translators)
- Domain-specific confidence scoring (current MVP uses a flat evidence-weight sum; per-domain ceiling tiers are the next step)
- Multi-factor incident detection, explaining-away logic when two candidates each have strong but independent evidence
- Broader stack-trace language coverage (currently Python, Java/JVM, Node/V8)

---

## Why Causal, Not Correlation

Correlation-based RCA tells you *what else was anomalous when the incident happened*. It can't distinguish a root cause from its victims, everything downstream of a failure looks anomalous too. Hyperion's counterfactual test asks the one question that actually separates cause from symptom: *would this incident have happened without this specific failure?* That's the question on-call engineers actually need answered at 2 AM.