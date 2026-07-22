// Incident List page — accepts `incidents` prop (passed from App state)
const IncidentList = ({ onOpen, incidents }) => {
  const [statusFilter, setStatusFilter] = React.useState('open');
  const [showNewIncident, setShowNewIncident] = React.useState(false);
  const filtered = incidents.filter(i => {
    if (statusFilter === 'open') return ['active','investigating','mitigated','monitoring'].includes(i.status);
    if (statusFilter === 'resolved') return i.status === 'resolved';
    return true;
  });

  const openCount     = incidents.filter(i => ['active','investigating','mitigated','monitoring'].includes(i.status)).length;
  const resolvedCount = incidents.filter(i => i.status === 'resolved').length;

  return (
    <>
      <Topbar crumbs={['Operate', 'Incidents']} />
      <div className="page">
        <div className="list-header">
          <div>
            <h1 className="h1">Incidents</h1>
            {/* TODO: wire to backend — service count from topology */}
            <div className="h1-sub">Active production incidents across 21 services · ap-south-1</div>
          </div>
          <div style={{ marginLeft: 'auto', display: 'flex', gap: 8 }}>
            <button className="btn">
              <Icon name="filter" size={13} />
              <span>Save view</span>
            </button>
            <button className="btn btn-primary" onClick={() => setShowNewIncident(true)}>
              <Icon name="plus" size={13} />
              <span>New incident</span>
            </button>
          </div>
        </div>

        {/* TODO: wire to backend — stat strip from aggregated incident metrics */}
        <div className="stat-strip">
          <div className="stat">
            <div className="stat-label"><Icon name="alert" size={11} /> Active incidents</div>
            <div className="stat-val">5</div>
            <div className="stat-delta up"><Icon name="arrow-up" size={10} /> +2 in last 1h</div>
          </div>
          <div className="stat">
            <div className="stat-label"><Icon name="clock" size={11} /> Avg time to verdict</div>
            <div className="stat-val">2m 14s</div>
            <div className="stat-delta down"><Icon name="arrow-down" size={10} /> 96% faster vs manual</div>
          </div>
          <div className="stat">
            <div className="stat-label"><Icon name="trending-up" size={11} /> Verdicts &gt; 85% confidence</div>
            <div className="stat-val">87%</div>
            <div className="stat-delta">last 30 days · 124 incidents</div>
          </div>
        </div>

        <div className="filter-bar">
          <div className="search">
            <Icon name="search" size={13} />
            <input placeholder="Search by service, error, commit, deployment…" />
            <span className="kbd">/</span>
          </div>

          <div className="seg">
            <button className={statusFilter==='open'?'active':''} onClick={()=>setStatusFilter('open')}>Open · {openCount}</button>
            <button className={statusFilter==='resolved'?'active':''} onClick={()=>setStatusFilter('resolved')}>Resolved · {resolvedCount}</button>
            <button className={statusFilter==='all'?'active':''} onClick={()=>setStatusFilter('all')}>All</button>
          </div>

          {/* TODO: wire to backend — filter chips from query params */}
          <button className="filter-chip active">
            <span>Severity</span><span className="val">SEV1, SEV2</span>
            <Icon name="x" size={11} className="ico x" />
          </button>
          <button className="filter-chip">
            <Icon name="services" size={11} />
            <span>Service</span><span className="val">any</span>
          </button>
          <button className="filter-chip">
            <Icon name="globe" size={11} />
            <span>Region</span><span className="val">ap-south-1</span>
          </button>
          <button className="filter-chip">
            <Icon name="user" size={11} />
            <span>Owner</span><span className="val">any</span>
          </button>
          <button className="filter-chip">
            <Icon name="plus" size={11} />
            <span>Add filter</span>
          </button>
          <div style={{ marginLeft: 'auto' }}>
            <button className="btn btn-ghost btn-sm">
              <Icon name="sliders" size={12} /> Columns
            </button>
          </div>
        </div>

        <div className="tbl">
          <div className="tbl-head">
            <div></div>
            <div>Incident</div>
            <div>Severity</div>
            <div>Confidence</div>
            <div>Status</div>
            {/* TODO: wire to backend — owner from PagerDuty integration */}
            <div>Owner</div>
            <div style={{ textAlign: 'right' }}>Age</div>
          </div>
          {filtered.map((inc) => {
            const isHero = inc.id === 'INC-2419';
            return (
              <div
                key={inc.id}
                className={'tbl-row ' + (isHero ? 'unread' : '')}
                onClick={() => onOpen(inc.id)}
              >
                <div>
                  <SeverityDotOnly sev={inc.severity} />
                </div>
                <div className="row-title">
                  <div className="t">{inc.title}</div>
                  <div className="s">
                    <span className="mono">{inc.id}</span>
                    <span style={{ color: 'var(--text-4)' }}>·</span>
                    <span className="row-svc">
                      <ServiceIcon name={inc.service} color={inc.serviceColor} />
                      {inc.service}
                    </span>
                    <span style={{ color: 'var(--text-4)' }}>·</span>
                    <span>{inc.region}</span>
                  </div>
                </div>
                <div><SeverityBadge sev={inc.severity} /></div>
                <div><Confidence value={inc.confidence} /></div>
                <div><StatusPill status={inc.status} /></div>
                <div>
                  <span className="row-flex">
                    <span className="avatar" style={{ width: 20, height: 20, fontSize: 9, background: inc.ownerColor }}>
                      {inc.owner.split(' ').map(s => s[0]).join('').slice(0,2)}
                    </span>
                    <span style={{ fontSize: 12 }}>{inc.owner}</span>
                  </span>
                </div>
                <div className="row-meta" style={{ textAlign: 'right' }}>{inc.age}</div>
              </div>
            );
          })}
        </div>

        <div className="pager">
          <span>Showing {filtered.length} of {incidents.length}</span>
          <span style={{ marginLeft: 'auto' }}>Auto-refresh every <span style={{color:'var(--text-1)'}}>30s</span></span>
          <span style={{ color: 'var(--text-4)' }}>·</span>
          <span>Updated 14:34:02 IST</span>
        </div>
      </div>

      {showNewIncident && <NewIncidentModal onClose={() => setShowNewIncident(false)} />}
    </>
  );
};

const SeverityDotOnly = ({ sev }) => {
  const colors = { SEV1: '#ef4444', SEV2: '#f5a524', SEV3: '#4c8dff', SEV4: '#5e6470' };
  return (
    <div style={{
      width: 8, height: 8, borderRadius: 4, background: colors[sev],
      boxShadow: sev === 'SEV1' ? '0 0 0 3px rgba(239,68,68,0.18)' : 'none'
    }}></div>
  );
};

window.IncidentList   = IncidentList;
window.SeverityDotOnly = SeverityDotOnly;
