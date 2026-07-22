// Shared chrome: sidebar + topbar
const Sidebar = ({ active, onNavigate }) => {
  const items = [
    { id: 'incidents', label: 'Incidents', icon: 'alert', count: 5, dotColor: '#ef4444' },
    { id: 'investigations', label: 'Investigations', icon: 'search', count: 2 },
    { id: 'services', label: 'Services', icon: 'services' },
    { id: 'deployments', label: 'Deployments', icon: 'rocket' },
    { id: 'changes', label: 'Change feed', icon: 'changes' },
    { id: 'history', label: 'History', icon: 'history' },
  ];
  const lower = [
    { id: 'runbooks', label: 'Runbooks', icon: 'book' },
    { id: 'integrations', label: 'Integrations', icon: 'share' },
    { id: 'settings', label: 'Settings', icon: 'settings' },
  ];
  return (
    <aside className="sidebar">
      <div className="brand">
        <div className="brand-mark"></div>
        <div className="brand-name">Hyperion</div>
        <div className="brand-env">PROD</div>
      </div>

      <div className="nav-section">Operate</div>
      {items.map(it => (
        <div
          key={it.id}
          className={'nav-item ' + (active === it.id ? 'active' : '')}
          onClick={() => onNavigate && onNavigate(it.id)}
        >
          <Icon name={it.icon} size={14} />
          <span>{it.label}</span>
          {it.count !== undefined && <span className="count">{it.count}</span>}
        </div>
      ))}

      <div className="nav-section">Reference</div>
      {lower.map(it => (
        <div key={it.id} className="nav-item">
          <Icon name={it.icon} size={14} />
          <span>{it.label}</span>
        </div>
      ))}

      <div className="sidebar-footer">
        <div className="avatar">PD</div>
        <div className="col" style={{ minWidth: 0 }}>
          <div className="user-name">Pranshu Dasgupta</div>
          <div className="user-team">logiq · payments</div>
        </div>
      </div>
    </aside>
  );
};

const Topbar = ({ crumbs, actions }) => (
  <header className="topbar">
    <div className="crumbs">
      {crumbs.map((c, i) => (
        <React.Fragment key={i}>
          {i > 0 && <span className="sep">/</span>}
          <span className={i === crumbs.length - 1 ? 'here' : ''}>{c}</span>
        </React.Fragment>
      ))}
    </div>
    <div className="topbar-actions">
      {actions || (
        <>
          <button className="btn btn-ghost">
            <Icon name="bell" size={14} />
          </button>
          <button className="btn btn-ghost">
            <Icon name="help" size={14} />
          </button>
          <div style={{ width: 1, height: 18, background: 'var(--border-2)', margin: '0 4px' }}></div>
          <button className="btn">
            <Icon name="command" size={13} />
            <span>Open command</span>
            <span className="kbd">⌘K</span>
          </button>
        </>
      )}
    </div>
  </header>
);

const ServiceIcon = ({ name, color }) => {
  const letter = (name || '?').charAt(0).toUpperCase();
  return (
    <span className="svc-ico" style={{ background: color }}>{letter}</span>
  );
};

const SeverityBadge = ({ sev }) => {
  const cls = 'sev sev-' + sev.toLowerCase();
  return (
    <span className={cls}>
      <span className="dot"></span>{sev}
    </span>
  );
};

const StatusPill = ({ status }) => {
  const cls = 'status-pill status-' + status;
  return (
    <span className={cls}>
      <span className="dot"></span>{status.charAt(0).toUpperCase() + status.slice(1)}
    </span>
  );
};

const Confidence = ({ value }) => {
  const tier = value >= 85 ? 'high' : (value >= 65 ? 'med' : 'low');
  return (
    <div className={'confidence ' + tier}>
      <div className="confidence-bar"><span style={{ width: value + '%' }}></span></div>
      <span className="num">{value}%</span>
    </div>
  );
};

window.Sidebar = Sidebar;
window.Topbar = Topbar;
window.ServiceIcon = ServiceIcon;
window.SeverityBadge = SeverityBadge;
window.StatusPill = StatusPill;
window.Confidence = Confidence;
