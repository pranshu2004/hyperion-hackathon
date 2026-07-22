// Incidents that trigger a live backend fetch when opened, keyed by incident id
const DEMO_LIVE_MAP = {
  'INC-2420': { url: '/demo/sc001?use_llm=true', msg: 'Running reasoning pipeline with LLM analysis…'        },
  'INC-2421': { url: '/demo/sc002?use_llm=true', msg: 'Running database RCA with LLM analysis…'              },
  'INC-2422': { url: '/demo/sc003?use_llm=true', msg: 'Running dependency RCA with LLM analysis…'            },
  'INC-2423': { url: '/demo/sc004?use_llm=true', msg: 'Running configuration RCA with LLM analysis…'         },
};

// Main app — hash router + live RCA fetch on page load
const App = () => {
  const [incidents, setIncidents] = React.useState(window.INCIDENTS);
  const [loading, setLoading]     = React.useState(true);
  const [route, setRoute]         = React.useState({ type: 'detail', id: 'INC-2419' });

  // Honour deep-links on first load
  React.useEffect(() => {
    const hash = window.location.hash;
    if (hash.startsWith('#/incident/')) {
      setRoute({ type: 'detail', id: hash.replace('#/incident/', '') });
    } else if (hash === '#/list' || hash === '#/incidents') {
      setRoute({ type: 'list' });
    }
  }, []);

  // Auto-fetch live RCA result and overlay onto INC-2419 mock data
  React.useEffect(() => {
    fetch('/demo/sc001', { method: 'POST' })
      .then(r => {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(result => {
        setIncidents(prev => prev.map(inc =>
          inc.id === 'INC-2419'
            ? window.buildIncidentFromResult(inc, result)
            : inc
        ));
      })
      .catch(err => {
        console.warn('Live RCA fetch failed, using mock data:', err.message);
      })
      .finally(() => setLoading(false));
  }, []);

  const openIncident = (id) => {
    setRoute({ type: 'detail', id });
    window.location.hash = '#/incident/' + id;
    window.scrollTo(0, 0);

    // Lazy-fetch live RCA for any incident wired to a demo endpoint
    const cfg = DEMO_LIVE_MAP[id];
    if (!cfg) return;

    const current = incidents.find(i => i.id === id);
    if (current && current.loading) return; // already in flight
    if (current && current.evidence && current.evidence.length > 0) return; // already loaded

    setIncidents(prev => prev.map(inc =>
      inc.id === id
        ? { ...inc, loading: true, verdict: { ...inc.verdict, summary: cfg.msg, window: 'reasoning · analyzing' } }
        : inc
    ));

    fetch(cfg.url, { method: 'POST' })
      .then(r => {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(result => {
        setIncidents(prev => prev.map(inc =>
          inc.id === id
            ? window.buildIncidentFromResultV2(inc, result)
            : inc
        ));
      })
      .catch(err => {
        console.warn('RCA fetch failed for', id, ':', err.message);
        setIncidents(prev => prev.map(inc =>
          inc.id === id
            ? { ...inc, loading: false, verdict: { ...inc.verdict, summary: 'Analysis failed — ' + err.message, window: 'reasoning · error' } }
            : inc
        ));
      });
  };
  const goList = () => {
    setRoute({ type: 'list' });
    window.location.hash = '#/list';
    window.scrollTo(0, 0);
  };

  let body;
  if (route.type === 'detail') {
    const inc  = incidents.find(i => i.id === route.id);
    const full = inc && inc.verdict ? inc : incidents[0];
    body = (
      <>
        {/* Loading bar while POST /demo/sc001 is in flight */}
        {loading && (
          <div style={{
            position: 'fixed', top: 46, left: 224, right: 0, height: 2,
            background: 'linear-gradient(90deg, var(--accent), var(--accent-2))',
            opacity: 0.85, zIndex: 100,
          }} />
        )}
        <RcaDetail incident={full} onBack={goList} />
      </>
    );
  } else {
    body = <IncidentList onOpen={openIncident} incidents={incidents} />;
  }

  return (
    <div className="app">
      <Sidebar active="incidents" onNavigate={() => goList()} />
      <main className="main">{body}</main>
    </div>
  );
};

ReactDOM.createRoot(document.getElementById('root')).render(<App />);
