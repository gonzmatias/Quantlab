"use strict";
const $ = (id) => document.getElementById(id);
const token = document.querySelector('meta[name="session-token"]').content;
const stages = ["researcher_node", "prototyper_node", "quant_validator_node", "stress_test_node", "production_coder_node", "reporter_node"];
const descriptions = {
  researcher_node: "Buscando fuentes, comprobando compatibilidad y formulando una hipótesis verificable con datos de entrenamiento.",
  prototyper_node: "El agente convierte la hipótesis en un prototipo reproducible.",
  quant_validator_node: "Evaluando entrenamiento, prueba fuera de muestra y filtros estadísticos.",
  stress_test_node: "Simulando $100 con costos normales, dobles y triples.",
  production_coder_node: "Preparando el módulo de simulación con las reglas que superaron las pruebas.",
  reporter_node: "El agente de reportes está documentando la evidencia y la decisión."
};
let mode = "public", revision = -1, snapshot = null, selectedAttempt = null, selectedReport = null;
let displayedRun = null, chartKey = "oos", reportRequest = 0, starting = false;
let settings = {max_iterations:0,dsr_trial_budget:100,bars_per_year:252,min_trades:30,max_drawdown:.25,commission_rate:.001,
  fixed_commission:0,spread_bps:5,slippage_bps:5,min_notional:5,quantity_step:.00001};
const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (c)=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const pct = (v) => Number.isFinite(v) ? `${(v*100).toFixed(2)}%` : "—";
const num = (v) => Number.isFinite(v) ? v.toFixed(2) : "—";
const money = (v) => new Intl.NumberFormat("es-PY",{style:"currency",currency:"USD",maximumFractionDigits:2}).format(v);
function renderResearch(report) {
  const research = report.research || {}, brief = research.brief;
  if (!brief) return `<h4 class="report-section-title">Investigación</h4><p>${escapeHtml(research.summary || research.rejection_reason || 'Sin ficha de investigación.')}</p>`;
  const labels = {mechanism:'Mecanismo',prediction:'Predicción',falsification:'Criterio de descarte',adaptation:'Adaptación',parameter_reasoning:'Justificación de parámetros',compatibility_reason:'Compatibilidad'};
  const sources = (brief.sources || []).map(source => {
    let safe = false;
    try { const url = new URL(source.url); safe = ['https:','http:'].includes(url.protocol) && !url.username && !url.password; } catch {}
    const title = escapeHtml(source.title);
    return `<li>${safe ? `<a href="${escapeHtml(source.url)}" target="_blank" rel="noopener noreferrer">${title}</a>` : title}<p>${escapeHtml(source.finding)}</p><small>${escapeHtml(source.source_kind)} · ${escapeHtml(source.limitations)}</small></li>`;
  }).join('');
  const comparison = report.quant_metrics?.baseline_comparison;
  return `<h4 class="report-section-title">Investigación y fuentes</h4>${Object.entries(labels).map(([key,label])=>`<p><strong>${label}:</strong> ${escapeHtml(brief[key])}</p>`).join('')}<ul>${sources}</ul><p>Una URL trazable no verifica la afirmación publicada. Los filtros del simulador no certifican el mecanismo.</p>${comparison ? `<p>Comparación de entrenamiento con la familia sin filtros: diferencia de retorno ${pct(comparison.net_return_difference)}; diferencia de Sharpe ${num(comparison.sharpe_difference)}. Diagnóstico, no criterio de aprobación.</p>` : ''}`;
}
function error(message) { $("formError").textContent = message; $("formError").classList.toggle("hidden", !message); }
async function api(path, payload) {
  const options = payload === undefined ? {} : {method:"POST",headers:{"Content-Type":"application/json","X-Session-Token":token},body:JSON.stringify(payload)};
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Error ${response.status}`);
  return data;
}
function setMode(value) {
  if (snapshot?.busy || starting) return;
  mode = value;
  for (const option of ["demo","public"]) {
    $(option+"Mode").classList.toggle("selected",value===option);
    $(option+"Mode").setAttribute("aria-pressed",String(value===option));
  }
  $("sourceDescription").innerHTML = value==="public" ? '<strong>Dukascopy + Coinbase · velas diarias</strong><span>Desde 2018 · descarga automática · sin Excel</span>' : '<strong>Demo offline · cinco series sintéticas</strong><span>Sin clave API · no puede aprobar estrategias reales</span>';
}
$("demoMode").onclick = () => setMode("demo"); $("publicMode").onclick = () => setMode("public");
$("settingsButton").onclick = () => $("settingsDialog").showModal();
$("closeSettings").onclick = () => $("settingsDialog").close();
$("settingsForm").onsubmit = (event) => {
  event.preventDefault();
  for (const [key,value] of new FormData(event.target)) settings[key] = Number(value);
  settings.max_drawdown /= 100;
  for (const input of document.querySelectorAll("[data-cost]")) {
    assetSettings[input.dataset.asset][input.dataset.cost] = Number(input.value) / (input.dataset.cost === "commission_rate" ? 100 : 1);
  }
  $("settingsDialog").close();
  if (!snapshot?.state.iteration_count) $("attemptCounter").textContent = "0 intentos · sin límite";
};
$("startButton").onclick = async () => {
  if (starting || snapshot?.busy) return;
  error(""); starting = true; $("startButton").disabled = true;
  try {
    const payload = {mode,settings,asset_settings:assetSettings,model:$("modelInput").value.trim(),api_key:$("apiKeyInput").value.trim()};
    await api("/api/start", payload);
    $("apiKeyInput").value = "";
    revision = -1;
    selectedAttempt = null; selectedReport = null; reportRequest++;
    $("reportPanel").classList.add("hidden");
    await refresh();
  } catch (exc) { error(exc.message); }
  finally { starting = false; $("startButton").disabled = Boolean(snapshot?.busy); }
};
$("stopButton").onclick = async () => {
  try { await api("/api/stop",{}); revision = -1; await refresh(); } catch (exc) { error(exc.message); }
};
$("reportLookup").onsubmit = (event) => {
  event.preventDefault();
  loadReport(Number($("reportNumber").value), true);
};
function metric(id, text, value, signed = false) {
  $(id).textContent = text;
  $(id).classList.toggle("positive", signed && Number.isFinite(value) && value > 0);
  $(id).classList.toggle("negative", signed && Number.isFinite(value) && value < 0);
}
function renderMetrics(q = {}) {
  const oos = q.out_of_sample || {};
  metric("returnMetric",pct(oos.net_return),oos.net_return,true); metric("sharpeMetric",num(oos.sharpe),oos.sharpe,true);
  metric("dsrMetric",pct(q.dsr),q.dsr); metric("drawdownMetric",pct(oos.max_drawdown),oos.max_drawdown);
  $("metricsContext").textContent = selectedReport ? `REPORTE #${selectedAttempt} · ${selectedReport.strategy_name}` : `MÉTRICAS · ${snapshot?.state.selected_asset || snapshot?.state.active_asset || "PRUEBA ACTUAL"}`;
  $("followCurrent").classList.toggle("hidden", !selectedReport || !snapshot?.busy);
}
$("followCurrent").onclick = () => {
  selectedAttempt = null; selectedReport = null; reportRequest++;
  $("reportPanel").classList.add("hidden");
  if(snapshot) render(snapshot);
};
function render(data) {
  snapshot = data;
  const s = data.state, busy = data.busy, lifecycle = s.lifecycle;
  if (displayedRun !== data.output) {
    displayedRun = data.output; selectedAttempt = null; selectedReport = null; reportRequest++;
    $("reportPanel").classList.add("hidden");
  }
  const labels = {IDLE:"LISTO PARA INICIAR",STARTING:"PREPARANDO DATOS",RUNNING:"INVESTIGACIÓN EN CURSO",COMPLETED:"APROBADO EN SIMULACIÓN",EXHAUSTED:"BÚSQUEDA FINALIZADA · SIN APROBACIÓN",CANCELLED:"DETENIDO POR EL USUARIO",ERROR:"ERROR DE EJECUCIÓN"};
  let label = labels[lifecycle] || "EN ESPERA";
  if (busy && s.status === "REJECTED") label = "INTENTO RECHAZADO · PREPARANDO ITERACIÓN";
  if (busy && data.stop_requested) label = "DETENIENDO INVESTIGACIÓN";
  $("statusBadge").textContent = label;
  $("statusBadge").className = "status-badge " + (lifecycle==="ERROR"?"error":lifecycle==="COMPLETED"?"success":busy?"running":lifecycle==="EXHAUSTED"?"rejected":"");
  $("attemptCounter").textContent = data.max_iterations > 0 ? `${s.iteration_count} / ${data.max_iterations} intentos` : `${s.iteration_count} intentos · sin límite`;
  $("strategyName").textContent = lifecycle === "IDLE" ? "Tu próxima hipótesis empieza aquí" : s.strategy_name;
  let description = descriptions[s.current_stage] || "Cada estrategia se prueba en los cinco activos antes de iterar.";
  if (lifecycle === "EXHAUSTED") description = "Se alcanzó el límite de intentos. Revisa los motivos y los reportes de cada prueba.";
  if (lifecycle === "COMPLETED") description = "La estrategia pasó los filtros. Su reporte completo está disponible más abajo.";
  if (lifecycle === "CANCELLED") description = "Detenida por tu solicitud. Los reportes completados permanecen disponibles.";
  if (lifecycle === "ERROR") description = data.error || s.logs.at(-1) || "La ejecución encontró un error operativo.";
  if (busy && data.stop_requested) description = "Cancelando el cálculo o la espera del modelo y conservando los reportes completados.";
  $("stageDescription").textContent = description;
  document.querySelector(".status-panel").classList.toggle("working",busy);
  $("startButton").disabled = busy || starting; $("startButton").textContent = busy ? "Investigando…" : s.iteration_count ? "↻ Nueva investigación" : "▶ Iniciar investigación";
  $("stopButton").classList.toggle("hidden",!busy); $("stopButton").disabled = data.stop_requested;
  for (const id of ["settingsButton","demoMode","publicMode"]) $(id).disabled = busy;
  $("liveLabel").textContent = busy ? "● EN VIVO" : "EN ESPERA"; $("liveLabel").classList.toggle("active",busy);
  document.querySelectorAll(".pipeline > div").forEach((element) => {
    const name = element.dataset.stage;
    const completed = name === "researcher_node" ? Boolean(s.hypothesis?.family) : name === "prototyper_node" ? Boolean(s.quant_metrics?.in_sample) || stages.indexOf(s.current_stage)>1 && !!s.hypothesis?.family : name === "quant_validator_node" ? !!s.quant_metrics?.in_sample : name === "stress_test_node" ? !!s.stress_metrics?.scenarios : name === "production_coder_node" ? s.status === "APPROVED" : (s.reports || []).some(r=>r.attempt===s.iteration_count);
    element.classList.toggle("active", busy && s.current_stage === name);
    element.classList.toggle("done", completed && !(busy && s.current_stage===name));
    element.classList.toggle("skipped", s.current_stage==="reporter_node" && !completed && name!=="reporter_node");
  });
  renderAssets(selectedReport || s);
  renderMetrics(selectedReport?.quant_metrics || s.quant_metrics || {});
  $("activityLog").innerHTML = s.logs.length ? [...s.logs].reverse().map((line,index)=>`<div class="log-item ${/rechaz|insuficiente|límite|inválid/i.test(line)?"reject":""}"><small>EVENTO ${String(s.logs.length-index).padStart(2,"0")}</small>${escapeHtml(line)}</div>`).join("") : '<div class="activity-empty"><span>◷</span><p>El motor está preparado.<br>Inicia una investigación para ver su progreso.</p></div>';
  const reports = s.reports || [];
  $("historyCount").textContent = s.report_count || reports.length;
  $("historyDescription").textContent = s.report_count > reports.length ? `Mostrando los últimos ${reports.length} reportes. Abre cualquier intento anterior por su número.` : "Cada hipótesis conserva sus resultados, incluso cuando se rechaza.";
  $("historyRows").innerHTML = reports.length ? reports.map(r=>`<tr data-attempt="${r.attempt}" class="${selectedAttempt===r.attempt?'selected':''}"><td>#${String(r.attempt).padStart(2,"0")}</td><td class="strategy-cell">${escapeHtml(r.strategy_name)}<small> · ${escapeHtml(r.selected_asset || "")}</small></td><td class="${r.oos.net_return>0?'positive':r.oos.net_return<0?'negative':''}">${pct(r.oos.net_return)}</td><td>${pct(r.dsr)}</td><td><span class="result-badge ${r.outcome==='APPROVED'?'approved':''}">${r.outcome==='APPROVED'?'APROBADO':'RECHAZADO'}</span></td><td><button class="view-report" data-attempt="${r.attempt}">Ver reporte ↗</button></td></tr>`).join("") : '<tr><td colspan="6" class="table-empty">Todavía no hay pruebas. Los reportes aparecerán aquí automáticamente.</td></tr>';
  $("historyRows").querySelectorAll("button").forEach(button=>button.onclick=()=>loadReport(Number(button.dataset.attempt),true));
  renderCharts(selectedReport?.charts || s.charts || {});
  const approved = reports.find(r=>r.outcome==="APPROVED");
  if (!busy && reports.length && selectedAttempt === null) loadReport((approved || [...reports].reverse().find(r=>Number.isFinite(r.dsr)) || reports.at(-1)).attempt, Boolean(approved));
}
function renderCharts(charts) {
  const entries = Object.entries(charts);
  if (!charts[chartKey]) chartKey = entries[0]?.[0] || "oos";
  $("chartSelect").innerHTML = entries.length ? entries.map(([key,value])=>`<option value="${escapeHtml(key)}">${escapeHtml(value.label)}</option>`).join("") : '<option value="oos">Validación OOS</option>';
  $("chartSelect").value = chartKey;
  const series = charts[chartKey];
  if (!series?.equity?.length) {
    $("equityChart").className = "chart empty";
    $("equityChart").innerHTML = '<div class="empty-chart-icon">∿</div><strong>Primero, los datos.</strong><span>Esta prueba aún no tiene una curva disponible.</span>';
    $("drawdownChart").innerHTML = ""; $("chartCaption").textContent = "La curva aparecerá al completar el primer backtest."; $("chartRange").textContent = "—";
    return;
  }
  $("equityChart").className = "chart";
  $("chartCaption").textContent = `${selectedAttempt ? `Intento #${selectedAttempt} · ` : ""}${series.label} · ${snapshot.synthetic?'datos sintéticos':'datos públicos'}`;
  plot($("equityChart"),series.equity,series.dates,false);
  plot($("drawdownChart"),series.drawdown,series.dates,true);
  $("chartRange").textContent = "PASA EL CURSOR PARA EXPLORAR";
}
$("chartSelect").onchange = () => { chartKey = $("chartSelect").value; renderCharts(selectedReport?.charts || snapshot?.state.charts || {}); };
function plot(container, values, dates, drawdown) {
  const width=800,height=drawdown?75:250,left=64,right=14,top=15,bottom=drawdown?8:32;
  const min = drawdown ? 0 : Math.min(...values), max = Math.max(...values);
  const span = max-min || Math.max(Math.abs(max)*.02,1);
  const x = i => left+i*(width-left-right)/Math.max(1,values.length-1);
  const y = v => drawdown ? top+(v-min)/span*(height-top-bottom) : height-bottom-(v-min)/span*(height-top-bottom);
  const points=values.map((v,i)=>`${x(i).toFixed(2)},${y(v).toFixed(2)}`).join(" ");
  const baseline=drawdown?top:height-bottom;
  const fill=`${left},${baseline} ${points} ${x(values.length-1)},${baseline}`;
  const grid=Array.from({length:drawdown?2:5},(_,i)=>{
    const value=min+span*i/(drawdown?1:4), yy=y(value);
    return `<line x1="${left}" x2="${width-right}" y1="${yy}" y2="${yy}" stroke="#edf1f3" stroke-dasharray="3 4"/><text x="${left-10}" y="${yy+3}" text-anchor="end" fill="#95a5ae" font-size="10">${drawdown?pct(-value):Math.round(value).toLocaleString('es-PY')}</text>`;
  }).join("");
  const color=drawdown?"#bd9d76":"#2b967b";
  container.innerHTML=`<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="${drawdown?'Drawdown':'Capital en USD'}"><title>${drawdown?'Caída desde máximos':'Capital neto de costos'}</title>${grid}<polygon points="${fill}" fill="${drawdown?'#f6ede0':'#e9f6ef'}" opacity=".8"/><polyline points="${points}" fill="none" stroke="${color}" stroke-width="${drawdown?1.4:2.2}" vector-effect="non-scaling-stroke"/>${drawdown?'':`<text x="${left}" y="${height-5}" fill="#97a6ae" font-size="10">Inicio</text><text x="${width-right}" y="${height-5}" text-anchor="end" fill="#97a6ae" font-size="10">${escapeHtml(String(dates?.at(-1)||'').slice(0,10))}</text>`}</svg>`;
  if (drawdown) return;
  const tooltip=document.createElement("div"); tooltip.className="chart-tooltip hidden"; container.appendChild(tooltip);
  container.onpointermove=(event)=>{
    const rect=container.getBoundingClientRect(), px=(event.clientX-rect.left)/rect.width*width;
    const index=Math.max(0,Math.min(values.length-1,Math.round((px-left)/(width-left-right)*(values.length-1))));
    tooltip.innerHTML=`${escapeHtml(String(dates?.[index]||'').slice(0,19))}<br><strong>${money(values[index])}</strong>`;
    tooltip.style.left=`${Math.max(0,Math.min(event.clientX-rect.left+12,rect.width-165))}px`;
    tooltip.style.top="20px"; tooltip.classList.remove("hidden");
  };
  container.onpointerleave=()=>tooltip.classList.add("hidden");
}
async function loadReport(attempt, scroll) {
  const request = ++reportRequest;
  try {
    const report = await api(`/api/report/${attempt}.json`);
    if (request !== reportRequest) return;
    selectedAttempt = attempt; selectedReport = report;
    renderAssets(report);
    renderMetrics(report.quant_metrics);
    $("reportPanel").classList.remove("hidden"); $("reportTitle").textContent = `#${String(attempt).padStart(2,'0')} · ${report.strategy_name} · ${report.selected_asset || ""}`;
    $("downloadHtml").href=`/api/report/${attempt}.html?download=1`; $("downloadJson").href=`/api/report/${attempt}.json?download=1`;
    $("downloadCode").classList.toggle("hidden",report.outcome!=="APPROVED");
    const h=report.hypothesis, ok=report.outcome==="APPROVED";
    const parameters = h.family ? [['Asignación',pct(h.allocation)],['Stop nominal',pct(h.stop_loss)],['Objetivo nominal',pct(h.take_profit)],['Tenencia máx.',`${h.max_holding} barras`]] : [];
    $("reportContent").innerHTML=`<div class="report-summary ${ok?'success':''}"><span class="symbol">${ok?'✓':'↻'}</span><div><h4>${ok?'Filtros superados · aprobado en simulación':'Intento rechazado · diagnóstico disponible'}</h4><p>${escapeHtml(report.summary)} ${escapeHtml(report.next_action)}</p></div></div><p class="report-copy">${escapeHtml(h.rationale || 'No se obtuvo una hipótesis válida en este intento.')}</p><div class="params">${parameters.map(([key,value])=>`<span>${escapeHtml(key)} <b>${escapeHtml(value)}</b></span>`).join('')}</div><h4 class="report-section-title">Resultados frente a los criterios de aprobación</h4><div class="check-grid">${report.checks.map(c=>`<div class="check ${c.passed===false?'fail':c.passed===null?'skip':''}"><span class="indicator">${c.passed===true?'✓':c.passed===false?'×':'—'}</span><div><strong>${escapeHtml(c.label)}</strong><p>${escapeHtml(c.value)}</p><small>${escapeHtml(c.requirement)}</small></div></div>`).join('')}</div>${report.rejection_reasons.length?`<h4 class="report-section-title">Motivos de rechazo</h4><ul class="reasons">${report.rejection_reasons.map(r=>`<li>${escapeHtml(r)}</li>`).join('')}</ul>`:''}<div class="limitations">${report.limitations.map(l=>escapeHtml(l)).join('<br>')}</div>`;
    document.querySelectorAll("#historyRows tr[data-attempt]").forEach(row=>row.classList.toggle("selected",Number(row.dataset.attempt)===attempt));
    $("reportContent").insertAdjacentHTML("beforeend", renderResearch(report));
    $("reportContent").insertAdjacentHTML("beforeend", `<h4 class="report-section-title">Resultados completos de los cinco activos</h4><p>OOS positivo: ${escapeHtml((report.positive_assets||[]).join(', ')||'ninguno')}. Activo destacado: ${escapeHtml(report.selected_asset||'—')}.</p>${Object.entries(report.asset_results||{}).map(([asset,row])=>`<details class="asset-cost"><summary>${escapeHtml(asset)} · métricas, fuente y costos</summary><pre style="white-space:pre-wrap;overflow-wrap:anywhere">${escapeHtml(JSON.stringify(row,null,2))}</pre></details>`).join('')}`);
    chartKey="oos"; renderCharts(report.charts);
    if (scroll) $("reportPanel").scrollIntoView({behavior:"smooth",block:"start"});
  } catch (exc) { if(request===reportRequest) error(`No se pudo abrir el reporte: ${exc.message}`); }
}
async function refresh() {
  try {
    const data = await api(`/api/status?since=${revision}`);
    $("connectionDot").className="connection-dot online"; $("connectionText").textContent="Motor conectado";
    if (!data.unchanged) { revision=data.revision; render(data); }
  } catch (exc) {
    revision = -1;
    $("connectionDot").className="connection-dot offline"; $("connectionText").textContent="Sin conexión · reintentando";
    $("startButton").disabled = true;
  }
}
async function poll() { await refresh(); setTimeout(poll,800); }
api("/api/config").then(data=>{
  $("modelInput").value=data.model;
  if(data.has_api_key) $("keyHint").textContent="Hay una clave configurada en el entorno. Puedes dejar este campo vacío.";
}).catch(()=>{});
poll();

const assetNames = {EURUSD:"EUR/USD",XAUUSD:"XAU/USD · Oro",GBPUSD:"GBP/USD",CADUSD:"CAD/USD",BTCUSD:"BTC/USD"};
const assetSettings = Object.fromEntries(Object.keys(assetNames).map(asset=>[asset, asset==="BTCUSD" ? {commission_rate:.006,fixed_commission:0,spread_bps:5,slippage_bps:5,min_notional:10,quantity_step:.00000001,holding_cost_bps:0} : {commission_rate:.000035,fixed_commission:0,spread_bps:asset==="XAUUSD"?3:2,slippage_bps:1,min_notional:0,quantity_step:asset==="XAUUSD"?1:1000,holding_cost_bps:1}]));
const costLabels = {commission_rate:"Comisión/lado (%)",fixed_commission:"Comisión fija USD",spread_bps:"Spread (bps)",slippage_bps:"Deslizamiento (bps)",min_notional:"Mínimo USD",quantity_step:"Paso de cantidad",holding_cost_bps:"Tenencia diaria (bps)"};
$("assetCostForms").innerHTML = Object.entries(assetSettings).map(([asset,cfg])=>`<details class="asset-cost"><summary>${assetNames[asset]}</summary><div class="form-grid">${Object.entries(cfg).map(([key,value])=>`<label>${costLabels[key]}<input data-asset="${asset}" data-cost="${key}" type="number" min="${key==='quantity_step'?'0.00000001':'0'}" step="any" value="${key==='commission_rate'?value*100:value}" required></label>`).join('')}</div></details>`).join('');
function renderAssets(context) {
  const rows=context.asset_results||{}, active=context.active_asset, selected=context.selected_asset;
  $("assetProgress").textContent = active ? `Probando ${assetNames[active]||active} · ${context.asset_progress?.index||0}/5 · misma hipótesis` : selected ? `Activo destacado: ${assetNames[selected]||selected} · ${Object.keys(rows).length}/5 evaluados` : "Se prueban los cinco activos antes de cambiar de hipótesis";
  $("assetRows").innerHTML=Object.entries(assetNames).map(([asset,name])=>{
    const r=rows[asset], q=r?.quant_metrics||{}, o=q.out_of_sample||{}, stress=r?.stress_metrics||{}, passed=q.passed&&stress.passed&&!snapshot?.synthetic;
    const label=active===asset?'EN PRUEBA':passed?'APROBADO':r?(o.net_return>0?'POSITIVO · REVISAR':'RECHAZADO'):'PENDIENTE';
    const scenario=stress.scenarios?.['1'];
    const reasons=[...(q.rejection_reasons||[]),...(stress.rejection_reasons||[])].join('; ');
    return `<tr class="${selected===asset?'selected':''}"><td><b>${name}</b><br><small>${escapeHtml(r?.metadata?.source || (snapshot?.synthetic?'Demo sintética':asset==='BTCUSD'?'Coinbase':'Dukascopy'))}${r?.metadata?.bars?` · ${r.metadata.bars} barras`:''}</small></td><td class="${o.net_return>0?'positive':o.net_return<0?'negative':''}">${pct(o.net_return)}</td><td>${num(o.sharpe)}</td><td>${pct(o.max_drawdown)}</td><td>${scenario?money(scenario.final_equity):'Sin evaluar'}</td><td><span class="result-badge ${passed?'approved':''}">${label}</span>${reasons?`<details><summary>Ver motivos</summary><small>${escapeHtml(reasons)}</small></details>`:''}</td></tr>`;
  }).join('');
}
