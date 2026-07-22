// Dependency graph — D3 force layout, custom shapes per node type
const DepGraph = ({ data, incidentService }) => {
  const svgRef  = React.useRef(null);
  const wrapRef = React.useRef(null);
  const [selectedId, setSelectedId] = React.useState(
    (data.nodes.find(n => n.state === 'root_cause') || data.nodes[0]).id
  );
  const [zoomLevel, setZoomLevel] = React.useState(1);

  React.useEffect(() => {
    if (!svgRef.current || !window.d3) return;
    const d3     = window.d3;
    const svgEl  = svgRef.current;
    const rect   = wrapRef.current.getBoundingClientRect();
    const width  = rect.width;
    const height = rect.height;

    const nodes = data.nodes.map(n => ({ ...n }));
    const links = data.edges.map(e => ({ ...e }));

    const svg = d3.select(svgEl);
    svg.selectAll('*').remove();
    svg.attr('viewBox', `0 0 ${width} ${height}`);

    const defs = svg.append('defs');
    const arrowConfigs = [
      { id: 'arrow-solid',  color: '#5b6473' },
      { id: 'arrow-dashed', color: '#5b6473' },
      { id: 'arrow-dotted', color: '#5b6473' },
      { id: 'arrow-thin',   color: '#3f4654' },
      { id: 'arrow-hot',    color: '#ef4444' },
    ];
    arrowConfigs.forEach(c => {
      defs.append('marker')
        .attr('id', c.id)
        .attr('viewBox', '0 0 10 10')
        .attr('refX', 9).attr('refY', 5)
        .attr('markerWidth', 7).attr('markerHeight', 7)
        .attr('orient', 'auto-start-reverse')
        .append('path').attr('d', 'M 0 0 L 10 5 L 0 10 z').attr('fill', c.color);
    });

    const filter = defs.append('filter')
      .attr('id', 'root-glow')
      .attr('x', '-50%').attr('y', '-50%')
      .attr('width', '200%').attr('height', '200%');
    filter.append('feGaussianBlur').attr('stdDeviation', '4').attr('result', 'blur');
    const feMerge = filter.append('feMerge');
    feMerge.append('feMergeNode').attr('in', 'blur');
    feMerge.append('feMergeNode').attr('in', 'SourceGraphic');

    const root = svg.append('g').attr('class', 'graph-root');
    const zoom = d3.zoom()
      .scaleExtent([0.4, 2.5])
      .on('zoom', (event) => {
        root.attr('transform', event.transform);
        setZoomLevel(event.transform.k);
      });
    svg.call(zoom);
    svg.on('dblclick.zoom', null);

    const grid    = root.append('g').attr('class', 'graph-grid').attr('pointer-events', 'none');
    const gridSize = 24;
    for (let x = 0; x < width + gridSize; x += gridSize) {
      for (let y = 0; y < height + gridSize; y += gridSize) {
        grid.append('circle').attr('cx', x).attr('cy', y).attr('r', 0.7).attr('fill', '#1d2028');
      }
    }

    const sim = d3.forceSimulation(nodes)
      .force('link', d3.forceLink(links).id(d => d.id).distance(125).strength(0.55))
      .force('charge', d3.forceManyBody().strength(-680))
      .force('center', d3.forceCenter(width / 2, height / 2))
      .force('collide', d3.forceCollide().radius(48))
      .force('x', d3.forceX(width / 2).strength(0.05))
      .force('y', d3.forceY(height / 2).strength(0.06));

    const edgeStyle = {
      calls:         { stroke: '#5b6473', width: 1.5, dash: null,    marker: 'arrow-solid' },
      reads_from:    { stroke: '#5b6473', width: 1.5, dash: '6 4',   marker: 'arrow-dashed' },
      writes_to:     { stroke: '#5b6473', width: 1.5, dash: '6 4',   marker: 'arrow-dashed' },
      publishes_to:  { stroke: '#5b6473', width: 1.5, dash: '1.5 4', marker: 'arrow-dotted' },
      subscribes_to: { stroke: '#5b6473', width: 1.5, dash: '1.5 4', marker: 'arrow-dotted' },
      depends_on:    { stroke: '#3f4654', width: 1,   dash: null,    marker: 'arrow-thin' },
    };

    const rootId    = (nodes.find(n => n.state === 'root_cause') || {}).id;
    const linkGroup = root.append('g').attr('class', 'links');
    const link      = linkGroup.selectAll('g.link').data(links).join('g').attr('class', 'link');

    link.append('line')
      .attr('class', 'link-line')
      .each(function(d) {
        const st    = edgeStyle[d.type] || edgeStyle.calls;
        const isHot = (d.source === rootId || d.target === rootId ||
                       d.source.id === rootId || d.target.id === rootId);
        const sel   = d3.select(this);
        sel
          .attr('stroke', isHot ? '#ef4444' : st.stroke)
          .attr('stroke-width', isHot ? 1.8 : st.width)
          .attr('stroke-opacity', isHot ? 0.9 : 0.7)
          .attr('marker-end', `url(#${isHot ? 'arrow-hot' : st.marker})`)
          .attr('fill', 'none');
        if (st.dash) sel.attr('stroke-dasharray', st.dash);
      });

    const linkLabel = link.append('text')
      .attr('class', 'link-label')
      .text(d => d.type.replace('_', ' '))
      .attr('font-family', 'JetBrains Mono, monospace')
      .attr('font-size', 8.5)
      .attr('fill', '#6b7280')
      .attr('text-anchor', 'middle')
      .attr('dy', -3)
      .attr('opacity', 0)
      .attr('pointer-events', 'none');

    link
      .on('mouseenter', function() {
        d3.select(this).select('.link-label').attr('opacity', 1);
        d3.select(this).select('.link-line').attr('stroke-opacity', 1);
      })
      .on('mouseleave', function(_, d) {
        d3.select(this).select('.link-label').attr('opacity', 0);
        const isHot = (d.source.id === rootId || d.target.id === rootId);
        d3.select(this).select('.link-line').attr('stroke-opacity', isHot ? 0.9 : 0.7);
      });

    const nodeG = root.append('g').attr('class', 'nodes');
    const node  = nodeG.selectAll('g.node').data(nodes).join('g')
      .attr('class', 'node')
      .style('cursor', 'pointer')
      .on('click', (_, d) => setSelectedId(d.id))
      .call(d3.drag()
        .on('start', (event, d) => {
          if (!event.active) sim.alphaTarget(0.3).restart();
          d.fx = d.x; d.fy = d.y;
        })
        .on('drag', (event, d) => { d.fx = event.x; d.fy = event.y; })
        .on('end', (event, d) => {
          if (!event.active) sim.alphaTarget(0);
          d.fx = null; d.fy = null;
        })
      );

    node.filter(d => d.state === 'root_cause')
      .append('circle')
      .attr('class', 'root-pulse')
      .attr('r', 22)
      .attr('fill', 'none')
      .attr('stroke', '#ef4444')
      .attr('stroke-width', 1.5)
      .attr('opacity', 0.5);

    const drawShape = (sel) => {
      sel.each(function(d) {
        const g       = d3.select(this);
        const fill    = d.state === 'root_cause' ? '#ef4444' : d.state === 'blast_radius' ? '#f5a524' : '#475569';
        const stroke  = d.state === 'root_cause' ? '#fca5a5' : d.state === 'blast_radius' ? '#fcd34d' : '#64748b';
        const strokeOp = d.state === 'healthy' ? 0.5 : 0.9;

        if (d.type === 'service') {
          g.append('circle')
            .attr('class', 'shape').attr('r', 18)
            .attr('fill', fill).attr('fill-opacity', d.state === 'healthy' ? 0.7 : 1)
            .attr('stroke', stroke).attr('stroke-opacity', strokeOp).attr('stroke-width', 1.5);
          if (d.state === 'root_cause') g.select('.shape').attr('filter', 'url(#root-glow)');
        } else if (d.type === 'database') {
          const w = 30, h = 26, rx = w / 2, ry = 5;
          const path = `M ${-rx} ${-h/2 + ry} L ${-rx} ${h/2 - ry} A ${rx} ${ry} 0 0 0 ${rx} ${h/2 - ry} L ${rx} ${-h/2 + ry} A ${rx} ${ry} 0 0 0 ${-rx} ${-h/2 + ry} Z`;
          g.append('path').attr('class', 'shape').attr('d', path)
            .attr('fill', fill).attr('fill-opacity', d.state === 'healthy' ? 0.7 : 1)
            .attr('stroke', stroke).attr('stroke-opacity', strokeOp).attr('stroke-width', 1.5);
          g.append('ellipse').attr('cx', 0).attr('cy', -h/2 + ry).attr('rx', rx).attr('ry', ry)
            .attr('fill', 'rgba(255,255,255,0.1)').attr('stroke', stroke).attr('stroke-opacity', strokeOp).attr('stroke-width', 1.5);
        } else if (d.type === 'queue') {
          const r = 18, pts = [];
          for (let i = 0; i < 6; i++) {
            const a = (Math.PI / 3) * i + Math.PI / 6;
            pts.push([r * Math.cos(a), r * Math.sin(a)]);
          }
          g.append('polygon').attr('class', 'shape')
            .attr('points', pts.map(p => p.join(',')).join(' '))
            .attr('fill', fill).attr('fill-opacity', d.state === 'healthy' ? 0.7 : 1)
            .attr('stroke', stroke).attr('stroke-opacity', strokeOp).attr('stroke-width', 1.5);
        } else if (d.type === 'external_dep') {
          const r = 18;
          g.append('polygon').attr('class', 'shape')
            .attr('points', `0,${-r} ${r},0 0,${r} ${-r},0`)
            .attr('fill', fill).attr('fill-opacity', d.state === 'healthy' ? 0.7 : 1)
            .attr('stroke', stroke).attr('stroke-opacity', strokeOp).attr('stroke-width', 1.5);
        }
      });
    };
    drawShape(node);

    node.append('text').attr('class', 'node-label')
      .text(d => d.id)
      .attr('y', d => d.type === 'database' ? 24 : 30)
      .attr('text-anchor', 'middle')
      .attr('font-family', 'JetBrains Mono, monospace').attr('font-size', 10.5)
      .attr('fill', d => d.state === 'healthy' ? '#9aa1ad' : '#e7e9ee')
      .attr('font-weight', d => d.state === 'root_cause' ? 600 : 500);

    node.append('text').attr('class', 'node-sublabel')
      .text(d => d.type)
      .attr('y', d => d.type === 'database' ? 36 : 42)
      .attr('text-anchor', 'middle')
      .attr('font-family', 'Inter, sans-serif').attr('font-size', 9)
      .attr('fill', '#5e6470').attr('letter-spacing', '0.04em');

    node.append('circle').attr('class', 'sel-ring')
      .attr('r', 26).attr('fill', 'none')
      .attr('stroke', '#4c8dff').attr('stroke-width', 1.5)
      .attr('stroke-dasharray', '3 3').attr('opacity', 0);

    sim.on('tick', () => {
      link.select('line')
        .attr('x1', d => d.source.x).attr('y1', d => d.source.y)
        .attr('x2', d => {
          const dx = d.target.x - d.source.x, dy = d.target.y - d.source.y;
          const dist = Math.sqrt(dx*dx + dy*dy) || 1;
          return d.target.x - (dx/dist) * 24;
        })
        .attr('y2', d => {
          const dx = d.target.x - d.source.x, dy = d.target.y - d.source.y;
          const dist = Math.sqrt(dx*dx + dy*dy) || 1;
          return d.target.y - (dy/dist) * 24;
        });
      linkLabel
        .attr('x', d => (d.source.x + d.target.x) / 2)
        .attr('y', d => (d.source.y + d.target.y) / 2);
      node.attr('transform', d => `translate(${d.x}, ${d.y})`);
    });

    const selectedIdRef = { current: selectedId };
    const updateSelection = () => {
      node.select('.sel-ring').attr('opacity', d => d.id === selectedIdRef.current ? 0.9 : 0);
    };
    setSelectedExposer(() => (id) => {
      selectedIdRef.current = id;
      updateSelection();
    });
    updateSelection();

    return () => { sim.stop(); };
  }, [data]);

  const [exposed, setSelectedExposer] = React.useState(() => () => {});
  React.useEffect(() => { exposed(selectedId); }, [selectedId, exposed]);

  const sel = data.nodes.find(n => n.id === selectedId);
  // Null-safe: real backend graph has no per-node metrics
  const m   = sel ? (data.metrics || {})[sel.id] || null : null;

  return (
    <div className="card dep-card">
      <div className="card-head">
        <div className="card-title">
          <Icon name="services" size={13} />
          Dependency graph · distance-1
          <span style={{
            fontFamily: 'var(--font-mono)', fontSize: 10, padding: '1px 5px',
            border: '1px solid var(--border-2)', borderRadius: 3,
            color: 'var(--text-3)', textTransform: 'none', letterSpacing: 0
          }}>{data.nodes.length} nodes · {data.edges.length} edges</span>
        </div>
        <div className="card-actions">
          <button className="btn btn-ghost btn-sm" onClick={() => {
            if (window.d3 && svgRef.current) {
              const svg = window.d3.select(svgRef.current);
              svg.transition().duration(300).call(window.d3.zoom().transform, window.d3.zoomIdentity);
            }
          }}>
            <Icon name="search" size={11} /> Fit
          </button>
          <button className="btn btn-ghost btn-sm">
            <Icon name="external" size={11} /> Open topology
          </button>
        </div>
      </div>

      <div className="dep-body">
        <div className="dep-canvas-wrap" ref={wrapRef}>
          <svg ref={svgRef} className="dep-canvas"></svg>

          <div className="dep-legend">
            <div className="dep-legend-title">States</div>
            <div className="dep-legend-row"><span className="lg-dot" style={{ background: '#ef4444' }}></span>root cause</div>
            <div className="dep-legend-row"><span className="lg-dot" style={{ background: '#f5a524' }}></span>blast radius</div>
            <div className="dep-legend-row"><span className="lg-dot" style={{ background: '#475569' }}></span>healthy</div>
            <div className="dep-legend-title">Edges</div>
            <div className="dep-legend-row"><svg width="28" height="8"><line x1="0" y1="4" x2="28" y2="4" stroke="#5b6473" strokeWidth="1.5"/></svg>calls</div>
            <div className="dep-legend-row"><svg width="28" height="8"><line x1="0" y1="4" x2="28" y2="4" stroke="#5b6473" strokeWidth="1.5" strokeDasharray="6 4"/></svg>reads / writes</div>
            <div className="dep-legend-row"><svg width="28" height="8"><line x1="0" y1="4" x2="28" y2="4" stroke="#5b6473" strokeWidth="1.5" strokeDasharray="1.5 4"/></svg>pub / sub</div>
            <div className="dep-legend-row"><svg width="28" height="8"><line x1="0" y1="4" x2="28" y2="4" stroke="#3f4654" strokeWidth="1"/></svg>depends on</div>
          </div>

          <div className="dep-zoom">
            <span className="mono">{(zoomLevel * 100).toFixed(0)}%</span>
            <span style={{ color: 'var(--text-3)' }}>· scroll to zoom · drag to pan</span>
          </div>
        </div>

        <div className="dep-side">
          {sel && <NodeDetail node={sel} metrics={m} />}
        </div>
      </div>
    </div>
  );
};

const NodeDetail = ({ node, metrics }) => {
  const stateMeta = {
    root_cause:   { color: '#ef4444', label: 'ROOT CAUSE',   bg: 'rgba(239,68,68,0.13)' },
    blast_radius: { color: '#f5a524', label: 'BLAST RADIUS', bg: 'rgba(245,165,36,0.13)' },
    healthy:      { color: '#64748b', label: 'HEALTHY',      bg: 'rgba(100,116,139,0.13)' },
  }[node.state];

  const shapeIcon = {
    service:      <circle cx="9" cy="9" r="7" fill="currentColor" />,
    database:     <path d="M2 4 C 2 2, 16 2, 16 4 L 16 14 C 16 16, 2 16, 2 14 Z M 2 4 C 2 6, 16 6, 16 4" fill="currentColor" stroke="none"/>,
    queue:        <polygon points="9,1 16,5 16,13 9,17 2,13 2,5" fill="currentColor"/>,
    external_dep: <polygon points="9,1 17,9 9,17 1,9" fill="currentColor"/>,
  }[node.type];

  // Graceful degradation when backend graph has no per-node metrics
  // TODO: wire to backend observability — error_rate and latency_p99_ms per node
  if (!metrics) {
    return (
      <div className="node-detail">
        <div className="nd-header">
          <div className="nd-shape" style={{ color: stateMeta.color }}>
            <svg width="18" height="18" viewBox="0 0 18 18">{shapeIcon}</svg>
          </div>
          <div style={{ minWidth: 0, flex: 1 }}>
            <div className="nd-id">{node.id}</div>
            <div className="nd-type">{node.type.replace(/_/g, ' ')}</div>
          </div>
        </div>
        <div className="nd-state" style={{
          background: stateMeta.bg, color: stateMeta.color,
          borderColor: stateMeta.color + '40',
        }}>
          <span className="dot" style={{ background: stateMeta.color }}></span>
          {stateMeta.label}
        </div>
        <div className="nd-actions">
          <button className="btn btn-sm" style={{ width: '100%', justifyContent: 'center' }}>
            <Icon name="external" size={11} /> View service dashboard
          </button>
          <button className="btn btn-ghost btn-sm" style={{ width: '100%', justifyContent: 'center', marginTop: 6 }}>
            <Icon name="terminal" size={11} /> Tail logs
          </button>
        </div>
      </div>
    );
  }

  const errPct        = (metrics.error_rate.current * 100).toFixed(2);
  const errBasePct    = (metrics.error_rate.baseline * 100).toFixed(2);
  const errMultiplier = (metrics.error_rate.current / Math.max(metrics.error_rate.baseline, 0.0001)).toFixed(0);
  const latencyMult   = (metrics.latency_p99_ms.current / metrics.latency_p99_ms.baseline).toFixed(1);

  return (
    <div className="node-detail">
      <div className="nd-header">
        <div className="nd-shape" style={{ color: stateMeta.color }}>
          <svg width="18" height="18" viewBox="0 0 18 18">{shapeIcon}</svg>
        </div>
        <div style={{ minWidth: 0, flex: 1 }}>
          <div className="nd-id">{node.id}</div>
          <div className="nd-type">{node.type.replace(/_/g, ' ')} · owner {metrics.owner}</div>
        </div>
      </div>

      <div className="nd-state" style={{ background: stateMeta.bg, color: stateMeta.color, borderColor: stateMeta.color + '40' }}>
        <span className="dot" style={{ background: stateMeta.color }}></span>
        {stateMeta.label}
      </div>

      <div className="nd-section-title">Live signals</div>

      <div className="nd-metric">
        <div className="nd-metric-head">
          <span className="nd-metric-name">Error rate</span>
          <span className="nd-metric-mult" style={{ color: errMultiplier > 5 ? '#fca5a5' : (errMultiplier > 1.5 ? '#fcd34d' : 'var(--text-2)') }}>
            ×{errMultiplier}
          </span>
        </div>
        <div className="nd-metric-row">
          <span className="nd-current">{errPct}%</span>
          <span className="nd-vs">vs baseline {errBasePct}%</span>
        </div>
        <MetricBar current={metrics.error_rate.current} baseline={metrics.error_rate.baseline} max={Math.max(metrics.error_rate.current, metrics.error_rate.baseline) * 1.2} />
      </div>

      <div className="nd-metric">
        <div className="nd-metric-head">
          <span className="nd-metric-name">p99 latency</span>
          <span className="nd-metric-mult" style={{ color: latencyMult > 5 ? '#fca5a5' : (latencyMult > 1.5 ? '#fcd34d' : 'var(--text-2)') }}>
            ×{latencyMult}
          </span>
        </div>
        <div className="nd-metric-row">
          <span className="nd-current">{metrics.latency_p99_ms.current.toLocaleString()}ms</span>
          <span className="nd-vs">vs baseline {metrics.latency_p99_ms.baseline}ms</span>
        </div>
        <MetricBar current={metrics.latency_p99_ms.current} baseline={metrics.latency_p99_ms.baseline} max={Math.max(metrics.latency_p99_ms.current, metrics.latency_p99_ms.baseline) * 1.2} />
      </div>

      <div className="nd-stat-grid">
        <div className="nd-stat">
          <div className="nd-stat-l">z-score</div>
          <div className="nd-stat-v" style={{ color: metrics.z_score >= 5 ? '#fca5a5' : (metrics.z_score >= 2.5 ? '#fcd34d' : 'var(--text-1)') }}>
            {metrics.z_score.toFixed(1)}
          </div>
        </div>
        <div className="nd-stat">
          <div className="nd-stat-l">RCA contribution</div>
          <div className="nd-stat-v">{(metrics.contribution * 100).toFixed(0)}%</div>
        </div>
      </div>

      <div className="nd-actions">
        <button className="btn btn-sm" style={{ width: '100%', justifyContent: 'center' }}>
          <Icon name="external" size={11} /> View service dashboard
        </button>
        <button className="btn btn-ghost btn-sm" style={{ width: '100%', justifyContent: 'center', marginTop: 6 }}>
          <Icon name="terminal" size={11} /> Tail logs
        </button>
      </div>
    </div>
  );
};

const MetricBar = ({ current, baseline, max }) => {
  const cw = Math.min(100, (current / max) * 100);
  const bw = Math.min(100, (baseline / max) * 100);
  return (
    <div className="nd-bar">
      <div className="nd-bar-current" style={{ width: cw + '%' }}></div>
      <div className="nd-bar-baseline" style={{ left: bw + '%' }}></div>
    </div>
  );
};

window.DepGraph = DepGraph;
