// New Incident modal — capture-only for now.
// TODO: wire to backend — POST /incidents with this payload once the endpoint exists.
const NI_INCIDENT_LOOKBACK = ['15 minutes', '30 minutes', '1 hour', '3 hours', '6 hours'];
const NI_FULL_LOOKBACK     = ['6 hours', '12 hours', '24 hours', '48 hours', '7 days'];
const NI_SEVERITIES        = ['SEV1', 'SEV2', 'SEV3', 'SEV4'];

const NewIncidentModal = ({ onClose }) => {
  const [form, setForm] = React.useState({
    name: '',
    severity: 'SEV2',
    leadEngineer: '',
    service: '',
    incidentLookback: '30 minutes',
    fullLookback: '24 hours',
    notes: '',
  });
  const set = (key) => (e) => setForm(f => ({ ...f, [key]: e.target.value }));

  // Escape closes; lock page scroll while open
  React.useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    document.body.style.overflow = 'hidden';
    return () => {
      window.removeEventListener('keydown', onKey);
      document.body.style.overflow = '';
    };
  }, [onClose]);

  const canCreate = form.name.trim().length > 0;
  const submit = () => {
    if (!canCreate) return;
    console.log('New incident (backend not wired yet):', form);
    onClose();
  };

  return (
    <div className="modal-overlay" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="modal" role="dialog" aria-modal="true" aria-label="Create new incident">
        <div className="modal-head">
          <div>
            <div className="modal-title">New incident</div>
            <div className="modal-sub">Kick off a root cause analysis for a new incident</div>
          </div>
          <button className="modal-close" onClick={onClose} aria-label="Close">
            <Icon name="x" size={14} />
          </button>
        </div>

        <div className="modal-body">
          <div className="field">
            <label className="field-label">Incident name <span className="req">*</span></label>
            <input
              type="text"
              autoFocus
              placeholder="e.g. Checkout latency spike in ap-south-1"
              value={form.name}
              onChange={set('name')}
              onKeyDown={(e) => { if (e.key === 'Enter') submit(); }}
            />
          </div>

          <div className="field-row">
            <div className="field">
              <label className="field-label">Severity</label>
              <div className="seg" style={{ alignSelf: 'flex-start' }}>
                {NI_SEVERITIES.map(sev => (
                  <button
                    key={sev}
                    className={form.severity === sev ? 'active' : ''}
                    onClick={() => setForm(f => ({ ...f, severity: sev }))}
                  >{sev}</button>
                ))}
              </div>
            </div>
            <div className="field">
              <label className="field-label"><Icon name="user" size={11} /> Lead engineer</label>
              <input
                type="text"
                placeholder="e.g. Priya Sharma"
                value={form.leadEngineer}
                onChange={set('leadEngineer')}
              />
            </div>
          </div>

          <div className="field">
            <label className="field-label"><Icon name="services" size={11} /> Affected service</label>
            <input
              type="text"
              placeholder="e.g. checkout-service (optional)"
              value={form.service}
              onChange={set('service')}
            />
          </div>

          <div className="field-row">
            <div className="field">
              <label className="field-label"><Icon name="clock" size={11} /> Incident lookback window</label>
              <select value={form.incidentLookback} onChange={set('incidentLookback')}>
                {NI_INCIDENT_LOOKBACK.map(o => <option key={o} value={o}>{o}</option>)}
              </select>
              <div className="field-hint">Window before incident start scanned for anomalies and changes</div>
            </div>
            <div className="field">
              <label className="field-label"><Icon name="history" size={11} /> Full lookback window</label>
              <select value={form.fullLookback} onChange={set('fullLookback')}>
                {NI_FULL_LOOKBACK.map(o => <option key={o} value={o}>{o}</option>)}
              </select>
              <div className="field-hint">Telemetry history pulled for baseline computation</div>
            </div>
          </div>

          <div className="field">
            <label className="field-label">Notes</label>
            <textarea
              placeholder="Anything the on-call should know — symptoms, suspected changes, links… (optional)"
              value={form.notes}
              onChange={set('notes')}
            />
          </div>
        </div>

        <div className="modal-foot">
          <span className="modal-foot-hint">Hyperion will begin causal analysis once telemetry is ingested</span>
          <button className="btn" onClick={onClose}>Cancel</button>
          <button className="btn btn-primary" disabled={!canCreate} onClick={submit}>
            <Icon name="plus" size={13} />
            <span>Create incident</span>
          </button>
        </div>
      </div>
    </div>
  );
};

window.NewIncidentModal = NewIncidentModal;
