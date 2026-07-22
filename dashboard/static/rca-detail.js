// RCA Detail page
const RcaDetail = ({ incident, onBack }) => {
  const inc = incident;
  const v   = inc.verdict;

  const [activeTab, setActiveTab] = React.useState('rca');

  const confTier = inc.confidence >= 85 ? '' : (inc.confidence >= 65 ? 'med' : 'low');

  return (
    <>
      <Topbar crumbs={['Operate', 'Incidents', inc.id]} actions={
        <>
          <button className="btn btn-ghost btn-sm"><Icon name="pin" size={12} /> Pin</button>
          <button className="btn btn-ghost btn-sm"><Icon name="share" size={12} /> Share</button>
          <button className="btn btn-ghost btn-sm"><Icon name="copy" size={12} /> Copy link</button>
          <div style={{ width: 1, height: 18, background: 'var(--border-2)', margin: '0 4px' }}></div>
          <button className="btn btn-ghost btn-sm"><Icon name="more" size={13} /></button>
        </>
      } />

      <div className="page">
        <button className="btn btn-ghost btn-sm" style={{ marginBottom: 12 }} onClick={onBack}>
          <Icon name="arrow-left" size={12} /> All incidents
        </button>

        {/* Header */}
        <div className="det-header">
          <div className="det-title-block">
            <div className="row-flex" style={{ marginBottom: 6, gap: 10 }}>
              <SeverityBadge sev={inc.severity} />
              <StatusPill status={inc.status} />
              <span className="det-id">{inc.id}</span>
              <span style={{ color: 'var(--text-4)' }}>·</span>
              <span style={{ color: 'var(--text-3)', fontSize: 12, fontFamily: 'var(--font-mono)' }}>
                opened {inc.opened} · {inc.age} ago
              </span>
            </div>
            <h1 className="det-title">{inc.title}</h1>
            <div className="det-meta">
              <span className="item">
                <ServiceIcon name={inc.service} color={inc.serviceColor} />
                <span style={{ color: 'var(--text-1)' }}>{inc.service}</span>
              </span>
              <span className="item"><Icon name="globe" size={13} /> {inc.region}</span>
              <span className="item">
                <span className="avatar" style={{ width: 18, height: 18, fontSize: 9, background: inc.ownerColor }}>
                  {inc.owner.split(' ').map(s => s[0]).join('').slice(0,2)}
                </span>
                Owner · {inc.owner}
              </span>
              <span className="item"><Icon name="message" size={13} /> #inc-{inc.id.toLowerCase()}</span>
            </div>
          </div>

          <div className="det-actions">
            <button className="btn"><Icon name="play" size={12} /> Snooze</button>
            <button className="btn"><Icon name="user" size={12} /> Reassign</button>
            <button className="btn btn-danger"><Icon name="rotate" size={12} /> Roll back deploy</button>
          </div>
        </div>

        {/* Tabs */}
        <div className="tabs">
          <button className={'tab ' + (activeTab==='rca'?'active':'')} onClick={()=>setActiveTab('rca')}>
            <Icon name="zap" size={13} /> RCA <span className="badge">{inc.confidence}%</span>
          </button>
          <button className={'tab ' + (activeTab==='timeline'?'active':'')} onClick={()=>setActiveTab('timeline')}>
            <Icon name="clock" size={13} /> Timeline
          </button>
          <button className={'tab ' + (activeTab==='evidence'?'active':'')} onClick={()=>setActiveTab('evidence')}>
            <Icon name="file-text" size={13} /> Evidence <span className="badge">{inc.evidence.length}</span>
          </button>
          {inc.action && (
            <button className={'tab ' + (activeTab==='fix'?'active':'')} onClick={()=>setActiveTab('fix')}
              style={{ color: 'var(--ok)' }}>
              <Icon name="check" size={13} /> Fix
            </button>
          )}
          <button className="tab">
            <Icon name="terminal" size={13} /> Logs
          </button>
        </div>

        <div className="det-grid">
          {/* MAIN COLUMN */}
          <div>
            {/* Verdict hero */}
            <div className="verdict">
              <div className="verdict-eyebrow">
                <Icon name="zap" size={12} />
                <span>Hyperion verdict</span>
                {inc.pipeline === 'v2' && (
                  <span style={{
                    fontFamily: 'var(--font-mono)', fontSize: 9, padding: '1px 5px',
                    border: '1px solid var(--accent-line)', borderRadius: 3,
                    color: 'var(--accent-2)', background: 'var(--accent-dim)',
                    letterSpacing: '0.04em', textTransform: 'uppercase',
                  }}>v2 pipeline</span>
                )}
                <span className="line"></span>
                {inc.loading ? (
                  <span style={{ color: 'var(--accent-2)', fontSize: 11 }}>
                    ⟳ Running reasoning + LLM analysis…
                  </span>
                ) : (
                  <span style={{ color: 'var(--text-2)' }}>
                    Generated {v.deployedAt || '—'} · {v.window}
                  </span>
                )}
              </div>
              <div className="verdict-text">
                {v.summary ? v.summary : (
                  <>
                    {v.summary_pre}{' '}
                    <span className="code">{v.summary_code}</span>{' '}
                    {v.summary_mid}{' '}
                    <span className="code err">{v.summary_err}</span>{' '}
                    {v.summary_end}
                  </>
                )}
              </div>
              <div className="verdict-foot">
                <div className={'confidence-big ' + confTier}>
                  <div className="confidence-ring" style={{ '--pct': inc.confidence }}>
                    <span className="pct">{inc.confidence}</span>
                  </div>
                  <div className="col">
                    <span className="label">Confidence</span>
                    <span style={{ fontSize: 12, color: 'var(--text-2)' }}>
                      {inc.evidence.length} signals evaluated
                    </span>
                  </div>
                </div>
                <div className="divider"></div>
                <div className="meta-grp">
                  <span className="l">Root cause</span>
                  <span className="v">{v.node_id || v.service}</span>
                </div>
                {(v.domain || v.cause) && <><div className="divider"></div>
                <div className="meta-grp">
                  <span className="l">Domain</span>
                  <span className="v">{v.domain || v.cause}</span>
                </div></>}
                <div className="divider"></div>
                <div className="meta-grp">
                  <span className="l">Analysis window</span>
                  <span className="v">{v.window}</span>
                </div>
                {v.pr && <><div className="divider"></div>
                <div className="meta-grp">
                  <span className="l">PR</span>
                  <span className="v">{v.pr}{v.author ? ' · ' + v.author : ''}</span>
                </div></>}
                <div style={{ marginLeft: 'auto' }}>
                  <button className="btn btn-sm"><Icon name="eye" size={12} /> How was this computed?</button>
                </div>
              </div>
            </div>

            {/* Dependency graph */}
            {inc.graph && <DepGraph data={inc.graph} incidentService={inc.service} />}

            {/* Evidence pack */}
            <div className="card">
              <div className="card-head">
                <div className="card-title">
                  <Icon name="file-text" size={13} />
                  Evidence pack
                  <span style={{
                    fontFamily: 'var(--font-mono)', fontSize: 10, padding: '1px 5px',
                    border: '1px solid var(--border-2)', borderRadius: 3,
                    color: 'var(--text-3)', textTransform: 'none', letterSpacing: 0
                  }}>{inc.evidence.length} signals</span>
                </div>
                <div className="card-actions">
                  <button className="btn btn-ghost btn-sm">
                    <Icon name="copy" size={11} /> Export pack
                  </button>
                </div>
              </div>
              <div className="evidence-list">
                {inc.loading ? (
                  <div style={{ padding: '24px 14px', color: 'var(--text-3)', fontSize: 12, textAlign: 'center' }}>
                    Fetching evidence from reasoning pipeline…
                  </div>
                ) : inc.evidence.length === 0 ? (
                  <div style={{ padding: '24px 14px', color: 'var(--text-3)', fontSize: 12, textAlign: 'center' }}>
                    No evidence signals returned.
                  </div>
                ) : (
                  inc.evidence.map((e, i) => <EvidenceItem key={i} n={i+1} ev={e} />)
                )}
              </div>
            </div>

            {/* Timeline — TODO: wire to backend PagerDuty + deploy hook integration */}
            <div className="card">
              <div className="card-head">
                <div className="card-title"><Icon name="clock" size={13} /> Incident timeline</div>
                <div className="card-actions">
                  <span style={{ fontSize: 11, color: 'var(--text-3)' }}>14:18 → 14:34 IST</span>
                </div>
              </div>
              <div className="card-body" style={{ paddingTop: 6 }}>
                <div className="timeline">
                  {inc.timeline.map((t, i) => (
                    <div className="tl-event" key={i}>
                      <div className="tl-time">
                        {t.time}
                        <span className="rel">{t.rel}</span>
                      </div>
                      <div className="tl-marker">
                        <div className={'tl-dot ' + t.kind}></div>
                      </div>
                      <div className="tl-body">
                        <div className="tl-line1">
                          <span>{t.l1}</span>
                          {t.code && <span className="code">{t.l1 === 'Hyperion RCA generated' ? `confidence ${inc.confidence}%` : t.code}</span>}
                        </div>
                        <div className="tl-line2">{t.l2}</div>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            </div>

            {/* Recommended fix — only present when inc.action is defined (INC-2420 / v2 pipeline) */}
            {inc.action && (
              <div className="card">
                <div className="card-head">
                  <div className="card-title" style={{ color: 'var(--ok)' }}>
                    <Icon name="check" size={13} /> Recommended fix
                  </div>
                </div>
                <div className="card-body">
                  <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 20 }}>
                    <div style={{ flex: 1 }}>
                      <div style={{ fontWeight: 600, fontSize: 13, color: 'var(--text-1)', marginBottom: 5 }}>
                        {inc.action.headline}
                      </div>
                      <div style={{ fontSize: 12, color: 'var(--text-3)', lineHeight: 1.6 }}>
                        {inc.action.detail}
                      </div>
                    </div>
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 7, flexShrink: 0 }}>
                      {inc.action.buttons.map((b, i) => (
                        <button key={i} className={
                          b.kind === 'primary' ? 'btn btn-danger' :
                          b.kind === 'ghost'   ? 'btn btn-ghost btn-sm' :
                                                 'btn btn-ghost btn-sm'
                        }>
                          <Icon name={b.icon} size={11} /> {b.label}
                        </button>
                      ))}
                    </div>
                  </div>
                </div>
              </div>
            )}
          </div>

          {/* SIDE COLUMN */}
          <div>
            {/* Live signals */}
            <div className="card">
              <div className="card-head">
                <div className="card-title"><Icon name="activity" size={13} /> Correlated signals</div>
                <div className="card-actions">
                  <span style={{ fontSize: 10.5, color: 'var(--text-3)', fontFamily: 'var(--font-mono)' }}>5m</span>
                </div>
              </div>
              <div className="signals">
                {inc.signals.map((s, i) => (
                  <div key={i} className={'signal ' + s.dir}>
                    <div className="l">
                      <div className="nm">
                        <Icon name={s.dir === 'up' ? 'trending-up' : 'trending-down'} size={11} />
                        {s.name}
                      </div>
                      <div className="sub">{s.sub}</div>
                    </div>
                    <div className="v">{s.value}</div>
                  </div>
                ))}
              </div>
            </div>

            {/* Similar past incidents — TODO: wire to backend cross-incident embedding search */}
            <div className="card">
              <div className="card-head">
                <div className="card-title"><Icon name="history" size={13} /> Similar past incidents</div>
              </div>
              <div className="sim">
                {inc.similar.map((s, i) => (
                  <div key={i} className="sim-row">
                    <div>
                      <div className="t">{s.title}</div>
                      <div className="m">
                        <span className="mono">{s.id}</span>
                        <span className="sep">·</span>
                        <span>{s.when}</span>
                        <span className="sep">·</span>
                        <span>resolved by <span style={{ color: 'var(--text-2)' }}>{s.res}</span></span>
                      </div>
                    </div>
                    <div className="pct">{s.match}% match</div>
                  </div>
                ))}
              </div>
            </div>

            {/* Data sources — TODO: wire to backend integration health checks */}
            <div className="card">
              <div className="card-head">
                <div className="card-title"><Icon name="share" size={13} /> Data sources</div>
              </div>
              <div className="card-body" style={{ padding: 0 }}>
                <SourceRow ico="datadog" name="Datadog" detail="metrics · logs · traces · APM" status="connected" />
                <SourceRow ico="github" name="GitHub" detail="logiq/checkout · 8 services" status="connected" />
                <SourceRow ico="rocket" name="ArgoCD" detail="prod · ap-south-1" status="connected" />
                <SourceRow ico="bell" name="PagerDuty" detail="payments · oncall" status="connected" />
                <SourceRow ico="flag" name="LaunchDarkly" detail="checkout.* flags" status="connected" last />
              </div>
            </div>
          </div>
        </div>
      </div>
    </>
  );
};

const SourceRow = ({ ico, name, detail, status, last }) => (
  <div className="signal" style={{ borderBottom: last ? 'none' : null }}>
    <div className="l">
      <div className="nm">
        <Icon name={ico} size={12} />
        <span>{name}</span>
      </div>
      <div className="sub">{detail}</div>
    </div>
    <div style={{ fontSize: 10.5, color: 'var(--ok)', display: 'flex', alignItems: 'center', gap: 5 }}>
      <span style={{ width: 6, height: 6, borderRadius: 3, background: 'var(--ok)' }}></span>
      live
    </div>
  </div>
);

const EvidenceItem = ({ n, ev }) => {
  const kinds = {
    deploy: { label: 'Deploy',  icon: 'rocket' },
    commit: { label: 'Commit',  icon: 'git-commit' },
    metric: { label: 'Metric',  icon: 'bar-chart' },
    log:    { label: 'Log',     icon: 'terminal' },
    trace:  { label: 'Trace',   icon: 'activity' },
    config: { label: 'Config',  icon: 'flag' },
  };
  const k = kinds[ev.kind] || kinds.metric;

  const sparkPath = (data, w, h) => {
    const max = Math.max(...data), min = Math.min(...data);
    const range = max - min || 1;
    return data.map((val, i) => {
      const x = (i / (data.length - 1)) * w;
      const y = h - ((val - min) / range) * (h - 4) - 2;
      return (i === 0 ? 'M' : 'L') + x.toFixed(1) + ',' + y.toFixed(1);
    }).join(' ');
  };

  return (
    <div className="ev-item">
      <div className="ev-num">{n}</div>
      <div className="ev-content">
        <div className="ev-row1">
          <span className={'ev-kind k-' + ev.kind}>
            <Icon name={k.icon} size={10} />
            {k.label}
          </span>
          <span className="ev-title">{ev.title}</span>
          <span className="ev-time mono">{ev.time}</span>
        </div>
        <div className="ev-desc">{ev.desc}</div>

        {ev.detail && ev.detail.type === 'diff' && (
          <div className="ev-detail">
            {ev.detail.lines.map((l, i) => (
              <div key={i} className={'diff-line ' + (l.kind === 'add' ? 'add' : l.kind === 'del' ? 'del' : '')}>
                <span className="ln">{l.kind === 'add' ? '+ ' + l.n : (l.kind === 'del' ? '- ' + l.n : l.n)}</span>
                <span>{l.t}</span>
              </div>
            ))}
          </div>
        )}

        {ev.detail && ev.detail.type === 'code' && (
          <pre className="ev-detail" style={{ margin: 0, whiteSpace: 'pre-wrap' }}>{ev.detail.text}</pre>
        )}

        {ev.detail && ev.detail.type === 'sparkline' && (
          <div className="ev-detail" style={{ padding: '8px 12px' }}>
            <svg viewBox="0 0 320 50" style={{ width: '100%', height: 50, display: 'block' }} preserveAspectRatio="none">
              <defs>
                <linearGradient id="spark-grad" x1="0" x2="0" y1="0" y2="1">
                  <stop offset="0%" stopColor="rgba(239,68,68,0.35)" />
                  <stop offset="100%" stopColor="rgba(239,68,68,0)" />
                </linearGradient>
              </defs>
              <path d={sparkPath(ev.detail.data, 320, 50) + ' L320,50 L0,50 Z'} fill="url(#spark-grad)" />
              <path d={sparkPath(ev.detail.data, 320, 50)} fill="none" stroke="#ef4444" strokeWidth="1.5" />
              <line x1="200" y1="0" x2="200" y2="50" stroke="rgba(167,139,250,0.5)" strokeWidth="1" strokeDasharray="3 3" />
              <text x="202" y="11" fontSize="9" fill="#a78bfa" fontFamily="var(--font-mono)">deploy</text>
            </svg>
          </div>
        )}

        {ev.link && (
          <div style={{ marginTop: 8 }}>
            <a className="ev-link" href="#">
              <Icon name={ev.link.icon} size={11} /> {ev.link.label}
              <Icon name="arrow-up-right" size={10} />
            </a>
          </div>
        )}
      </div>
      <div className="ev-right">
        <span className="ev-corr">{ev.correlation}</span>
      </div>
    </div>
  );
};

window.RcaDetail = RcaDetail;
