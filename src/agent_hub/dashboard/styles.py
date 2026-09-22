"""Dashboard layout and shared workspace visual theme."""

_CSS = """\
body{font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
  padding:2rem;background:#020617;color:#e2e8f0;margin:0}
h1{color:#38bdf8;margin-bottom:0.25rem}
header{display:flex;justify-content:space-between;align-items:flex-start;gap:1rem}
.operator{display:flex;align-items:center;gap:0.6rem;color:#94a3b8;font-size:0.8rem}
.operator-email{color:#e2e8f0}
.operator-role{border:1px solid #334155;border-radius:999px;padding:0.15rem 0.45rem}
.operator a{color:#38bdf8;text-decoration:none}
.operator a:hover{text-decoration:underline}
nav{display:flex;flex-wrap:wrap;gap:0.5rem 1.5rem;margin-bottom:2rem}
nav a{color:#38bdf8;text-decoration:none}
nav a:hover{text-decoration:underline}
section{margin-bottom:2rem}
table{border-collapse:collapse;width:100%}
th,td{border:1px solid #334155;padding:0.5rem 0.75rem;text-align:left;vertical-align:top}
th{background:#0f172a;white-space:nowrap}
tr:hover td{background:#0f172a}
.badge{font-size:0.68rem;padding:0.1rem 0.35rem;border-radius:3px;
  margin:0.1rem 0.1rem 0 0;display:inline-block}
.badge-multi{background:#1f4a2e;color:#3fb950}
.badge-free{background:#2d1f6e;color:#a5a0ff}
.owner-filter{display:flex;gap:.4rem;align-items:center;flex-wrap:wrap;margin:.5rem 0;
  font-size:.85rem;color:#94a3b8}
.owner-chip{background:#1e293b;color:#e2e8f0;border:1px solid #334155;border-radius:999px;
  padding:.15rem .7rem;font-size:.8rem;cursor:pointer}
.owner-chip:hover{background:#334155}
.owner-chip[aria-pressed="true"]{background:#0369a1;border-color:#0369a1;color:#fff}
.owner-group th{text-align:left;background:#0f172a;color:#38bdf8;padding-top:.7rem}
.owner-chip.selected{background:#0369a1;border-color:#0369a1;color:#fff}
.tool-console{border:1px solid #334155;border-radius:6px;padding:.6rem;margin:.4rem 0;
  background:#020617}
.tool-console summary{cursor:pointer;color:#38bdf8}
.tool-console input[type=text]{width:100%;box-sizing:border-box;margin:.35rem 0}
.tool-result{white-space:pre-wrap;background:#010409;border:1px solid #334155;border-radius:4px;
  padding:.4rem;margin-top:.35rem;font-size:.85rem;max-height:14rem;overflow:auto}
.badge-tool{background:#1a2a3a;color:#79c0ff}
.badge-skill{background:#2a1a3a;color:#d2a8ff}
.badge-kind{background:#3a2a1a;color:#f0883e}
.status-active{color:#3fb950}
.status-idle{color:#d29922}
.status-degraded{color:#d29922}
.status-offline{color:#94a3b8}
.status-discovered{color:#38bdf8}
.lat{font-size:0.75rem;color:#94a3b8}
.lat span{color:#e2e8f0}
.model{font-size:0.75rem;color:#94a3b8;display:block;margin-top:0.15rem}
input,select{background:#0f172a;color:#e2e8f0;border:1px solid #334155;
  padding:0.4rem 0.6rem;border-radius:4px;margin-right:0.5rem}
button{background:#0369a1;color:#fff;border:none;padding:0.4rem 0.9rem;
  border-radius:4px;cursor:pointer}
button:hover{background:#075985}
button:disabled{cursor:wait;opacity:0.65}
button.selected{background:#1f4a2e;color:#3fb950;border:1px solid #3fb950}
.msg{color:#3fb950;margin-top:0.5rem}
.controls{display:flex;align-items:center;flex-wrap:wrap;gap:0.5rem;margin-bottom:1rem}
:where(a,button,input,select,textarea):focus-visible{outline:3px solid #38bdf8;
  outline-offset:2px}
.htmx-indicator{opacity:0}
#global-progress{position:fixed;z-index:100;top:0;left:0;right:0;padding:0.35rem 1rem;
  text-align:center;background:#0369a1;color:#fff;pointer-events:none;
  opacity:0;transition:opacity 120ms linear}
/* Show-delay: the dashboard polls a few regions every 1-5s, and each poll
   toggles this global indicator. A 600ms delay before it fades in means those
   quick requests finish first and never flash the banner, while a real
   navigation or a slow action still surfaces it. */
.htmx-request#global-progress{opacity:1;transition-delay:600ms}
@media (prefers-reduced-motion:reduce){#global-progress{transition:none}}
#global-feedback{position:fixed;z-index:101;right:1rem;bottom:1rem;max-width:28rem;
  border:1px solid #f85149;border-radius:6px;padding:0.75rem 1rem;background:#2d1117;
  color:#ff7b72;box-shadow:0 4px 20px #010409}
#global-feedback:empty{display:none}
form.htmx-request{opacity:0.78}
"""

_CSS_EXTRA = """\
textarea{background:#0f172a;color:#e2e8f0;border:1px solid #334155;padding:0.4rem 0.6rem;
  border-radius:4px;width:100%;box-sizing:border-box;font-family:monospace;resize:vertical}
label{display:block;color:#94a3b8;font-size:0.8rem;margin-top:0.75rem;
  margin-bottom:0.2rem}
.field-row{display:grid;grid-template-columns:1fr 1fr;gap:1rem}
.form-section{background:#0f172a;border:1px solid #334155;border-radius:6px;
  padding:1.25rem;margin-bottom:1.5rem}
.form-section h3{margin:0 0 1rem;color:#38bdf8}
input[type=number]{width:6rem}
.doc-page{max-width:980px;line-height:1.55}
.doc-page h2{color:#38bdf8;margin-bottom:0.35rem}
.doc-page h3{color:#e2e8f0;margin:1.25rem 0 0.4rem}
.doc-page p{color:#e2e8f0}
.doc-page ul{padding-left:1.4rem}
.doc-page li{margin:0.35rem 0}
.doc-muted{color:#94a3b8}
.doc-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:1rem}
.doc-card{background:#0f172a;border:1px solid #334155;border-radius:6px;padding:1rem}
.doc-card h3{margin-top:0;color:#38bdf8}
.doc-flow{background:#010409;border:1px solid #334155;border-radius:6px;
  padding:1rem;white-space:pre-wrap;overflow:auto}
.spend-ok{color:#3fb950}
.spend-warn{color:#d29922}
.spend-over{color:#f85149}
.audit-success{color:#3fb950}
.audit-failure{color:#f85149}
.section-heading{display:flex;justify-content:space-between;align-items:center;gap:1rem}
.section-heading h2,.section-heading p{margin:0 0 0.35rem}
.action-link{display:inline-block;color:#38bdf8;border:1px solid #334155;border-radius:4px;
  padding:0.4rem 0.7rem;text-decoration:none;white-space:nowrap}
.action-link:hover{border-color:#38bdf8;background:#0f172a;text-decoration:none}
.action-link.primary{color:#fff;background:#0369a1;border-color:#0369a1}
.overview-grid{display:grid;grid-template-columns:repeat(4,minmax(120px,1fr));gap:0.75rem;
  margin-top:0.75rem}
.overview-card{background:#0f172a;border:1px solid #334155;border-radius:6px;padding:1rem}
.overview-value{font-size:1.8rem;display:block;color:#e2e8f0}
.overview-label{font-size:0.75rem;color:#94a3b8}
.overview-good .overview-value{color:#3fb950}
.overview-warn .overview-value{color:#d29922}
.overview-muted .overview-value{color:#94a3b8}
.attention-panel{border:1px solid #5a4217;background:#17130b;border-radius:6px;padding:1rem}
.attention-count{font-size:0.75rem;background:#5a4217;color:#f2cc60;border-radius:999px;
  padding:0.15rem 0.45rem;vertical-align:middle}
.attention-list{display:grid;gap:0.6rem;margin-top:0.75rem}
.attention-item{display:flex;justify-content:space-between;align-items:center;gap:1rem;
  background:#020617;border:1px solid #334155;border-radius:4px;padding:0.75rem}
.attention-status{font-size:0.72rem;margin-left:0.5rem}
.attention-detail{font-size:0.75rem;color:#94a3b8;margin-top:0.25rem}
.attention-clear{display:flex;gap:0.75rem;align-items:center;border:1px solid #1f4a2e;
  background:#0e1711;border-radius:6px;padding:0.8rem 1rem;color:#3fb950}
.attention-clear span{color:#94a3b8;font-size:0.8rem}
.empty-state{text-align:center;max-width:650px;margin:4rem auto;padding:2rem;
  border:1px dashed #334155;border-radius:8px;background:#0f172a}
.empty-state h2{color:#38bdf8}.empty-state p{line-height:1.6;color:#94a3b8}
.empty-icon{font-size:2.5rem;color:#38bdf8}.empty-actions{display:flex;gap:0.75rem;
  justify-content:center;margin-top:1.25rem}
@media (max-width:760px){
  body{padding:1rem}
  header{align-items:flex-start;flex-direction:column}
  .operator{align-items:flex-start;flex-wrap:wrap}
  nav{gap:0.75rem 1.25rem;margin:1rem 0 1.5rem}
  .overview-grid{grid-template-columns:repeat(2,minmax(0,1fr))}
  .field-row{grid-template-columns:1fr}
  .attention-item,.section-heading{align-items:flex-start;flex-direction:column}
  .empty-actions{align-items:stretch;flex-direction:column}
  table{display:block;max-width:100%;overflow-x:auto}
  input:not([type=checkbox]),select,textarea{box-sizing:border-box;max-width:100%;width:100%}
  form[style*="display:flex"],form[style*="display:inline-flex"]{align-items:stretch!important;
    flex-direction:column}
  button,.action-link{min-height:44px}
}
@media (prefers-reduced-motion:reduce){*{scroll-behavior:auto!important}}
"""

WORKSPACE_CSS = """
:root{color-scheme:dark}
body{font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
  background:radial-gradient(ellipse at top left,#172b46,transparent 65%),#020617;
  color:#e2e8f0;min-height:100vh;box-sizing:border-box;max-width:1600px;margin:auto;
  line-height:1.5}
a{color:#38bdf8}h1{letter-spacing:-.035em;color:#f1f5f9}
nav{padding:.75rem 0;border-bottom:1px solid #334155}
nav a{padding:.45rem .65rem;border-radius:.5rem}
nav a:hover{background:#1e293b;text-decoration:none}
button,input,select,textarea{font:inherit}
button,.action-link{border-radius:.6rem;min-height:44px;box-sizing:border-box}
.action-link{display:inline-flex;align-items:center;justify-content:center}
button{background:#0369a1}button:hover{background:#075985}
input,select,textarea{background:#0f172a;color:#e2e8f0;border:1px solid #334155;
  padding:.55rem .7rem;border-radius:.5rem}
:where(a,button,input,select,textarea):focus-visible{outline:3px solid #38bdf8;outline-offset:3px}
.agent-card,.form-section,.doc-card,.overview-card,.workspace-panel{
  background:#0f172a;border-radius:1rem;box-shadow:0 10px 30px #00000035}
.agent-card{display:flex;flex-direction:column;padding:1.25rem}
.agent-card:hover{border-top-color:#38bdf8;border-right-color:#38bdf8;border-bottom-color:#38bdf8}
.agent-card-title a{color:#f1f5f9;font-size:1.1rem}
.agent-card-actions{margin-top:auto;padding-top:1rem}
.agent-capabilities{padding-bottom:1rem}.agent-kind-icon{border-radius:.75rem;background:#172b46}
.badge-kind{background:#1e293b;color:#bae6fd}.status-healthy{color:#4ade80}
.workspace-intro{display:flex;justify-content:space-between;align-items:center;gap:1rem;
  flex-wrap:wrap;margin:1rem 0 2rem}
.workspace-intro h2{font-size:1.8rem;margin:0}.workspace-intro p{color:#94a3b8;margin:.4rem 0}
.workspace-panel{padding:1.25rem;border:1px solid #334155;margin:1rem 0}
.workspace-panel h2{margin-top:0;color:#38bdf8}.workspace-panel summary{cursor:pointer}
#voicestate{min-width:0;flex-wrap:wrap}#voicehint{flex-basis:100%}
@media(max-width:760px){body{padding:1rem}.workspace-intro{align-items:stretch;
  flex-direction:column}.workspace-panel{padding:1rem}}
@media(prefers-reduced-motion:reduce){.voice-live #voicedot{animation:none}}
"""

DASHBOARD_CSS = _CSS + _CSS_EXTRA + WORKSPACE_CSS
