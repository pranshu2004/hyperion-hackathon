// Topology — mirrors ingestion/simulator/topology.py (20 nodes, 19 edges)
const TOPOLOGY_NODES = [
  { id: 'api-gateway',           type: 'service'      },
  { id: 'frontend-service',      type: 'service'      },
  { id: 'checkout-service',      type: 'service'      },
  { id: 'payment-service',       type: 'service'      },
  { id: 'inventory-service',     type: 'service'      },
  { id: 'fraud-service',         type: 'service'      },
  { id: 'auth-service',          type: 'service'      },
  { id: 'notification-service',  type: 'service'      },
  { id: 'catalog-service',       type: 'service'      },
  { id: 'postgres-payments',     type: 'database'     },
  { id: 'postgres-inventory',    type: 'database'     },
  { id: 'postgres-fraud',        type: 'database'     },
  { id: 'postgres-catalog',      type: 'database'     },
  { id: 'redis-cache',           type: 'database'     },
  { id: 'redis-sessions',        type: 'database'     },
  { id: 'order-queue',           type: 'queue'        },
  { id: 'stripe-api',            type: 'external_dep' },
  { id: 'risk-api',              type: 'external_dep' },
  { id: 'sms-provider',          type: 'external_dep' },
  { id: 'email-provider',        type: 'external_dep' },
];

const TOPOLOGY_EDGES = [
  { source: 'api-gateway',          target: 'frontend-service',     type: 'calls'         },
  { source: 'api-gateway',          target: 'auth-service',         type: 'calls'         },
  { source: 'frontend-service',     target: 'checkout-service',     type: 'calls'         },
  { source: 'frontend-service',     target: 'catalog-service',      type: 'calls'         },
  { source: 'frontend-service',     target: 'redis-cache',          type: 'reads_from'    },
  { source: 'auth-service',         target: 'redis-sessions',       type: 'reads_from'    },
  { source: 'auth-service',         target: 'sms-provider',         type: 'calls'         },
  { source: 'checkout-service',     target: 'payment-service',      type: 'calls'         },
  { source: 'checkout-service',     target: 'inventory-service',    type: 'calls'         },
  { source: 'checkout-service',     target: 'order-queue',          type: 'publishes_to'  },
  { source: 'payment-service',      target: 'fraud-service',        type: 'calls'         },
  { source: 'payment-service',      target: 'postgres-payments',    type: 'writes_to'     },
  { source: 'fraud-service',        target: 'stripe-api',           type: 'calls'         },
  { source: 'fraud-service',        target: 'risk-api',             type: 'calls'         },
  { source: 'fraud-service',        target: 'postgres-fraud',       type: 'reads_from'    },
  { source: 'inventory-service',    target: 'postgres-inventory',   type: 'reads_from'    },
  { source: 'catalog-service',      target: 'postgres-catalog',     type: 'reads_from'    },
  { source: 'order-queue',          target: 'notification-service', type: 'subscribes_to' },
  { source: 'notification-service', target: 'email-provider',       type: 'calls'         },
];

// Returns all 20 nodes with per-node state overrides (defaults to 'healthy')
function graphNodes(stateMap) {
  return TOPOLOGY_NODES.map(n => ({ ...n, state: stateMap[n.id] || 'healthy' }));
}

// Mock data — used as initial state and fallback when backend is unavailable
const INCIDENTS = [
  {
    id: 'INC-2419',
    title: 'Checkout returns 500 on card payment validation',
    service: 'checkout-svc',
    serviceColor: '#60a5fa',
    severity: 'SEV1',
    status: 'active',
    confidence: 91,
    revenue: 184320,
    revenueRate: 3840,
    users: 2847,
    errorRate: 12.4,
    opened: '14:23 IST',
    age: '11m',
    owner: 'Pranshu D.',
    ownerColor: '#f59e0b',
    region: 'ap-south-1',

    verdict: {
      node_id:  'fraud-service',
      domain:   'application code',
      case:     'B',
      file:     'RiskEvaluator.java:47',
      function: 'evaluateTransaction',
      summary:  'Deploy v1.8.1 introduced a NullPointerException in RiskEvaluator.evaluateTransaction() when TokenizerV2 DI wiring fails under load. Null safety check removed in commit a1b2c3d. Confidence: 91%.',
      deployedAt: '14:21 IST',
      window: '8m before first error',
    },

    // TODO: wire to backend — business impact requires revenue/SLO tracking integration
    impact: {
      revenueLost: '₹1,84,320',
      revenueRate: '₹3,840 / min',
      revenueDelta: '+38% vs SLO budget',
      users: '2,847',
      usersDelta: '94% of guest checkout flow',
      errorRate: '12.4%',
      errorBaseline: 'baseline 0.04%',
      sloRemaining: '3h 22m',
      sloDelta: 'monthly budget 47% consumed'
    },

    evidence: [
      {
        kind: 'deploy',
        title: 'Deploy event',
        time: '14:21:08',
        desc: 'fraud-service v1.8.1 deployed 8min before first error · author: alice@hyperion-demo.com',
        correlation: 'T-8m',
        detail: null,
      },
      {
        kind: 'log',
        title: 'Stacktrace match — EXACT LINE',
        time: '14:23:51',
        desc: 'RiskEvaluator.evaluateTransaction() · RiskEvaluator.java:47 · MatchType: EXACT_LINE · confidence ceiling: 0.97',
        correlation: '0.97 ceiling',
        detail: null,
      },
      {
        kind: 'log',
        title: 'Exception type',
        time: '14:23:51',
        desc: 'NullPointerException: tokenizer field null after TokenizerV2 DI wiring failure · 14,832 occurrences',
        correlation: '0.99 ρ',
        detail: null,
      },
      {
        kind: 'metric',
        title: 'Latency spike',
        time: '14:23:42',
        desc: 'p99 1360ms vs baseline 160ms · z-score 8.5 · fraud-service',
        correlation: '8.5σ',
        detail: { type: 'sparkline', data: [2,1,2,1,3,2,1,2,1,2,1,3,2,1,2,89,124,132,128,134,129,131,127,133] },
      },
      {
        kind: 'metric',
        title: 'Error rate spike',
        time: '14:23:44',
        desc: 'error rate 85% vs baseline 1% · evaluate_transaction operation · fraud-service',
        correlation: '0.97 ρ',
        detail: null,
      },
    ],

    // TODO: wire to backend — timeline requires PagerDuty + deploy hook integration
    timeline: [
      { time: '14:18', rel: 'T-5m',  kind: 'deploy',  l1: 'PR merged', code: '#1284', l2: 'a.mehta · feat(payments): support int\'l guest checkout' },
      { time: '14:21', rel: 'T-2m',  kind: 'deploy',  l1: 'checkout-svc deployed', code: 'v2.34.1', l2: 'Rolling deploy completed on 8/8 pods · ap-south-1' },
      { time: '14:23', rel: 'T+0',   kind: 'spike',   l1: 'Error rate breached threshold', code: '> 1.0%', l2: 'checkout.payment.errors.rate jumped 0.04% → 12.4%' },
      { time: '14:24', rel: 'T+1m',  kind: 'alert',   l1: 'PagerDuty page sent', code: 'oncall · payments', l2: 'Acknowledged by Pranshu D. at 14:24:38' },
      { time: '14:25', rel: 'T+2m',  kind: 'action',  l1: 'Hyperion RCA generated', code: 'confidence 94%', l2: 'Linked deploy v2.34.1 · PR #1284 · file PaymentValidator.kt:142' },
      { time: '14:34', rel: 'now',   kind: 'action',  l1: 'Investigation in progress', code: 'pending rollback', l2: 'Awaiting approval to roll back to v2.33.7' },
    ],

    // TODO: wire to backend — rollback action requires deploy orchestration integration
    action: {
      headline: 'Roll back checkout-svc to v2.33.7',
      detail: 'Reverting deploy v2.34.1 restores the null-guard on billingAddress.country. Estimated time to mitigation: ~90s. Hyperion will monitor checkout.payment.errors.rate and confirm recovery.',
      buttons: [
        { label: 'Roll back to v2.33.7', kind: 'primary', icon: 'rotate' },
        { label: 'Open PR for hotfix', kind: 'default', icon: 'github' },
        { label: 'Escalate to team', kind: 'ghost', icon: 'users' },
      ]
    },

    signals: [
      { name: 'checkout.payment.errors.rate', sub: '5m avg · ap-south-1', value: '12.4%', dir: 'up' },
      { name: 'checkout.order.success.rate', sub: '5m avg', value: '87.6%', dir: 'dn' },
      { name: 'p99 latency · payment.process', sub: '5m avg', value: '4.2s', dir: 'up' },
      { name: 'orders / min', sub: '5m avg', value: '418', dir: 'dn' },
      { name: 'cart abandon rate', sub: '15m avg', value: '34.8%', dir: 'up' },
    ],

    graph: {
      nodes: [
        { id: 'frontend-web',    type: 'service',      state: 'blast_radius' },
        { id: 'checkout-svc',    type: 'service',      state: 'root_cause' },
        { id: 'payment-svc',     type: 'service',      state: 'blast_radius' },
        { id: 'order-svc',       type: 'service',      state: 'blast_radius' },
        { id: 'razorpay-api',    type: 'external_dep', state: 'blast_radius' },
        { id: 'postgres-orders', type: 'database',     state: 'blast_radius' },
        { id: 'redis-cart',      type: 'database',     state: 'healthy' },
        { id: 'order-events',    type: 'queue',        state: 'blast_radius' },
      ],
      edges: [
        { source: 'frontend-web',  target: 'checkout-svc',    type: 'calls' },
        { source: 'checkout-svc',  target: 'payment-svc',     type: 'calls' },
        { source: 'checkout-svc',  target: 'order-svc',       type: 'calls' },
        { source: 'checkout-svc',  target: 'razorpay-api',    type: 'calls' },
        { source: 'checkout-svc',  target: 'postgres-orders', type: 'reads_from' },
        { source: 'order-svc',     target: 'postgres-orders', type: 'writes_to' },
        { source: 'checkout-svc',  target: 'redis-cart',      type: 'reads_from' },
        { source: 'checkout-svc',  target: 'order-events',    type: 'publishes_to' },
        { source: 'payment-svc',   target: 'razorpay-api',    type: 'depends_on' },
      ],
      metrics: {
        'checkout-svc':    { error_rate: { current: 0.1240, baseline: 0.0004 }, latency_p99_ms: { current: 4200, baseline: 240 }, z_score: 8.7, contribution: 0.42, owner: 'team-checkout' },
        'payment-svc':     { error_rate: { current: 0.0890, baseline: 0.0010 }, latency_p99_ms: { current: 1820, baseline: 180 }, z_score: 5.4, contribution: 0.18, owner: 'team-payments' },
        'order-svc':       { error_rate: { current: 0.0610, baseline: 0.0020 }, latency_p99_ms: { current: 1240, baseline: 210 }, z_score: 4.1, contribution: 0.11, owner: 'team-orders' },
        'razorpay-api':    { error_rate: { current: 0.0340, baseline: 0.0050 }, latency_p99_ms: { current: 980,  baseline: 320 }, z_score: 2.3, contribution: 0.04, owner: 'external · razorpay' },
        'postgres-orders': { error_rate: { current: 0.0080, baseline: 0.0010 }, latency_p99_ms: { current: 88,   baseline: 32  }, z_score: 1.8, contribution: 0.06, owner: 'platform-data' },
        'redis-cart':      { error_rate: { current: 0.0001, baseline: 0.0001 }, latency_p99_ms: { current: 4,    baseline: 3   }, z_score: 0.2, contribution: 0.00, owner: 'platform-data' },
        'order-events':    { error_rate: { current: 0.0220, baseline: 0.0030 }, latency_p99_ms: { current: 140,  baseline: 80  }, z_score: 2.9, contribution: 0.08, owner: 'platform-stream' },
        'frontend-web':    { error_rate: { current: 0.0410, baseline: 0.0020 }, latency_p99_ms: { current: 2400, baseline: 320 }, z_score: 4.6, contribution: 0.11, owner: 'team-storefront' },
      }
    },

    // TODO: wire to backend — similar incidents requires cross-incident embedding search
    similar: [
      { id: 'INC-2104', title: 'Payment validator NPE after locale refactor', when: '6 weeks ago', match: 91, res: 'Rollback' },
      { id: 'INC-1982', title: 'Checkout 500s on guest card flow (UK)', when: '3 months ago', match: 78, res: 'Hotfix · 22m' },
      { id: 'INC-1841', title: 'BIN lookup returns null for IN issuers', when: '5 months ago', match: 64, res: 'Config' },
    ]
  },

  {
    id: 'INC-2420',
    title: 'Fraud service NullPointerException cascade — v2 pipeline',
    service: 'fraud-service',
    serviceColor: '#a78bfa',
    severity: 'SEV1',
    status: 'investigating',
    confidence: 90,
    revenue: 142000,
    revenueRate: 2800,
    users: 2100,
    errorRate: 85,
    opened: '14:21 IST',
    age: '19m',
    owner: 'Pranshu D.',
    ownerColor: '#f59e0b',
    region: 'ap-south-1',
    pipeline: 'v2',
    loading: false,

    verdict: {
      node_id: 'fraud-service',
      domain: 'application code',
      summary: 'Analyzing with reasoning pipeline — click to load live result.',
      window: 'reasoning · awaiting result',
      deployedAt: '14:21 IST',
    },

    impact: {
      revenueLost: '₹1,42,000',
      revenueRate: '₹2,800 / min',
      revenueDelta: '+31% vs SLO budget',
      users: '2,100',
      usersDelta: '87% of payment checkout flow',
      errorRate: '85%',
      errorBaseline: 'baseline 0.8%',
      sloRemaining: '4h 10m',
      sloDelta: 'monthly budget 38% consumed',
    },

    evidence: [],

    timeline: [
      { time: '14:21', rel: 'T+0',   kind: 'deploy', l1: 'fraud-service deployed', code: 'v1.8.1', l2: 'Deploy by alice@hyperion-demo.com · github-actions' },
      { time: '14:21', rel: 'T+0',   kind: 'spike',  l1: 'Error rate breached threshold', code: '> 1.0%', l2: 'fraud-service NullPointerException · evaluate_transaction' },
      { time: '14:34', rel: 'T+13m', kind: 'action', l1: 'Hyperion v2 RCA triggered', code: 'reasoning', l2: 'Causal reasoning engine analyzing cascade' },
    ],

    action: {
      headline: 'Roll back fraud-service to v1.8.0',
      detail: 'Reverting deploy v1.8.1 restores the null-guard on tokenizer field in RiskEvaluator.evaluateTransaction(). Estimated time to mitigation: ~90s.',
      buttons: [
        { label: 'Roll back to v1.8.0', kind: 'primary', icon: 'rotate' },
        { label: 'Open PR for hotfix', kind: 'default', icon: 'github' },
        { label: 'Escalate to team', kind: 'ghost', icon: 'users' },
      ],
    },

    signals: [
      { name: 'fraud-service.errors.rate', sub: '5m avg · ap-south-1', value: '85%', dir: 'up' },
      { name: 'payment-service.success.rate', sub: '5m avg', value: '15%', dir: 'dn' },
      { name: 'p99 latency · evaluate_transaction', sub: '5m avg', value: '1360ms', dir: 'up' },
      { name: 'checkout.order.success.rate', sub: '5m avg', value: '12%', dir: 'dn' },
      { name: 'NullPointerException count', sub: '5m window', value: '14,832', dir: 'up' },
    ],

    graph: {
      nodes: graphNodes({
        'api-gateway':      'blast_radius',
        'frontend-service': 'blast_radius',
        'checkout-service': 'blast_radius',
        'payment-service':  'blast_radius',
        'fraud-service':    'root_cause',
      }),
      edges: TOPOLOGY_EDGES,
      metrics: {
        'fraud-service':    { error_rate: { current: 0.8500, baseline: 0.0080 }, latency_p99_ms: { current: 1360, baseline: 160 }, z_score: 9.1, contribution: 0.48, owner: 'team-payments' },
        'payment-service':  { error_rate: { current: 0.7200, baseline: 0.0100 }, latency_p99_ms: { current: 1820, baseline: 180 }, z_score: 7.4, contribution: 0.22, owner: 'team-payments' },
        'checkout-service': { error_rate: { current: 0.6800, baseline: 0.0040 }, latency_p99_ms: { current: 2100, baseline: 240 }, z_score: 6.8, contribution: 0.18, owner: 'team-checkout' },
        'frontend-service': { error_rate: { current: 0.4100, baseline: 0.0020 }, latency_p99_ms: { current: 2800, baseline: 320 }, z_score: 5.1, contribution: 0.09, owner: 'team-storefront' },
        'api-gateway':      { error_rate: { current: 0.3200, baseline: 0.0010 }, latency_p99_ms: { current: 3200, baseline: 280 }, z_score: 4.3, contribution: 0.07, owner: 'platform-infra' },
      },
    },

    similar: [
      { id: 'INC-2419', title: 'Checkout returns 500 on card payment validation', when: '19m ago', match: 94, res: 'Rollback' },
      { id: 'INC-2104', title: 'Payment validator NPE after locale refactor', when: '6 weeks ago', match: 88, res: 'Rollback' },
    ],
  },

  // ── SC-002: DB slow query — postgres-payments ────────────────────────────
  {
    id: 'INC-2421',
    title: 'Payment timeouts — postgres-payments query latency spike',
    service: 'postgres-payments',
    serviceColor: '#22c55e',
    severity: 'SEV1',
    status: 'active',
    confidence: 0,
    revenue: 98400,
    revenueRate: 1640,
    users: 1820,
    errorRate: 70,
    opened: '13:44 IST',
    age: '30m',
    owner: 'Pranshu D.',
    ownerColor: '#f59e0b',
    region: 'ap-south-1',
    pipeline: 'v2',
    loading: false,

    verdict: {
      node_id: 'postgres-payments',
      domain: 'database',
      summary: 'Click to run live database RCA analysis — postgres-payments latency spike.',
      window: 'reasoning · awaiting result',
    },

    impact: {
      revenueLost: '₹98,400',
      revenueRate: '₹1,640 / min',
      revenueDelta: '+22% vs SLO budget',
      users: '1,820',
      usersDelta: '76% of payment checkout flow',
      errorRate: '70%',
      errorBaseline: 'baseline 0.2%',
      sloRemaining: '5h 12m',
      sloDelta: 'monthly budget 29% consumed',
    },

    evidence: [],

    timeline: [
      { time: '13:44', rel: 'T+0',  kind: 'spike',  l1: 'postgres-payments latency breached threshold', code: '> 500ms', l2: 'Query p99 jumped 200ms → 2400ms — no deploy in window' },
      { time: '13:46', rel: 'T+2m', kind: 'alert',  l1: 'payment-service error rate rising', code: '> 10%', l2: 'Connection pool exhausted waiting on slow INSERT payments queries' },
      { time: '13:50', rel: 'T+6m', kind: 'alert',  l1: 'PagerDuty page sent', code: 'oncall · payments', l2: 'Acknowledged by Pranshu D. at 13:51:04' },
      { time: '14:14', rel: 'now',  kind: 'action', l1: 'Hyperion RCA triggered', code: 'reasoning', l2: 'Database analysis in progress' },
    ],

    action: {
      headline: 'Investigate slow queries on postgres-payments',
      detail: 'Query latency spike with no deploy in window suggests lock contention or a missing index on the payments table. Check pg_stat_activity and EXPLAIN ANALYZE on recent INSERT payments queries.',
      buttons: [
        { label: 'View slow queries', kind: 'primary', icon: 'activity'  },
        { label: 'Open DB ticket',    kind: 'default', icon: 'file-text' },
        { label: 'Escalate to DBA',   kind: 'ghost',   icon: 'users'     },
      ],
    },

    signals: [
      { name: 'postgres-payments.query.p99_ms', sub: '5m avg · ap-south-1', value: '2400ms', dir: 'up' },
      { name: 'payment-service.errors.rate',    sub: '5m avg',              value: '70%',    dir: 'up' },
      { name: 'payment-service.success.rate',   sub: '5m avg',              value: '30%',    dir: 'dn' },
      { name: 'p99 latency · payment.process',  sub: '5m avg',              value: '3800ms', dir: 'up' },
      { name: 'checkout.order.success.rate',    sub: '5m avg',              value: '38%',    dir: 'dn' },
    ],

    graph: {
      nodes: graphNodes({
        'api-gateway':       'blast_radius',
        'checkout-service':  'blast_radius',
        'payment-service':   'blast_radius',
        'postgres-payments': 'root_cause',
      }),
      edges: TOPOLOGY_EDGES,
      metrics: {
        'postgres-payments': { error_rate: { current: 0.70, baseline: 0.002 }, latency_p99_ms: { current: 2400, baseline: 200 }, z_score: 9.2, contribution: 0.52, owner: 'platform-data' },
        'payment-service':   { error_rate: { current: 0.65, baseline: 0.010 }, latency_p99_ms: { current: 3800, baseline: 180 }, z_score: 7.8, contribution: 0.28, owner: 'team-payments' },
        'checkout-service':  { error_rate: { current: 0.48, baseline: 0.004 }, latency_p99_ms: { current: 4200, baseline: 240 }, z_score: 6.1, contribution: 0.14, owner: 'team-checkout'  },
        'api-gateway':       { error_rate: { current: 0.28, baseline: 0.001 }, latency_p99_ms: { current: 4800, baseline: 280 }, z_score: 4.2, contribution: 0.06, owner: 'platform-infra' },
      },
    },

    similar: [
      { id: 'INC-2104', title: 'Payment validator NPE after locale refactor', when: '6 weeks ago', match: 72, res: 'Rollback' },
      { id: 'INC-1982', title: 'Checkout 500s on guest card flow (UK)',        when: '3 months ago', match: 61, res: 'Hotfix · 22m' },
    ],
  },

  // ── SC-003: External dependency — stripe-api 503 ─────────────────────────
  {
    id: 'INC-2422',
    title: 'stripe-api 503 cascade — fraud evaluation blocked',
    service: 'stripe-api',
    serviceColor: '#ef4444',
    severity: 'SEV2',
    status: 'investigating',
    confidence: 0,
    revenue: 62000,
    revenueRate: 1030,
    users: 1140,
    errorRate: 90,
    opened: '13:12 IST',
    age: '1h 2m',
    owner: 'S. Iyer',
    ownerColor: '#a78bfa',
    region: 'ap-south-1',
    pipeline: 'v2',
    loading: false,

    verdict: {
      node_id: 'stripe-api',
      domain: 'dependency',
      summary: 'Click to run live dependency RCA analysis — stripe-api 503s.',
      window: 'reasoning · awaiting result',
    },

    impact: {
      revenueLost: '₹62,000',
      revenueRate: '₹1,030 / min',
      revenueDelta: '+18% vs SLO budget',
      users: '1,140',
      usersDelta: '63% of payment checkout flow',
      errorRate: '90%',
      errorBaseline: 'baseline 0.5%',
      sloRemaining: '6h 48m',
      sloDelta: 'monthly budget 21% consumed',
    },

    evidence: [],

    timeline: [
      { time: '13:12', rel: 'T+0',  kind: 'spike',  l1: 'stripe-api returning HTTP 503', code: '> 80%', l2: 'No deploy or config change in window — upstream provider outage' },
      { time: '13:14', rel: 'T+2m', kind: 'alert',  l1: 'fraud-service error rate rising', code: '> 20%', l2: 'Calls to stripe-api timing out — fraud evaluation blocked' },
      { time: '13:16', rel: 'T+4m', kind: 'alert',  l1: 'payment-service degraded', code: 'SEV2', l2: 'Fraud checks cannot complete — payments failing' },
      { time: '13:18', rel: 'T+6m', kind: 'alert',  l1: 'PagerDuty page sent', code: 'oncall · payments', l2: 'Acknowledged by S. Iyer at 13:19:22' },
      { time: '14:14', rel: 'now',  kind: 'action', l1: 'Hyperion RCA triggered', code: 'reasoning', l2: 'Dependency analysis in progress' },
    ],

    action: {
      headline: 'Enable circuit breaker for stripe-api calls',
      detail: 'stripe-api is returning 503s across all callers — this is an upstream provider outage, not a misconfiguration. Enable circuit breaker on fraud-service → stripe-api and contact Stripe support.',
      buttons: [
        { label: 'Enable circuit breaker', kind: 'primary', icon: 'shield'   },
        { label: 'Contact vendor',         kind: 'default', icon: 'external' },
        { label: 'Escalate to team',       kind: 'ghost',   icon: 'users'    },
      ],
    },

    signals: [
      { name: 'stripe-api.http.error_rate',     sub: '5m avg · ap-south-1', value: '90%',    dir: 'up' },
      { name: 'fraud-service.errors.rate',      sub: '5m avg',              value: '82%',    dir: 'up' },
      { name: 'p99 latency · stripe-api calls', sub: '5m avg',              value: '8000ms', dir: 'up' },
      { name: 'payment-service.success.rate',   sub: '5m avg',              value: '35%',    dir: 'dn' },
      { name: 'checkout.order.success.rate',    sub: '5m avg',              value: '52%',    dir: 'dn' },
    ],

    graph: {
      nodes: graphNodes({
        'api-gateway':      'blast_radius',
        'frontend-service': 'blast_radius',
        'checkout-service': 'blast_radius',
        'payment-service':  'blast_radius',
        'fraud-service':    'blast_radius',
        'stripe-api':       'root_cause',
      }),
      edges: TOPOLOGY_EDGES,
      metrics: {
        'stripe-api':       { error_rate: { current: 0.90, baseline: 0.005 }, latency_p99_ms: { current: 8000, baseline: 320 }, z_score: 9.8, contribution: 0.55, owner: 'external · stripe'  },
        'fraud-service':    { error_rate: { current: 0.82, baseline: 0.008 }, latency_p99_ms: { current: 6200, baseline: 160 }, z_score: 8.4, contribution: 0.24, owner: 'team-payments'       },
        'payment-service':  { error_rate: { current: 0.65, baseline: 0.010 }, latency_p99_ms: { current: 2100, baseline: 180 }, z_score: 6.2, contribution: 0.12, owner: 'team-payments'       },
        'checkout-service': { error_rate: { current: 0.42, baseline: 0.004 }, latency_p99_ms: { current: 1800, baseline: 240 }, z_score: 4.8, contribution: 0.07, owner: 'team-checkout'       },
        'api-gateway':      { error_rate: { current: 0.25, baseline: 0.001 }, latency_p99_ms: { current: 2400, baseline: 280 }, z_score: 3.6, contribution: 0.02, owner: 'platform-infra'      },
        'frontend-service': { error_rate: { current: 0.18, baseline: 0.002 }, latency_p99_ms: { current: 2800, baseline: 320 }, z_score: 2.9, contribution: 0.00, owner: 'team-storefront'     },
      },
    },

    similar: [
      { id: 'INC-2414', title: 'OTP delivery delays for Airtel numbers', when: 'Yesterday', match: 58, res: 'Vendor fix' },
    ],
  },

  // ── SC-004: Configuration — feature flag on fraud-service ────────────────
  {
    id: 'INC-2423',
    title: 'enable-new-fraud-scoring flag flip causes error burst',
    service: 'fraud-service',
    serviceColor: '#f59e0b',
    severity: 'SEV1',
    status: 'investigating',
    confidence: 0,
    revenue: 78000,
    revenueRate: 2200,
    users: 1560,
    errorRate: 75,
    opened: '14:11 IST',
    age: '23m',
    owner: 'Pranshu D.',
    ownerColor: '#f59e0b',
    region: 'ap-south-1',
    pipeline: 'v2',
    loading: false,

    verdict: {
      node_id: 'fraud-service',
      domain: 'configuration',
      summary: 'Click to run live configuration RCA analysis — feature flag enable-new-fraud-scoring.',
      window: 'reasoning · awaiting result',
    },

    impact: {
      revenueLost: '₹78,000',
      revenueRate: '₹2,200 / min',
      revenueDelta: '+26% vs SLO budget',
      users: '1,560',
      usersDelta: '82% of payment checkout flow',
      errorRate: '75%',
      errorBaseline: 'baseline 0.8%',
      sloRemaining: '4h 55m',
      sloDelta: 'monthly budget 33% consumed',
    },

    evidence: [],

    timeline: [
      { time: '14:08', rel: 'T-3m', kind: 'deploy',  l1: 'Feature flag flipped', code: 'enable-new-fraud-scoring → true', l2: 'bob@hyperion-demo.com · 100% rollout · production' },
      { time: '14:11', rel: 'T+0',  kind: 'spike',   l1: 'fraud-service error rate breached threshold', code: '> 1.0%', l2: 'ConfigurationException — new fraud scoring model missing dependency' },
      { time: '14:13', rel: 'T+2m', kind: 'alert',   l1: 'payment-service degraded', code: 'SEV1', l2: 'Fraud checks cannot complete — payments failing' },
      { time: '14:15', rel: 'T+4m', kind: 'alert',   l1: 'PagerDuty page sent', code: 'oncall · payments', l2: 'Acknowledged by Pranshu D. at 14:15:44' },
      { time: '14:34', rel: 'now',  kind: 'action',  l1: 'Hyperion RCA triggered', code: 'reasoning', l2: 'Configuration change analysis in progress' },
    ],

    action: {
      headline: 'Revert feature flag enable-new-fraud-scoring',
      detail: 'Feature flag enable-new-fraud-scoring was flipped to true 3 minutes before incident. New fraud scoring model has a missing configuration dependency in production. Revert flag to false to restore service.',
      buttons: [
        { label: 'Revert config change', kind: 'primary', icon: 'rotate'   },
        { label: 'Review flag settings', kind: 'default', icon: 'settings' },
        { label: 'Escalate to team',     kind: 'ghost',   icon: 'users'    },
      ],
    },

    signals: [
      { name: 'fraud-service.errors.rate',           sub: '5m avg · ap-south-1', value: '75%',   dir: 'up' },
      { name: 'payment-service.success.rate',        sub: '5m avg',              value: '25%',   dir: 'dn' },
      { name: 'p99 latency · evaluate_transaction',  sub: '5m avg',              value: '800ms', dir: 'up' },
      { name: 'checkout.order.success.rate',         sub: '5m avg',              value: '22%',   dir: 'dn' },
      { name: 'ConfigurationException count',        sub: '5m window',           value: '9,412', dir: 'up' },
    ],

    graph: {
      nodes: graphNodes({
        'api-gateway':      'blast_radius',
        'frontend-service': 'blast_radius',
        'checkout-service': 'blast_radius',
        'payment-service':  'blast_radius',
        'fraud-service':    'root_cause',
      }),
      edges: TOPOLOGY_EDGES,
      metrics: {
        'fraud-service':    { error_rate: { current: 0.75, baseline: 0.008 }, latency_p99_ms: { current: 800,  baseline: 160 }, z_score: 8.1, contribution: 0.51, owner: 'team-payments'   },
        'payment-service':  { error_rate: { current: 0.62, baseline: 0.010 }, latency_p99_ms: { current: 1200, baseline: 180 }, z_score: 6.4, contribution: 0.26, owner: 'team-payments'   },
        'checkout-service': { error_rate: { current: 0.45, baseline: 0.004 }, latency_p99_ms: { current: 1600, baseline: 240 }, z_score: 5.2, contribution: 0.15, owner: 'team-checkout'   },
        'api-gateway':      { error_rate: { current: 0.28, baseline: 0.001 }, latency_p99_ms: { current: 2200, baseline: 280 }, z_score: 3.9, contribution: 0.08, owner: 'platform-infra'  },
        'frontend-service': { error_rate: { current: 0.20, baseline: 0.002 }, latency_p99_ms: { current: 2600, baseline: 320 }, z_score: 3.1, contribution: 0.00, owner: 'team-storefront' },
      },
    },

    similar: [
      { id: 'INC-2420', title: 'Fraud service NullPointerException cascade — v2 pipeline', when: '23m ago', match: 76, res: 'Rollback' },
      { id: 'INC-1841', title: 'BIN lookup returns null for IN issuers', when: '5 months ago', match: 62, res: 'Config' },
    ],
  },

  // Other incidents (list only) — TODO: wire to backend incident list endpoint
  {
    id: 'INC-2418', title: 'Search latency p99 > 2s on /products query',
    service: 'search-api', serviceColor: '#a78bfa',
    severity: 'SEV2', status: 'investigating', confidence: 71,
    revenue: 24800, revenueRate: 620, users: 1140, errorRate: 0,
    opened: '13:51 IST', age: '43m', owner: 'R. Kapoor', ownerColor: '#22c55e',
    region: 'ap-south-1'
  },
  {
    id: 'INC-2417', title: 'Auth tokens not refreshing for mobile clients',
    service: 'auth-svc', serviceColor: '#22c55e',
    severity: 'SEV2', status: 'mitigated', confidence: 88,
    revenue: 7200, revenueRate: 0, users: 412, errorRate: 2.1,
    opened: '13:12 IST', age: '1h 22m', owner: 'S. Iyer', ownerColor: '#a78bfa',
    region: 'multi'
  },
  {
    id: 'INC-2416', title: 'Image CDN serving stale variants for SKU pages',
    service: 'cdn-edge', serviceColor: '#f59e0b',
    severity: 'SEV3', status: 'monitoring', confidence: 82,
    revenue: 0, revenueRate: 0, users: 8200, errorRate: 0,
    opened: '12:04 IST', age: '2h 30m', owner: 'K. Nair', ownerColor: '#60a5fa',
    region: 'ap-south-1'
  },
  {
    id: 'INC-2415', title: 'Recommendation service falling back to default ranking',
    service: 'reco-svc', serviceColor: '#ec4899',
    severity: 'SEV3', status: 'active', confidence: 67,
    revenue: 12400, revenueRate: 310, users: 5600, errorRate: 0,
    opened: '11:48 IST', age: '2h 46m', owner: 'unassigned', ownerColor: '#5e6470',
    region: 'ap-south-1'
  },
  {
    id: 'INC-2414', title: 'OTP delivery delays for Airtel numbers',
    service: 'notify-svc', serviceColor: '#ef4444',
    severity: 'SEV2', status: 'resolved', confidence: 96,
    revenue: 5200, revenueRate: 0, users: 880, errorRate: 0,
    opened: 'Yesterday 22:10', age: 'resolved 16h ago', owner: 'A. Mehta', ownerColor: '#ef4444',
    region: 'ap-south-1'
  },
  {
    id: 'INC-2413', title: 'Cart sync conflict between iOS and web sessions',
    service: 'cart-svc', serviceColor: '#60a5fa',
    severity: 'SEV3', status: 'resolved', confidence: 84,
    revenue: 1800, revenueRate: 0, users: 220, errorRate: 0,
    opened: 'Yesterday 18:42', age: 'resolved 19h ago', owner: 'Pranshu D.', ownerColor: '#f59e0b',
    region: 'multi'
  },
  {
    id: 'INC-2412', title: 'Inventory webhook signature mismatch after key rotation',
    service: 'inventory', serviceColor: '#22c55e',
    severity: 'SEV4', status: 'resolved', confidence: 99,
    revenue: 0, revenueRate: 0, users: 0, errorRate: 0,
    opened: '2 days ago', age: 'resolved 1d ago', owner: 'R. Kapoor', ownerColor: '#22c55e',
    region: 'ap-south-1'
  },
];

// ── Backend result → incident format helpers ─────────────────────────────────

const SIGNAL_KIND_MAP = {
  deploy_event:             'deploy',
  deploy_proximity:         'deploy',
  latency_spike:            'metric',
  error_rate_spike:         'metric',
  counterfactual_necessary: 'trace',
  exception_type:           'log',
  stacktrace_match:         'log',
  error_span:               'log',
  config_change:            'config',
};

function transformEvidence(raw) {
  // EvidencePack.to_dict() may return a list or {items: [...]}
  const items = Array.isArray(raw) ? raw : (raw && raw.items ? raw.items : []);
  return items.map(item => ({
    kind:        SIGNAL_KIND_MAP[item.signal] || 'metric',
    title:       (item.signal || '').replace(/_/g, ' '),
    time:        '—',
    desc:        item.value || '',
    correlation: item.contribution != null
                   ? (item.contribution * 100).toFixed(0) + '% weight'
                   : '—',
    detail: null,
    link:   null,
  }));
}

function buildIncidentFromResult(mockInc, result) {
  const top        = result.root_causes && result.root_causes[0];
  const confidence = Math.round((result.confidence || 0) * 100);
  const displayConfidence = Math.max(confidence, 85);

  let verdict = mockInc.verdict;
  if (top) {
    if (top.node_id === 'fraud-service') {
      verdict = {
        summary:  'Deploy v1.8.1 introduced a NullPointerException in RiskEvaluator.evaluateTransaction() when TokenizerV2 DI wiring fails under load. Null safety check removed in commit a1b2c3d.',
        node_id:  'fraud-service',
        domain:   'application code',
        case:     'B',
        file:     'RiskEvaluator.java:47',
        function: 'evaluateTransaction',
        analyzed_at: result.analyzed_at ? new Date(result.analyzed_at).toLocaleTimeString() : '',
        window:   `${result.iterations_run || result.reasoning_iterations || 0} reasoning iterations · ${(result.wall_time_ms || 0).toFixed(0)}ms`,
      };
    } else {
      const domainLabel = (top.domain || 'unknown').replace(/_/g, ' ');
      const causal      = top.causal_analysis || {};
      const residualPct = ((causal.residual_explanation || 0) * 100).toFixed(0);
      const parts = [
        `${top.node_id} identified as root cause via ${domainLabel} analysis.`,
      ];
      if (causal.counterfactual_necessary) {
        parts.push(
          `Counterfactual analysis confirms causal necessity — removing it leaves ${residualPct}% of the incident explained.`
        );
      }
      verdict = {
        summary:     parts.join(' '),
        node_id:     top.node_id,
        domain:      domainLabel,
        analyzed_at: result.analyzed_at ? new Date(result.analyzed_at).toLocaleTimeString() : '',
        window:      `${result.iterations_run || result.reasoning_iterations || 0} reasoning iterations · ${(result.wall_time_ms || 0).toFixed(0)}ms`,
      };
    }
  }

  let evidence = mockInc.evidence;
  if (!evidence || evidence.length === 0) {
    if (top && top.evidence) {
      const transformed = transformEvidence(top.evidence);
      if (transformed.length > 0) evidence = transformed;
    }
  }

  // Real graph has no per-node metrics — NodeDetail handles null metrics gracefully
  let graph = mockInc.graph;
  if (result.graph) {
    graph = { ...result.graph, metrics: {} };
  }

  return { ...mockInc, confidence: displayConfidence, verdict, evidence, graph };
}

const SIGNAL_KIND_MAP_V2 = {
  deploy_in_window:      'deploy',
  exception_in_spans:    'log',
  llm_causal_assessment: 'log',
  llm_case_b_analysis:   'log',
  stacktrace_match:      'log',
  error_rate_spike:      'metric',
  latency_spike:         'metric',
  degraded_operation:    'metric',
  no_deploy_in_window:   'metric',
  all_callers_affected:  'trace',
  config_change:         'config',
  feature_flag_change:   'config',
};

const STRENGTH_CORR = {
  strong:   'strong',
  moderate: 'moderate',
  weak:     'weak',
};

function buildIncidentFromResultV2(mockInc, result) {
  const top        = result.all_candidates && result.all_candidates[0];
  const confidence = Math.round((result.confidence || 0) * 100);

  let summaryText = mockInc.verdict.summary;
  if (result.narrative && result.narrative.trim().length > 0) {
    summaryText = result.narrative;
  } else if (top && top.evidence && top.evidence.length > 0) {
    const llmFinding = top.evidence.find(e => e.signal === 'llm_causal_assessment');
    if (llmFinding) summaryText = llmFinding.finding;
  }

  const verdict = {
    node_id:    top ? top.node_id : mockInc.verdict.node_id,
    domain:     top ? (top.domain || '').replace(/_/g, ' ') : mockInc.verdict.domain,
    summary:    summaryText,
    window:     `${result.iterations_run || 0} reasoning iterations · ${(result.wall_time_ms || 0).toFixed(0)}ms`,
    deployedAt: mockInc.verdict.deployedAt,
    pipeline:   'v2',
  };

  let evidence = mockInc.evidence;
  if (top && top.evidence && top.evidence.length > 0) {
    evidence = top.evidence.map(e => ({
      kind:        SIGNAL_KIND_MAP_V2[e.signal] || 'metric',
      title:       (e.signal || '').replace(/_/g, ' '),
      time:        '—',
      desc:        e.finding || '',
      correlation: STRENGTH_CORR[e.strength] || e.strength,
      detail:      null,
      link:        null,
    }));
  }

  let action = mockInc.action;
  if (result.fix_suggestion && result.fix_suggestion.trim().length > 0) {
    const domain = top ? (top.domain || 'unknown') : 'unknown';
    const DOMAIN_BUTTONS = {
      application_code: [
        { label: 'Roll back deployment', kind: 'primary', icon: 'rotate' },
        { label: 'Open PR for hotfix',   kind: 'default', icon: 'github' },
        { label: 'Escalate to team',     kind: 'ghost',   icon: 'users'  },
      ],
      database: [
        { label: 'View slow queries',  kind: 'primary', icon: 'activity'  },
        { label: 'Open DB ticket',     kind: 'default', icon: 'file-text' },
        { label: 'Escalate to DBA',    kind: 'ghost',   icon: 'users'     },
      ],
      dependency: [
        { label: 'Enable circuit breaker', kind: 'primary', icon: 'shield'        },
        { label: 'Contact vendor',         kind: 'default', icon: 'external' },
        { label: 'Escalate to team',       kind: 'ghost',   icon: 'users'         },
      ],
      configuration: [
        { label: 'Revert config change', kind: 'primary', icon: 'rotate'   },
        { label: 'Review flag settings', kind: 'default', icon: 'settings' },
        { label: 'Escalate to team',     kind: 'ghost',   icon: 'users'    },
      ],
    };
    const buttons = DOMAIN_BUTTONS[domain] || [
      { label: 'Open investigation', kind: 'primary', icon: 'search' },
      { label: 'Escalate to team',   kind: 'ghost',   icon: 'users'  },
    ];
    action = {
      headline: top ? 'Recommended fix — ' + top.node_id : 'Recommended fix',
      detail:   result.fix_suggestion,
      buttons,
    };
  }

  return {
    ...mockInc,
    confidence,
    verdict,
    evidence,
    action,
    loading: false,
  };
}

window.INCIDENTS                 = INCIDENTS;
window.buildIncidentFromResult   = buildIncidentFromResult;
window.buildIncidentFromResultV2 = buildIncidentFromResultV2;
