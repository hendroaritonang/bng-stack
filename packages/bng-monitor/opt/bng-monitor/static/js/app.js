/* BNG Monitor — Frontend Application */

// ===== State =====
const state = {
    token: localStorage.getItem('bng_token') || '',
    username: localStorage.getItem('bng_user') || '',
    userRole: localStorage.getItem('bng_role') || 'admin',
    allowedPages: JSON.parse(localStorage.getItem('bng_pages') || '[]'),
    currentPage: 'dashboard',
    ws: null,
    wsRetry: 0,
    charts: {},
    refreshTimers: {},
    dashboardData: null,
};

// ===== API Helpers =====
async function api(method, path, body = null) {
    const opts = {
        method,
        headers: { 'Content-Type': 'application/json' },
    };
    if (state.token) opts.headers['Authorization'] = `Bearer ${state.token}`;
    if (body) opts.body = JSON.stringify(body);

    const resp = await fetch(`/api${path}`, opts);
    if (resp.status === 401) {
        logout();
        throw new Error('Session expired');
    }
    if (!resp.ok) {
        const err = await resp.json().catch(() => ({ detail: resp.statusText }));
        throw new Error(err.detail || 'API Error');
    }
    return resp.json();
}

const GET = (p) => api('GET', p);
const POST = (p, b) => api('POST', p, b);
const PUT = (p, b) => api('PUT', p, b);

// ===== Auth =====
async function login(e) {
    e.preventDefault();
    const user = document.getElementById('login-user').value;
    const pass = document.getElementById('login-pass').value;
    const errEl = document.getElementById('login-error');
    try {
        errEl.style.display = 'none';
        const data = await POST('/auth/login', { username: user, password: pass });
        state.token = data.access_token;
        state.username = data.username;
        state.userRole = data.role || 'admin';
        state.allowedPages = data.allowed_pages || [];
        localStorage.setItem('bng_token', state.token);
        localStorage.setItem('bng_user', state.username);
        localStorage.setItem('bng_role', state.userRole);
        localStorage.setItem('bng_pages', JSON.stringify(state.allowedPages));
        showApp();
    } catch (err) {
        errEl.textContent = err.message;
        errEl.style.display = 'block';
    }
}

function logout() {
    state.token = '';
    state.username = '';
    localStorage.removeItem('bng_token');
    localStorage.removeItem('bng_user');
    localStorage.removeItem('bng_role');
    localStorage.removeItem('bng_pages');
    if (state.ws) { state.ws.close(); state.ws = null; }
    Object.values(state.refreshTimers).forEach(clearInterval);
    state.refreshTimers = {};
    document.getElementById('app').style.display = 'none';
    document.getElementById('login-screen').style.display = 'flex';
}

function showApp() {
    document.getElementById('login-screen').style.display = 'none';
    document.getElementById('app').style.display = 'flex';
    document.getElementById('current-user').textContent = state.username;
    filterMenuByPermissions();
    connectWS();
    navigateTo('dashboard');
    startAutoRefresh();
}

function filterMenuByPermissions() {
    // Admin sees everything
    if (state.userRole === 'admin') {
        document.querySelectorAll('.nav-link').forEach(l => l.parentElement.style.display = '');
        return;
    }
    // Non-admin: show only allowed pages
    document.querySelectorAll('.nav-link[data-page]').forEach(link => {
        const page = link.dataset.page;
        if (state.allowedPages.length === 0 || state.allowedPages.includes(page)) {
            link.parentElement.style.display = '';
        } else {
            link.parentElement.style.display = 'none';
        }
    });
}

// ===== WebSocket =====
function connectWS() {
    if (state.ws) { state.ws.close(); state.ws = null; }
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const url = `${proto}://${location.host}/ws?token=${state.token}`;
    const ws = new WebSocket(url);

    ws.onopen = () => {
        state.wsRetry = 0;
        setConnStatus(true);
    };
    ws.onmessage = (e) => {
        try {
            const msg = JSON.parse(e.data);
            if (msg.type === 'update') handleWSUpdate(msg);
        } catch {}
    };
    ws.onclose = () => {
        setConnStatus(false);
        state.ws = null;
        const delay = Math.min(2000 * Math.pow(2, state.wsRetry), 30000);
        state.wsRetry++;
        setTimeout(() => { if (state.token) connectWS(); }, delay);
    };
    ws.onerror = () => ws.close();
    state.ws = ws;

    // Ping keepalive
    setInterval(() => { if (ws.readyState === 1) ws.send('ping'); }, 25000);
}

function setConnStatus(connected) {
    const el = document.getElementById('connection-status');
    el.textContent = connected ? 'Connected' : 'Disconnected';
    el.className = `conn-status ${connected ? 'conn-connected' : 'conn-disconnected'}`;
}

function handleWSUpdate(msg) {
    document.getElementById('last-update').textContent = `Updated ${formatTime(msg.ts)}`;
    // Update dashboard if visible
    if (state.currentPage === 'dashboard' && state.dashboardData) {
        if (msg.vpp) {
            state.dashboardData.vpp.running = msg.vpp.running;
            state.dashboardData.vpp.pid = msg.vpp.pid;
            state.dashboardData.vpp.total_pppoe_sessions = msg.vpp.total_sessions;
        }
        if (msg.brs) {
            for (const [br, info] of Object.entries(msg.brs)) {
                const brData = state.dashboardData.brs.find(b => b.name === br);
                if (brData) {
                    brData.running = info.running;
                    brData.session_count = info.session_count;
                }
            }
        }
        renderDashboard(state.dashboardData);
    }
    // Update alert badge
    if (msg.unack_alerts !== undefined) updateAlertBadge(msg.unack_alerts);
}

// ===== Navigation =====
function navigateTo(page) {
    state.currentPage = page;
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.nav-link').forEach(l => l.classList.remove('active'));
    const pageEl = document.getElementById(`page-${page}`);
    if (pageEl) pageEl.classList.add('active');
    const navEl = document.querySelector(`[data-page="${page}"]`);
    if (navEl) navEl.classList.add('active');

    // Load page data
    switch (page) {
        case 'dashboard': loadDashboard(); break;
        case 'sessions': loadSessions(); break;
        case 'trace': loadTracePage(); break;
        case 'br-mgmt': loadBRMgmt(); break;
        case 'alerts': loadAlerts(); break;
        case 'history': loadHistory(); break;
        case 'radius': loadRadius(); break;
        case 'traffic': loadTraffic(); break;
        case 'users': loadUsers(); break;
    }
}

function startAutoRefresh() {
    // Dashboard auto-refresh every 10s
    state.refreshTimers.dashboard = setInterval(() => {
        if (state.currentPage === 'dashboard') loadDashboard(true);
    }, 10000);
    // Alert badge refresh every 30s
    state.refreshTimers.alertBadge = setInterval(refreshAlertBadge, 30000);
    refreshAlertBadge();
}

async function refreshAlertBadge() {
    try {
        const data = await GET('/alerts?unack_only=true&limit=1');
        // Use dashboard unack count if available
        const d = await GET('/dashboard');
        updateAlertBadge(d.unack_alerts);
    } catch {}
}

function updateAlertBadge(count) {
    const badge = document.getElementById('alert-badge');
    if (count > 0) {
        badge.textContent = count > 99 ? '99+' : count;
        badge.style.display = 'inline';
    } else {
        badge.style.display = 'none';
    }
}

// ===== Dashboard =====
async function loadDashboard(silent = false) {
    const page = document.getElementById('page-dashboard');
    if (!silent) page.innerHTML = '<div class="skeleton" style="height:200px;margin-bottom:16px"></div>';
    try {
        const data = await GET('/dashboard');
        state.dashboardData = data;
        renderDashboard(data);
    } catch (err) {
        if (!silent) page.innerHTML = `<p class="text-danger">Failed to load: ${esc(err.message)}</p>`;
    }
}

function renderDashboard(data) {
    const page = document.getElementById('page-dashboard');
    const vpp = data.vpp;
    const sys = data.system;
    const brs = data.brs;
    const totalSess = data.total_sessions;

    const cpuClass = sys.cpu_percent > 90 ? 'text-danger' : sys.cpu_percent > 70 ? 'text-warning' : 'text-success';
    const memClass = sys.mem_percent > 90 ? 'text-danger' : sys.mem_percent > 70 ? 'text-warning' : 'text-success';
    const diskClass = sys.disk_percent > 90 ? 'text-danger' : sys.disk_percent > 70 ? 'text-warning' : 'text-success';

    let brCards = brs.map(br => `
        <div class="card">
            <div class="card-header">
                <span class="card-label">${esc(br.name.toUpperCase())}</span>
                <span class="status-tag ${br.running ? 'tag-up' : 'tag-down'}">${br.running ? 'UP' : 'DOWN'}</span>
            </div>
            <div class="card-value">${br.session_count}</div>
            <div class="card-sub">sessions${br.vlan ? ` | VLAN ${br.vlan}` : ''}${br.gw_ip ? ` | GW ${br.gw_ip}` : ''}</div>
        </div>
    `).join('');

    page.innerHTML = `
        <div class="page-header">
            <h1 class="page-title">Dashboard</h1>
            <span class="text-muted">Auto-refresh: 10s</span>
        </div>

        <div class="cards-grid">
            <div class="card">
                <div class="card-header">
                    <span class="card-label">VPP Status</span>
                    <span class="status-tag ${vpp.running ? 'tag-up' : 'tag-down'}">${vpp.running ? 'RUNNING' : 'DOWN'}</span>
                </div>
                <div class="card-value">${vpp.running ? `PID ${vpp.pid}` : 'OFFLINE'}</div>
                <div class="card-sub">${esc(vpp.version || 'N/A')}</div>
            </div>

            <div class="card">
                <div class="card-header">
                    <span class="card-label">Total Sessions</span>
                </div>
                <div class="card-value">${totalSess}</div>
                <div class="card-sub">VPP PPPoE: ${vpp.total_pppoe_sessions} | Accel: ${totalSess}</div>
            </div>

            <div class="card">
                <div class="card-header">
                    <span class="card-label">CPU Usage</span>
                </div>
                <div class="card-value ${cpuClass}">${sys.cpu_percent.toFixed(1)}%</div>
                <div class="progress-bar ${sys.cpu_percent > 90 ? 'progress-red' : sys.cpu_percent > 70 ? 'progress-yellow' : 'progress-green'}">
                    <div class="progress-bar-fill" style="width:${Math.min(sys.cpu_percent, 100)}%"></div>
                </div>
                <div class="card-sub">Load: ${sys.load_avg.map(l => l.toFixed(2)).join(' / ')}</div>
            </div>

            <div class="card">
                <div class="card-header">
                    <span class="card-label">Memory</span>
                </div>
                <div class="card-value ${memClass}">${sys.mem_percent.toFixed(1)}%</div>
                <div class="progress-bar ${sys.mem_percent > 90 ? 'progress-red' : sys.mem_percent > 70 ? 'progress-yellow' : 'progress-green'}">
                    <div class="progress-bar-fill" style="width:${Math.min(sys.mem_percent, 100)}%"></div>
                </div>
                <div class="card-sub">${sys.mem_used_mb.toFixed(0)} / ${sys.mem_total_mb.toFixed(0)} MB | VPP RSS: ${sys.vpp_rss_mb.toFixed(0)} MB</div>
            </div>

            <div class="card">
                <div class="card-header">
                    <span class="card-label">Disk</span>
                </div>
                <div class="card-value ${diskClass}">${sys.disk_percent.toFixed(1)}%</div>
                <div class="progress-bar ${sys.disk_percent > 90 ? 'progress-red' : sys.disk_percent > 70 ? 'progress-yellow' : 'progress-green'}">
                    <div class="progress-bar-fill" style="width:${Math.min(sys.disk_percent, 100)}%"></div>
                </div>
            </div>

            <div class="card">
                <div class="card-header">
                    <span class="card-label">System Uptime</span>
                </div>
                <div class="card-value">${formatDuration(sys.uptime_seconds)}</div>
                <div class="card-sub">Alerts (1h): ${data.unack_alerts} unacknowledged</div>
            </div>
        </div>

        <h2 style="font-size:16px;margin-bottom:12px">BR Instances</h2>
        <div class="cards-grid">
            ${brCards}
        </div>
    `;
}

// ===== Sessions =====
async function loadSessions() {
    const page = document.getElementById('page-sessions');
    page.innerHTML = `
        <div class="page-header">
            <h1 class="page-title">Sessions</h1>
            <button class="btn btn-sm" onclick="loadSessions()">Refresh</button>
        </div>
        <div class="filter-bar">
            <input type="text" id="session-search" placeholder="Search username, IP, MAC..." oninput="filterSessions()">
            <select id="session-br-filter" onchange="filterSessions()">
                <option value="">All BRs</option>
            </select>
        </div>
        <div id="sessions-table" class="table-wrapper"><div class="skeleton" style="height:200px"></div></div>
    `;

    try {
        const data = await GET('/sessions');
        window._sessionsData = data.sessions;

        // Populate BR filter
        const brs = [...new Set(data.sessions.map(s => s.br))];
        const sel = document.getElementById('session-br-filter');
        if (sel) brs.forEach(br => { const o = document.createElement('option'); o.value = br; o.textContent = br.toUpperCase(); sel.appendChild(o); });

        renderSessionsTable(data.sessions);
    } catch (err) {
        document.getElementById('sessions-table').innerHTML = `<p class="text-danger">${esc(err.message)}</p>`;
    }
}

function filterSessions() {
    const search = (document.getElementById('session-search')?.value || '').toLowerCase();
    const br = document.getElementById('session-br-filter')?.value || '';
    let filtered = window._sessionsData || [];
    if (br) filtered = filtered.filter(s => s.br === br);
    if (search) filtered = filtered.filter(s =>
        (s.username || '').toLowerCase().includes(search) ||
        (s.ip || '').toLowerCase().includes(search) ||
        (s.mac || '').toLowerCase().includes(search) ||
        (s.ifname || '').toLowerCase().includes(search)
    );
    renderSessionsTable(filtered);
}

function renderSessionsTable(sessions) {
    const el = document.getElementById('sessions-table');
    if (!sessions.length) {
        el.innerHTML = '<p class="text-muted" style="padding:20px">No sessions found</p>';
        return;
    }
    el.innerHTML = `
        <table>
            <thead><tr>
                <th>BR</th><th>Interface</th><th>Username</th><th>IP Address</th>
                <th>MAC</th><th>Uptime</th><th>Rate Limit</th><th>VPP</th><th>Traffic</th><th></th>
            </tr></thead>
            <tbody>
                ${sessions.map(s => `
                    <tr>
                        <td><span class="status-tag ${s.br ? 'tag-info' : ''}">${esc(s.br)}</span></td>
                        <td class="mono">${esc(s.ifname)}</td>
                        <td>${esc(s.username)}</td>
                        <td class="mono">${esc(s.ip)}</td>
                        <td class="mono">${esc(s.mac)}</td>
                        <td>${esc(s.uptime)}</td>
                        <td class="mono">${esc(s.rate_limit || '-')}</td>
                        <td><span class="status-dot ${s.vpp_state === 'up' ? 'status-up' : s.vpp_session_id ? 'status-up' : 'status-down'}"></span>${esc(s.vpp_state || (s.vpp_session_id ? 'up' : '?'))}</td>
                        <td class="mono">${s.rx_bytes ? formatBytes(s.rx_bytes) + ' / ' + formatBytes(s.tx_bytes) : '-'}</td>
                        <td><button class="btn btn-sm" onclick="showSessionDetail('${esc(s.br)}','${esc(s.ifname)}')">Detail</button></td>
                    </tr>
                `).join('')}
            </tbody>
        </table>
        <div class="card-sub mt-8">${sessions.length} session(s)</div>
    `;
}

async function showSessionDetail(br, ifname) {
    try {
        const data = await GET(`/sessions/${br}/${ifname}`);
        const s = data.session || {};
        const vi = data.vpp_interface || {};
        const vs = data.vpp_session || {};
        const pols = data.policers || [];
        const disc = data.disconnect_history || [];

        // Connection timeline
        const uptime = s.uptime || '';
        const uptimeSec = parseUptimeStr(uptime);

        let html = `
            <div class="session-detail-grid">
                <!-- Connection Timeline -->
                <div class="card" style="grid-column:1/-1;padding:16px">
                    <div class="flex-between mb-8">
                        <h4>Connection Info</h4>
                        <div class="btn-group">
                            <button class="btn btn-sm btn-danger" onclick="disconnectSession('${esc(br)}','${esc(ifname)}')">Disconnect User</button>
                        </div>
                    </div>
                    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px">
                        <div><span class="label">Username</span><div class="mono" style="font-size:16px;font-weight:600">${esc(s.username || '-')}</div></div>
                        <div><span class="label">IP Address</span><div class="mono">${esc(s.ip || s.address || '-')}</div></div>
                        <div><span class="label">MAC</span><div class="mono" style="font-size:11px">${esc(s['calling-sid'] || s.mac || '-')}</div></div>
                        <div><span class="label">Interface</span><div class="mono">${esc(ifname)}</div></div>
                        <div><span class="label">Uptime</span><div class="mono">${esc(uptime)}${uptimeSec > 0 ? ` (${formatDuration(uptimeSec)})` : ''}</div></div>
                        <div><span class="label">Rate Limit</span><div class="mono">${esc(s['rate-limit'] || s.rate || '-')}</div></div>
                        <div><span class="label">State</span><div><span class="status-tag ${s.state === 'active' ? 'tag-up' : 'tag-down'}">${esc(s.state || 'unknown')}</span></div></div>
                        <div><span class="label">VPP State</span><div><span class="status-dot ${vi.state === 'up' ? 'status-up' : 'status-down'}"></span>${esc(vi.state || 'unknown')}</div></div>
                    </div>
                </div>

                <!-- Traffic Stats -->
                <div class="card" style="padding:16px">
                    <h4 style="margin-bottom:12px">Traffic Statistics</h4>
                    <div class="traffic-stat-grid">
                        <div class="traffic-stat">
                            <span class="label">RX (from subscriber)</span>
                            <div class="mono" style="font-size:18px;font-weight:600;color:var(--info)">${formatBytes(vi.rx_bytes || 0)}</div>
                            <div class="card-sub">${formatNumber(vi.rx_packets || 0)} packets</div>
                        </div>
                        <div class="traffic-stat">
                            <span class="label">TX (to subscriber)</span>
                            <div class="mono" style="font-size:18px;font-weight:600;color:var(--success)">${formatBytes(vi.tx_bytes || 0)}</div>
                            <div class="card-sub">${formatNumber(vi.tx_packets || 0)} packets</div>
                        </div>
                        <div class="traffic-stat">
                            <span class="label">Drops</span>
                            <div class="mono" style="font-size:18px;font-weight:600;color:${(vi.drops || 0) > 0 ? 'var(--danger)' : 'var(--text-muted)'}">${formatNumber(vi.drops || 0)}</div>
                        </div>
                        <div class="traffic-stat">
                            <span class="label">SW If Index</span>
                            <div class="mono" style="font-size:18px;font-weight:600">${vi.sw_if_index !== undefined ? vi.sw_if_index : '-'}</div>
                        </div>
                    </div>
                </div>

                <!-- Policer Stats -->
                <div class="card" style="padding:16px">
                    <h4 style="margin-bottom:12px">Policer Stats</h4>
                    ${pols.length ? pols.map(p => {
                        const totalPkts = (p.conform_packets||0) + (p.exceed_packets||0) + (p.violate_packets||0);
                        const conformPct = totalPkts > 0 ? (p.conform_packets / totalPkts * 100) : 100;
                        const exceedPct = totalPkts > 0 ? (p.exceed_packets / totalPkts * 100) : 0;
                        const violatePct = totalPkts > 0 ? (p.violate_packets / totalPkts * 100) : 0;
                        const dir = p.direction || '?';
                        return `
                        <div style="margin-bottom:12px;padding:10px;background:var(--bg-primary);border-radius:var(--radius-sm);border:1px solid var(--border)">
                            <div class="flex-between mb-8">
                                <span class="mono" style="font-size:11px">${esc(p.name)}</span>
                                <span class="status-tag ${dir === 'up' ? 'tag-info' : 'tag-warn'}">${esc(dir)}</span>
                            </div>
                            <div class="flex-between mb-8">
                                <span class="card-sub">CIR: ${p.cir} kbps | EIR: ${p.eir} kbps</span>
                            </div>
                            <!-- Policer gauge bar -->
                            <div style="height:20px;background:var(--bg-tertiary);border-radius:10px;overflow:hidden;display:flex">
                                <div style="width:${conformPct}%;background:var(--success);transition:width 0.5s" title="Conform: ${conformPct.toFixed(1)}%"></div>
                                <div style="width:${exceedPct}%;background:var(--warning);transition:width 0.5s" title="Exceed: ${exceedPct.toFixed(1)}%"></div>
                                <div style="width:${violatePct}%;background:var(--danger);transition:width 0.5s" title="Violate: ${violatePct.toFixed(1)}%"></div>
                            </div>
                            <div style="display:flex;justify-content:space-between;margin-top:6px;font-size:11px">
                                <span style="color:var(--success)">Conform: ${formatNumber(p.conform_packets||0)} (${conformPct.toFixed(1)}%)</span>
                                <span style="color:var(--warning)">Exceed: ${formatNumber(p.exceed_packets||0)} (${exceedPct.toFixed(1)}%)</span>
                                <span style="color:var(--danger)">Violate: ${formatNumber(p.violate_packets||0)} (${violatePct.toFixed(1)}%)</span>
                            </div>
                        </div>`;
                    }).join('') : '<p class="text-muted">No policers configured</p>'}
                </div>
            </div>

            <!-- VPP PPPoE Session -->
            ${vs ? `
            <div class="card mt-16" style="padding:16px">
                <h4 style="margin-bottom:8px">VPP PPPoE Session</h4>
                <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:8px">
                    ${Object.entries(vs).map(([k,v]) => `<div class="br-card-row"><span class="label">${esc(k)}</span><span class="mono">${esc(String(v))}</span></div>`).join('')}
                </div>
            </div>` : ''}

            <!-- Disconnect History -->
            ${disc.length ? `
            <div class="mt-16">
                <h4 style="margin-bottom:8px">Disconnect History (last ${disc.length})</h4>
                <table>
                    <thead><tr><th>Time</th><th>Reason</th><th>Duration</th><th>IP</th></tr></thead>
                    <tbody>
                    ${disc.map(d => `<tr><td>${formatDateTime(d.ts)}</td><td>${esc(d.reason || '-')}</td><td>${d.duration ? formatDuration(d.duration) : '-'}</td><td class="mono">${esc(d.ip || '')}</td></tr>`).join('')}
                    </tbody>
                </table>
            </div>` : ''}
        `;

        openModal(`${br.toUpperCase()} / ${ifname}`, html);
    } catch (err) {
        toast('error', err.message);
    }
}

async function disconnectSession(br, ifname) {
    if (!confirm(`Disconnect session "${ifname}" on ${br.toUpperCase()}? The subscriber will be disconnected.`)) return;
    try {
        toast('info', `Disconnecting ${ifname}...`);
        const data = await POST(`/sessions/${br}/${ifname}/disconnect`);
        if (data.success) {
            toast('success', `Session ${ifname} disconnected`);
            closeModal();
            setTimeout(() => loadSessions(), 2000);
        } else {
            toast('error', `Disconnect failed: ${data.output}`);
        }
    } catch (err) {
        toast('error', err.message);
    }
}

function parseUptimeStr(s) {
    if (!s) return 0;
    try {
        let days = 0, time = s;
        const dm = s.match(/(\d+)d\s+(.+)/);
        if (dm) { days = parseInt(dm[1]); time = dm[2]; }
        const parts = time.split(':').map(Number);
        if (parts.length === 3) return days * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2];
        if (parts.length === 2) return days * 86400 + parts[0] * 60 + parts[1];
    } catch {}
    return 0;
}

// ===== Trace / Debug =====
function loadTracePage() {
    const page = document.getElementById('page-trace');
    page.innerHTML = `
        <div class="page-header">
            <h1 class="page-title">Trace / Debug</h1>
        </div>

        <div class="trace-section">
            <div class="trace-section-header">VPP Ping Test</div>
            <div class="trace-section-body">
                <div class="trace-form">
                    <div class="form-group">
                        <label>Destination IP</label>
                        <input type="text" id="ping-dest" placeholder="192.168.100.10">
                    </div>
                    <div class="form-group">
                        <label>Source Interface (optional, auto-detect)</label>
                        <input type="text" id="ping-src" placeholder="auto (e.g. loop100)">
                    </div>
                    <div class="form-group">
                        <label>Count</label>
                        <input type="number" id="ping-count" value="3" min="1" max="10" style="width:80px">
                    </div>
                    <button class="btn btn-primary" onclick="doPing()" id="ping-btn">Ping</button>
                </div>
                <div id="ping-result" class="trace-output mt-16" style="display:none"></div>
            </div>
        </div>

        <div class="trace-section">
            <div class="trace-section-header">Interface Traffic Lookup</div>
            <div class="trace-section-body">
                <div class="trace-form">
                    <div class="form-group">
                        <label>Interface Name</label>
                        <input type="text" id="traffic-ifname" placeholder="noceng">
                    </div>
                    <button class="btn btn-primary" onclick="doTrafficLookup()">Lookup</button>
                </div>
                <div id="traffic-result" class="trace-output mt-16" style="display:none"></div>
            </div>
        </div>

        <div class="trace-section">
            <div class="trace-section-header">Policer Lookup</div>
            <div class="trace-section-body">
                <div class="trace-form">
                    <div class="form-group">
                        <label>Interface Name</label>
                        <input type="text" id="policer-ifname" placeholder="noceng">
                    </div>
                    <button class="btn btn-primary" onclick="doPolicerLookup()">Lookup</button>
                </div>
                <div id="policer-result" class="mt-16" style="display:none"></div>
            </div>
        </div>

        <div class="trace-section">
            <div class="trace-section-header">Disconnect History</div>
            <div class="trace-section-body">
                <div class="trace-form">
                    <div class="form-group">
                        <label>Username (optional)</label>
                        <input type="text" id="disc-username" placeholder="All users">
                    </div>
                    <div class="form-group">
                        <label>BR (optional)</label>
                        <select id="disc-br">
                            <option value="">All</option>
                        </select>
                    </div>
                    <button class="btn btn-primary" onclick="doDiscLookup()">Search</button>
                </div>
                <div id="disc-result" class="mt-16"></div>
            </div>
        </div>
    `;

    // Populate BR dropdown
    populateBRDropdown('disc-br');
}

async function doPing() {
    const btn = document.getElementById('ping-btn');
    btn.disabled = true; btn.textContent = 'Pinging...';
    const res = document.getElementById('ping-result');
    res.style.display = 'block';
    res.textContent = 'Sending ping...';
    try {
        const data = await POST('/trace/ping', {
            destination: document.getElementById('ping-dest').value,
            source: document.getElementById('ping-src').value || '',
            count: parseInt(document.getElementById('ping-count').value) || 3,
        });
        res.textContent = data.output || (data.success ? 'Ping succeeded' : 'Ping failed');
    } catch (err) {
        res.textContent = `Error: ${err.message}`;
    }
    btn.disabled = false; btn.textContent = 'Ping';
}

async function doTrafficLookup() {
    const ifname = document.getElementById('traffic-ifname').value;
    if (!ifname) return;
    const res = document.getElementById('traffic-result');
    res.style.display = 'block';
    try {
        const data = await GET(`/trace/traffic/${ifname}`);
        res.textContent = JSON.stringify(data, null, 2);
    } catch (err) {
        res.textContent = `Error: ${err.message}`;
    }
}

async function doPolicerLookup() {
    const ifname = document.getElementById('policer-ifname').value;
    if (!ifname) return;
    const res = document.getElementById('policer-result');
    res.style.display = 'block';
    try {
        const data = await GET(`/trace/policer/${ifname}`);
        if (data.policers.length === 0) {
            res.innerHTML = `<span class="text-muted">No policers found for "${esc(ifname)}". Policer names use pattern: vyos_&lt;br&gt;_&lt;sw_if_index&gt;_&lt;rate&gt;_&lt;burst&gt;_&lt;direction&gt;</span>`;
        } else {
            res.innerHTML = renderPolicerTable(data.policers, ifname);
        }
    } catch (err) {
        res.innerHTML = `<span class="text-danger">Error: ${esc(err.message)}</span>`;
    }
}

function renderPolicerTable(policers, ifname) {
    let html = `<div style="margin-bottom:8px;font-size:13px;color:var(--text-secondary)">Found ${policers.length} policer(s) for <strong>${esc(ifname)}</strong></div>`;
    html += `<table>
        <thead><tr>
            <th>Name</th><th>Dir</th><th>CIR (kbps)</th><th>EIR (kbps)</th>
            <th>Conform</th><th>Exceed</th><th>Violate</th><th>Actions</th>
        </tr></thead><tbody>`;

    for (const p of policers) {
        const dir = p.direction || (p.name.endsWith('_up') ? 'up' : p.name.endsWith('_down') ? 'down' : '?');
        const dirClass = dir === 'up' ? 'tag-info' : 'tag-warn';
        const totalPkts = (p.conform_packets || 0) + (p.exceed_packets || 0) + (p.violate_packets || 0);
        const exceedPct = totalPkts > 0 ? ((p.exceed_packets || 0) / totalPkts * 100).toFixed(1) : '0.0';
        const violatePct = totalPkts > 0 ? ((p.violate_packets || 0) / totalPkts * 100).toFixed(1) : '0.0';
        const exceedWarn = parseFloat(exceedPct) > 10 ? ' text-warning' : '';
        const violateWarn = parseFloat(violatePct) > 5 ? ' text-danger' : '';

        html += `<tr>
            <td class="mono" style="font-size:11px;white-space:normal;word-break:break-all">${esc(p.name)}</td>
            <td><span class="status-tag ${dirClass}">${esc(dir)}</span></td>
            <td class="mono">${p.cir || '-'}</td>
            <td class="mono">${p.eir || '-'}</td>
            <td class="mono">${formatPolicerStat(p.conform_packets, p.conform_bytes)}</td>
            <td class="mono${exceedWarn}">${formatPolicerStat(p.exceed_packets, p.exceed_bytes)} <span style="font-size:10px">(${exceedPct}%)</span></td>
            <td class="mono${violateWarn}">${formatPolicerStat(p.violate_packets, p.violate_bytes)} <span style="font-size:10px">(${violatePct}%)</span></td>
            <td class="mono" style="font-size:11px">${esc(p.conform_action || 'transmit')} / ${esc(p.exceed_action || 'transmit')} / ${esc(p.violate_action || 'drop')}</td>
        </tr>`;
    }
    html += '</tbody></table>';
    return html;
}

function formatPolicerStat(packets, bytes) {
    if (!packets && !bytes) return '0';
    return `${formatNumber(packets || 0)} pkts<br>${formatBytes(bytes || 0)}`;
}

function formatNumber(n) {
    if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
    if (n >= 1000) return (n / 1000).toFixed(1) + 'K';
    return String(n);
}

async function doDiscLookup() {
    const username = document.getElementById('disc-username').value;
    const br = document.getElementById('disc-br').value;
    const el = document.getElementById('disc-result');
    try {
        let q = '/trace/disconnects?limit=50';
        if (username) q += `&username=${encodeURIComponent(username)}`;
        if (br) q += `&br=${encodeURIComponent(br)}`;
        const data = await GET(q);
        if (!data.disconnects.length) {
            el.innerHTML = '<p class="text-muted">No disconnect records found</p>';
            return;
        }
        el.innerHTML = `
            <table>
                <thead><tr><th>Time</th><th>BR</th><th>Username</th><th>IP</th><th>MAC</th><th>Reason</th><th>Duration</th></tr></thead>
                <tbody>${data.disconnects.map(d => `
                    <tr>
                        <td>${formatDateTime(d.ts)}</td>
                        <td>${esc(d.br_name)}</td>
                        <td>${esc(d.username || '-')}</td>
                        <td class="mono">${esc(d.ip || '-')}</td>
                        <td class="mono">${esc(d.mac || '-')}</td>
                        <td>${esc(d.reason || '-')}</td>
                        <td>${d.duration ? formatDuration(d.duration) : '-'}</td>
                    </tr>
                `).join('')}</tbody>
            </table>`;
    } catch (err) {
        el.innerHTML = `<p class="text-danger">${esc(err.message)}</p>`;
    }
}

// ===== BR Management =====
async function loadBRMgmt() {
    const page = document.getElementById('page-br-mgmt');
    page.innerHTML = `
        <div class="page-header">
            <h1 class="page-title">BR Management</h1>
            <button class="btn btn-sm" onclick="loadBRMgmt()">Refresh</button>
        </div>
        <div id="br-grid" class="br-grid"><div class="skeleton" style="height:200px"></div></div>
    `;

    try {
        const data = await GET('/br');
        const grid = document.getElementById('br-grid');
        const instances = data.instances || {};

        if (!Object.keys(instances).length) {
            grid.innerHTML = '<p class="text-muted">No BR instances found</p>';
            return;
        }

        grid.innerHTML = Object.entries(instances).map(([name, info]) => {
            const h = info.health || {};
            const score = h.score !== undefined ? h.score : '-';
            const hStatus = h.status || 'unknown';
            const scoreClass = score >= 90 ? 'text-success' : score >= 70 ? 'text-warning' : score >= 0 ? 'text-danger' : 'text-muted';
            const scoreBarClass = score >= 90 ? 'progress-green' : score >= 70 ? 'progress-yellow' : 'progress-red';
            return `
            <div class="br-card">
                <div class="br-card-header">
                    <span class="br-card-name">${esc(name.toUpperCase())}</span>
                    <div style="display:flex;align-items:center;gap:8px">
                        <span class="status-tag ${hStatus === 'healthy' ? 'tag-up' : hStatus === 'degraded' ? 'tag-warn' : hStatus === 'down' ? 'tag-down' : 'tag-info'}" style="font-size:10px">${esc(hStatus.toUpperCase())}</span>
                        <span class="status-tag ${info.running ? 'tag-up' : 'tag-down'}">${info.running ? 'RUNNING' : 'DOWN'}</span>
                    </div>
                </div>
                <div class="br-card-body">
                    <div class="br-card-row"><span class="label">Health Score</span><span class="${scoreClass}" style="font-weight:600">${score}/100</span></div>
                    <div class="progress-bar ${scoreBarClass}" style="margin-top:2px;margin-bottom:6px">
                        <div class="progress-bar-fill" style="width:${Math.max(score, 0)}%"></div>
                    </div>
                    <div class="br-card-row"><span class="label">PID</span><span class="mono">${info.pid || '-'}</span></div>
                    <div class="br-card-row"><span class="label">Sessions</span><span>${info.session_count || 0}</span></div>
                    <div class="br-card-row"><span class="label">VLAN</span><span class="mono">${info.vlan || '-'}</span></div>
                    <div class="br-card-row"><span class="label">Gateway</span><span class="mono">${esc(info.gw_ip || '-')}</span></div>
                    <div class="br-card-row"><span class="label">Interface</span><span class="mono">${esc(info.interface || '-')}</span></div>
                    <div class="br-card-row"><span class="label">CLI Port</span><span class="mono">${info.cli_port || '-'}</span></div>
                    ${h.factors ? `
                    <div class="br-card-row"><span class="label">Drop Rate</span><span class="mono ${h.factors.drop_rate > 1 ? 'text-danger' : ''}">${h.factors.drop_rate || 0}%</span></div>
                    <div class="br-card-row"><span class="label">Error Rate</span><span class="mono ${h.factors.error_rate > 0 ? 'text-warning' : ''}">${h.factors.error_rate || 0}%</span></div>
                    ` : ''}
                </div>
                <div class="br-card-actions">
                    <button class="btn btn-sm" onclick="showBRLogs('${esc(name)}')">Logs</button>
                    <button class="btn btn-sm" onclick="showBRLogFile('${esc(name)}')">Log File</button>
                    <button class="btn btn-sm" onclick="showBRConfig('${esc(name)}')">Config</button>
                    <button class="btn btn-sm btn-primary" onclick="reloadBR('${esc(name)}')" title="Graceful reload (SIGUSR1) — re-reads config without dropping sessions">Reload</button>
                    <button class="btn btn-sm btn-danger" onclick="restartBR('${esc(name)}')">Restart</button>
                </div>
            </div>`;
        }).join('');
    } catch (err) {
        document.getElementById('br-grid').innerHTML = `<p class="text-danger">${esc(err.message)}</p>`;
    }
}

async function showBRLogs(br) {
    try {
        const data = await GET(`/br/${br}/logs?lines=200`);
        openModal(`${br.toUpperCase()} Logs (journalctl)`, `<pre>${esc(data.logs)}</pre>`);
    } catch (err) {
        toast('error', err.message);
    }
}

async function showBRLogFile(br, filter = '') {
    try {
        let url = `/br/${br}/logs/file?lines=300`;
        if (filter) url += `&grep=${encodeURIComponent(filter)}`;
        const data = await GET(url);
        const html = `
            <div class="trace-form mb-16">
                <div class="form-group">
                    <label>Filter (grep)</label>
                    <input type="text" id="br-log-filter" value="${esc(filter)}" placeholder="e.g. PADT, error, terminate">
                </div>
                <button class="btn btn-primary btn-sm" onclick="showBRLogFile('${esc(br)}', document.getElementById('br-log-filter').value)">Filter</button>
                <button class="btn btn-sm" onclick="showBRLogFile('${esc(br)}', '')">Clear</button>
            </div>
            ${filter ? `<div class="card-sub mb-8">Filtered by: "${esc(filter)}"</div>` : ''}
            <pre>${esc(data.logs)}</pre>
        `;
        openModal(`${br.toUpperCase()} Log File`, html);
    } catch (err) {
        toast('error', err.message);
    }
}

async function showBRConfig(br) {
    try {
        const data = await GET(`/br/${br}/config`);
        const html = `
            <div style="margin-bottom:12px">
                <div class="card-sub">Edit the config below. Click Save to write changes (backup is created automatically).</div>
            </div>
            <textarea id="br-config-editor" style="width:100%;min-height:400px;font-family:'SF Mono','Fira Code','Consolas',monospace;font-size:12px;line-height:1.5;background:var(--bg-primary);color:var(--text-primary);border:1px solid var(--border);border-radius:var(--radius-sm);padding:12px;resize:vertical">${esc(data.config)}</textarea>
            <div class="btn-group mt-16">
                <button class="btn btn-primary" onclick="saveBRConfig('${esc(br)}')">Save Config</button>
                <button class="btn" onclick="closeModal()">Cancel</button>
            </div>
        `;
        openModal(`${br.toUpperCase()} Config`, html);
    } catch (err) {
        toast('error', err.message);
    }
}

async function saveBRConfig(br) {
    const editor = document.getElementById('br-config-editor');
    if (!editor) return;
    const content = editor.value;
    if (!confirm(`Save config for ${br.toUpperCase()}? A backup will be created. You may need to reload/restart the BR for changes to take effect.`)) return;
    try {
        const result = await PUT(`/br/${br}/config`, { config: content });
        if (result.success) {
            toast('success', result.output || 'Config saved');
            closeModal();
        } else {
            toast('error', `Save failed: ${result.output}`);
        }
    } catch (err) {
        toast('error', err.message);
    }
}

async function reloadBR(br) {
    if (!confirm(`Graceful reload ${br.toUpperCase()} (SIGUSR1)? This re-reads the config without dropping active sessions.`)) return;
    try {
        toast('info', `Sending SIGUSR1 to ${br.toUpperCase()}...`);
        const data = await POST(`/br/${br}/reload`);
        if (data.success) {
            toast('success', `${br.toUpperCase()} reloaded: ${data.output}`);
        } else {
            toast('error', `Reload failed: ${data.output}`);
        }
        setTimeout(() => loadBRMgmt(), 2000);
    } catch (err) {
        toast('error', err.message);
    }
}

async function restartBR(br) {
    if (!confirm(`Restart accel-ppp@${br}? Active sessions on this BR will disconnect.`)) return;
    try {
        toast('info', `Restarting ${br.toUpperCase()}...`);
        const data = await POST(`/br/${br}/restart`);
        if (data.success) {
            toast('success', `${br.toUpperCase()} restarted successfully`);
        } else {
            toast('error', `Restart failed: ${data.output}`);
        }
        setTimeout(() => loadBRMgmt(), 3000);
    } catch (err) {
        toast('error', err.message);
    }
}

// ===== Alerts =====
async function loadAlerts() {
    const page = document.getElementById('page-alerts');
    page.innerHTML = `
        <div class="page-header">
            <h1 class="page-title">Alerts</h1>
            <div class="btn-group">
                <button class="btn btn-sm" onclick="loadAlerts()">Refresh</button>
                <button class="btn btn-sm" onclick="showAlertConfig()">Settings</button>
            </div>
        </div>
        <div id="alerts-list"><div class="skeleton" style="height:200px"></div></div>
    `;

    try {
        const data = await GET('/alerts?limit=100');
        const el = document.getElementById('alerts-list');

        if (!data.alerts.length) {
            el.innerHTML = '<div class="card"><p class="text-muted" style="padding:20px;text-align:center">No alerts recorded yet</p></div>';
            return;
        }

        el.innerHTML = `
            <div class="card" style="padding:0;overflow:hidden">
                ${data.alerts.map(a => `
                    <div class="alert-item${a.acknowledged ? ' text-muted' : ''}">
                        <div class="alert-severity ${a.severity}"></div>
                        <div class="alert-content">
                            <div class="alert-title">${esc(a.title)}</div>
                            <div class="alert-message">${esc(a.message)}</div>
                            <div class="alert-meta">${formatDateTime(a.ts)} | ${esc(a.category)}${a.acknowledged ? ` | Ack by ${esc(a.ack_by)} at ${formatDateTime(a.ack_at)}` : ''}</div>
                        </div>
                        <div class="alert-actions">
                            ${!a.acknowledged ? `<button class="btn btn-sm btn-success" onclick="ackAlert(${a.id})">Ack</button>` : '<span class="text-muted" style="font-size:11px">ACK</span>'}
                        </div>
                    </div>
                `).join('')}
            </div>
        `;
    } catch (err) {
        document.getElementById('alerts-list').innerHTML = `<p class="text-danger">${esc(err.message)}</p>`;
    }
}

async function ackAlert(id) {
    try {
        await POST(`/alerts/${id}/ack`);
        toast('success', 'Alert acknowledged');
        loadAlerts();
        refreshAlertBadge();
    } catch (err) {
        toast('error', err.message);
    }
}

async function showAlertConfig() {
    try {
        const cfg = await GET('/alerts/config');
        const html = `
            <div class="config-panel" style="border:none;padding:0">
                <div class="config-row">
                    <label>Alerting Enabled</label>
                    <label class="toggle"><input type="checkbox" id="acfg-enabled" ${cfg.enabled ? 'checked' : ''}><span class="toggle-slider"></span></label>
                </div>
                <div class="config-row">
                    <label>Session Drop Threshold (%)</label>
                    <input type="number" id="acfg-threshold" value="${(cfg.session_drop_threshold * 100).toFixed(0)}" min="1" max="100" style="width:80px">
                </div>
                <div class="config-row">
                    <label>Cooldown (seconds)</label>
                    <input type="number" id="acfg-cooldown" value="${cfg.cooldown_seconds}" min="60" max="3600" style="width:100px">
                </div>
                <h4 style="margin:16px 0 8px">Checks</h4>
                ${Object.entries(cfg.checks || {}).map(([k, v]) => `
                    <div class="config-row">
                        <label>${esc(k.replace(/_/g, ' '))}</label>
                        <label class="toggle"><input type="checkbox" class="acfg-check" data-key="${esc(k)}" ${v ? 'checked' : ''}><span class="toggle-slider"></span></label>
                    </div>
                `).join('')}
                <h4 style="margin:16px 0 8px">Thresholds</h4>
                <div class="config-row">
                    <label>CPU Warning (%)</label>
                    <input type="number" id="acfg-cpu" value="${cfg.thresholds?.cpu_percent || 90}" min="50" max="100" style="width:80px">
                </div>
                <div class="config-row">
                    <label>Memory Warning (%)</label>
                    <input type="number" id="acfg-mem" value="${cfg.thresholds?.memory_percent || 90}" min="50" max="100" style="width:80px">
                </div>
                <div class="config-row">
                    <label>Policer Exceed Rate Warning (%)</label>
                    <input type="number" id="acfg-exceed" value="${cfg.thresholds?.exceed_rate_percent || 10}" min="1" max="100" style="width:80px">
                </div>
                <div class="config-row">
                    <label>Max Sessions (0 = disabled)</label>
                    <input type="number" id="acfg-sessmax" value="${cfg.thresholds?.session_max || 0}" min="0" max="100000" style="width:100px">
                </div>
                <h4 style="margin:16px 0 8px">Notifications</h4>
                <div class="form-group">
                    <label>Telegram Bot Token</label>
                    <input type="text" id="acfg-tg-token" value="${esc(cfg.telegram_bot_token || '')}" placeholder="Optional">
                </div>
                <div class="form-group">
                    <label>Telegram Chat ID</label>
                    <input type="text" id="acfg-tg-chat" value="${esc(cfg.telegram_chat_id || '')}" placeholder="Optional">
                </div>
                <div class="form-group">
                    <label>Webhook URL</label>
                    <input type="text" id="acfg-webhook" value="${esc(cfg.webhook_url || '')}" placeholder="Optional">
                </div>
                <button class="btn btn-primary mt-16" onclick="saveAlertConfig()">Save Configuration</button>
            </div>
        `;
        openModal('Alert Configuration', html);
    } catch (err) {
        toast('error', err.message);
    }
}

async function saveAlertConfig() {
    const checks = {};
    document.querySelectorAll('.acfg-check').forEach(el => {
        checks[el.dataset.key] = el.checked;
    });

    const cfg = {
        enabled: document.getElementById('acfg-enabled').checked,
        session_drop_threshold: parseInt(document.getElementById('acfg-threshold').value) / 100,
        cooldown_seconds: parseInt(document.getElementById('acfg-cooldown').value),
        checks,
        thresholds: {
            cpu_percent: parseInt(document.getElementById('acfg-cpu').value),
            memory_percent: parseInt(document.getElementById('acfg-mem').value),
            exceed_rate_percent: parseInt(document.getElementById('acfg-exceed').value),
            session_max: parseInt(document.getElementById('acfg-sessmax').value),
        },
        telegram_bot_token: document.getElementById('acfg-tg-token').value,
        telegram_chat_id: document.getElementById('acfg-tg-chat').value,
        webhook_url: document.getElementById('acfg-webhook').value,
    };

    try {
        await PUT('/alerts/config', cfg);
        toast('success', 'Alert configuration saved');
        closeModal();
    } catch (err) {
        toast('error', err.message);
    }
}

// ===== History =====
async function loadHistory() {
    const page = document.getElementById('page-history');
    page.innerHTML = `
        <div class="page-header">
            <h1 class="page-title">Historical Data</h1>
            <div class="filter-bar" style="margin-bottom:0">
                <select id="history-hours" onchange="refreshHistory()">
                    <option value="1">Last 1 hour</option>
                    <option value="6">Last 6 hours</option>
                    <option value="24" selected>Last 24 hours</option>
                    <option value="72">Last 3 days</option>
                    <option value="168">Last 7 days</option>
                </select>
            </div>
        </div>
        <div class="chart-grid">
            <div>
                <div class="chart-title">Sessions Over Time</div>
                <div class="chart-container"><canvas id="chart-sessions"></canvas></div>
            </div>
            <div>
                <div class="chart-title">CPU & Memory Usage</div>
                <div class="chart-container"><canvas id="chart-system"></canvas></div>
            </div>
            <div>
                <div class="chart-title">Traffic (Bytes) per BR</div>
                <div class="chart-container"><canvas id="chart-traffic"></canvas></div>
            </div>
            <div>
                <div class="chart-title">VPP RSS Memory</div>
                <div class="chart-container"><canvas id="chart-vpp-mem"></canvas></div>
            </div>
        </div>
    `;

    refreshHistory();
}

async function refreshHistory() {
    const hours = parseInt(document.getElementById('history-hours')?.value || '24');

    try {
        const [sessData, sysData] = await Promise.all([
            GET(`/history/sessions?hours=${hours}`),
            GET(`/history/system?hours=${hours}`),
        ]);

        renderSessionChart(sessData.data);
        renderSystemChart(sysData.data);
        renderTrafficChart(sessData.data);
        renderVPPMemChart(sysData.data);
    } catch (err) {
        toast('error', `History load failed: ${err.message}`);
    }
}

const chartColors = ['#5b8def', '#3dd68c', '#f0b429', '#ef4444', '#38bdf8', '#a78bfa', '#f472b6'];

function chartDefaults() {
    return {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
            legend: { labels: { color: '#9095a8', font: { size: 11 } } },
        },
        scales: {
            x: {
                ticks: { color: '#6b7085', font: { size: 10 }, maxRotation: 0 },
                grid: { color: 'rgba(46,49,72,0.5)' },
            },
            y: {
                ticks: { color: '#6b7085', font: { size: 10 } },
                grid: { color: 'rgba(46,49,72,0.5)' },
                beginAtZero: true,
            },
        },
    };
}

function renderSessionChart(data) {
    destroyChart('sessions');
    const canvas = document.getElementById('chart-sessions');
    if (!canvas || !data.length) return;

    const brs = [...new Set(data.map(d => d.br))];
    const timestamps = [...new Set(data.map(d => d.ts))].sort();
    const labels = timestamps.map(t => formatTimeShort(t));

    const datasets = brs.map((br, i) => ({
        label: br.toUpperCase(),
        data: timestamps.map(ts => {
            const point = data.find(d => d.ts === ts && d.br === br);
            return point ? point.sessions : null;
        }),
        borderColor: chartColors[i % chartColors.length],
        backgroundColor: chartColors[i % chartColors.length] + '20',
        tension: 0.3, fill: true, pointRadius: 1,
    }));

    state.charts.sessions = new Chart(canvas, {
        type: 'line', data: { labels, datasets },
        options: chartDefaults(),
    });
}

function renderSystemChart(data) {
    destroyChart('system');
    const canvas = document.getElementById('chart-system');
    if (!canvas || !data.length) return;

    const labels = data.map(d => formatTimeShort(d.ts));
    state.charts.system = new Chart(canvas, {
        type: 'line',
        data: {
            labels,
            datasets: [
                { label: 'CPU %', data: data.map(d => d.cpu), borderColor: '#5b8def', tension: 0.3, pointRadius: 1 },
                { label: 'Memory %', data: data.map(d => d.mem_total > 0 ? (d.mem_used / d.mem_total * 100).toFixed(1) : 0), borderColor: '#3dd68c', tension: 0.3, pointRadius: 1 },
            ],
        },
        options: { ...chartDefaults(), scales: { ...chartDefaults().scales, y: { ...chartDefaults().scales.y, max: 100 } } },
    });
}

function renderTrafficChart(data) {
    destroyChart('traffic');
    const canvas = document.getElementById('chart-traffic');
    if (!canvas || !data.length) return;

    const brs = [...new Set(data.map(d => d.br))];
    const timestamps = [...new Set(data.map(d => d.ts))].sort();
    const labels = timestamps.map(t => formatTimeShort(t));

    const datasets = [];
    brs.forEach((br, i) => {
        datasets.push({
            label: `${br.toUpperCase()} RX`,
            data: timestamps.map(ts => {
                const p = data.find(d => d.ts === ts && d.br === br);
                return p ? p.rx_bytes : 0;
            }),
            borderColor: chartColors[i % chartColors.length],
            tension: 0.3, pointRadius: 1, borderDash: [],
        });
        datasets.push({
            label: `${br.toUpperCase()} TX`,
            data: timestamps.map(ts => {
                const p = data.find(d => d.ts === ts && d.br === br);
                return p ? p.tx_bytes : 0;
            }),
            borderColor: chartColors[i % chartColors.length],
            tension: 0.3, pointRadius: 1, borderDash: [5, 3],
        });
    });

    state.charts.traffic = new Chart(canvas, {
        type: 'line', data: { labels, datasets },
        options: chartDefaults(),
    });
}

function renderVPPMemChart(data) {
    destroyChart('vppMem');
    const canvas = document.getElementById('chart-vpp-mem');
    if (!canvas || !data.length) return;

    const labels = data.map(d => formatTimeShort(d.ts));
    state.charts.vppMem = new Chart(canvas, {
        type: 'line',
        data: {
            labels,
            datasets: [{
                label: 'VPP RSS (MB)', data: data.map(d => d.vpp_rss),
                borderColor: '#f0b429', backgroundColor: '#f0b42920',
                tension: 0.3, fill: true, pointRadius: 1,
            }],
        },
        options: chartDefaults(),
    });
}

function destroyChart(name) {
    if (state.charts[name]) { state.charts[name].destroy(); state.charts[name] = null; }
}

// ===== RADIUS Monitoring =====
async function loadRadius() {
    const page = document.getElementById('page-radius');
    page.innerHTML = `
        <div class="page-header">
            <h1 class="page-title">RADIUS Monitoring</h1>
            <button class="btn btn-sm" onclick="loadRadius()">Refresh</button>
        </div>
        <div id="radius-content"><div class="skeleton" style="height:200px"></div></div>
    `;

    try {
        const data = await GET('/radius');
        const el = document.getElementById('radius-content');
        const brs = data.brs || {};

        if (!Object.keys(brs).length) {
            el.innerHTML = '<p class="text-muted">No BR instances found</p>';
            return;
        }

        let html = '<div class="cards-grid">';
        for (const [br, info] of Object.entries(brs)) {
            const r = info.radius || {};
            const p = info.pppoe || {};
            const stateClass = r.state === 'active' ? 'tag-up' : 'tag-down';
            const authLost = (r.auth_lost_total || 0);
            const acctLost = (r.acct_lost_total || 0);
            const lostWarn = (authLost + acctLost) > 0 ? ' text-warning' : '';

            html += `
            <div class="card card-wide">
                <div class="card-header">
                    <span class="card-label">${esc(br.toUpperCase())} — RADIUS</span>
                    <span class="status-tag ${stateClass}">${esc(r.state || 'unknown')}</span>
                </div>
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:8px">
                    <div>
                        <div class="br-card-row"><span class="label">Server</span><span class="mono">${esc(r.server_ip || '-')}</span></div>
                        <div class="br-card-row"><span class="label">Fail Count</span><span class="mono${r.fail_count > 0 ? ' text-danger' : ''}">${r.fail_count || 0}</span></div>
                        <div class="br-card-row"><span class="label">Queue Length</span><span class="mono">${r.queue_length || 0}</span></div>
                        <div class="br-card-row"><span class="label">Sessions</span><span class="mono">${info.session_count || 0}</span></div>
                    </div>
                    <div>
                        <h4 style="font-size:12px;color:var(--text-muted);margin-bottom:6px">AUTHENTICATION</h4>
                        <div class="br-card-row"><span class="label">Auth Sent</span><span class="mono">${r.auth_sent || 0}</span></div>
                        <div class="br-card-row"><span class="label">Auth Lost</span><span class="mono${authLost > 0 ? ' text-danger' : ''}">${authLost} (5m: ${r.auth_lost_5m || 0}, 1m: ${r.auth_lost_1m || 0})</span></div>
                        <div class="br-card-row"><span class="label">Auth Latency</span><span class="mono">${r.auth_avg_time_1m || 0} ms (5m: ${r.auth_avg_time_5m || 0} ms)</span></div>
                        <h4 style="font-size:12px;color:var(--text-muted);margin:8px 0 6px">ACCOUNTING</h4>
                        <div class="br-card-row"><span class="label">Acct Sent</span><span class="mono">${r.acct_sent || 0}</span></div>
                        <div class="br-card-row"><span class="label">Acct Lost</span><span class="mono${acctLost > 0 ? ' text-danger' : ''}">${acctLost} (5m: ${r.acct_lost_5m || 0}, 1m: ${r.acct_lost_1m || 0})</span></div>
                        <div class="br-card-row"><span class="label">Acct Latency</span><span class="mono">${r.acct_avg_time_1m || 0} ms (5m: ${r.acct_avg_time_5m || 0} ms)</span></div>
                    </div>
                </div>
                ${Object.keys(p).length ? `
                <div style="margin-top:12px;padding-top:12px;border-top:1px solid var(--border)">
                    <h4 style="font-size:12px;color:var(--text-muted);margin-bottom:6px">PPPoE STATS</h4>
                    <div style="display:flex;gap:16px;flex-wrap:wrap;font-size:12px">
                        ${p.recv_padi !== undefined ? `<span>PADI: ${p.recv_padi}</span>` : ''}
                        ${p.drop_padi !== undefined ? `<span class="${p.drop_padi > 0 ? 'text-warning' : ''}">Drop PADI: ${p.drop_padi}</span>` : ''}
                        ${p.sent_pado !== undefined ? `<span>PADO: ${p.sent_pado}</span>` : ''}
                        ${p.recv_padr !== undefined ? `<span>PADR: ${p.recv_padr}${p.recv_padr_dup ? ` (dup: ${p.recv_padr_dup})` : ''}</span>` : ''}
                        ${p.sent_pads !== undefined ? `<span>PADS: ${p.sent_pads}</span>` : ''}
                        ${p.filtered !== undefined ? `<span class="${p.filtered > 0 ? 'text-warning' : ''}">Filtered: ${p.filtered}</span>` : ''}
                    </div>
                </div>` : ''}
            </div>`;
        }
        html += '</div>';

        // RADIUS history chart
        html += `
        <h2 style="font-size:16px;margin:24px 0 12px">RADIUS Latency History</h2>
        <div class="filter-bar" style="margin-bottom:12px">
            <select id="radius-hours" onchange="refreshRadiusChart()">
                <option value="1">Last 1 hour</option>
                <option value="6">Last 6 hours</option>
                <option value="24" selected>Last 24 hours</option>
                <option value="72">Last 3 days</option>
            </select>
        </div>
        <div class="chart-grid">
            <div>
                <div class="chart-title">Auth Latency (ms)</div>
                <div class="chart-container"><canvas id="chart-radius-latency"></canvas></div>
            </div>
            <div>
                <div class="chart-title">Auth/Acct Sent</div>
                <div class="chart-container"><canvas id="chart-radius-counts"></canvas></div>
            </div>
        </div>`;

        el.innerHTML = html;
        refreshRadiusChart();
    } catch (err) {
        document.getElementById('radius-content').innerHTML = `<p class="text-danger">${esc(err.message)}</p>`;
    }
}

async function refreshRadiusChart() {
    const hours = parseInt(document.getElementById('radius-hours')?.value || '24');
    try {
        const data = await GET(`/radius/history?hours=${hours}`);
        renderRadiusLatencyChart(data.data);
        renderRadiusCountsChart(data.data);
    } catch (err) {
        toast('error', `RADIUS history failed: ${err.message}`);
    }
}

function renderRadiusLatencyChart(data) {
    destroyChart('radiusLatency');
    const canvas = document.getElementById('chart-radius-latency');
    if (!canvas || !data.length) return;

    const brs = [...new Set(data.map(d => d.br))];
    const timestamps = [...new Set(data.map(d => d.ts))].sort();
    const labels = timestamps.map(t => formatTimeShort(t));

    const datasets = brs.map((br, i) => ({
        label: `${br.toUpperCase()} Auth`,
        data: timestamps.map(ts => {
            const p = data.find(d => d.ts === ts && d.br === br);
            return p ? p.auth_latency : null;
        }),
        borderColor: chartColors[i % chartColors.length],
        tension: 0.3, pointRadius: 1,
    }));

    state.charts.radiusLatency = new Chart(canvas, {
        type: 'line', data: { labels, datasets },
        options: { ...chartDefaults(), scales: { ...chartDefaults().scales, y: { ...chartDefaults().scales.y, title: { display: true, text: 'ms', color: '#6b7085' } } } },
    });
}

function renderRadiusCountsChart(data) {
    destroyChart('radiusCounts');
    const canvas = document.getElementById('chart-radius-counts');
    if (!canvas || !data.length) return;

    const brs = [...new Set(data.map(d => d.br))];
    const timestamps = [...new Set(data.map(d => d.ts))].sort();
    const labels = timestamps.map(t => formatTimeShort(t));

    const datasets = [];
    brs.forEach((br, i) => {
        datasets.push({
            label: `${br.toUpperCase()} Auth`,
            data: timestamps.map(ts => {
                const p = data.find(d => d.ts === ts && d.br === br);
                return p ? p.auth_sent : 0;
            }),
            borderColor: chartColors[i % chartColors.length],
            tension: 0.3, pointRadius: 1,
        });
        datasets.push({
            label: `${br.toUpperCase()} Acct`,
            data: timestamps.map(ts => {
                const p = data.find(d => d.ts === ts && d.br === br);
                return p ? p.acct_sent : 0;
            }),
            borderColor: chartColors[i % chartColors.length],
            tension: 0.3, pointRadius: 1, borderDash: [5, 3],
        });
    });

    state.charts.radiusCounts = new Chart(canvas, {
        type: 'line', data: { labels, datasets },
        options: chartDefaults(),
    });
}

// ===== Traffic Analytics =====
async function loadTraffic() {
    const page = document.getElementById('page-traffic');
    page.innerHTML = `
        <div class="page-header">
            <h1 class="page-title">Traffic Analytics</h1>
            <div class="btn-group">
                <button class="btn btn-sm" onclick="loadTraffic()">Refresh</button>
                <button class="btn btn-sm btn-primary" onclick="exportTrafficCSV()">Export CSV</button>
            </div>
        </div>
        <div id="traffic-content"><div class="skeleton" style="height:200px"></div></div>
    `;

    try {
        const [summary, top] = await Promise.all([
            GET('/traffic/summary'),
            GET('/traffic/top?limit=20'),
        ]);

        const el = document.getElementById('traffic-content');
        const brs = summary.brs || {};

        // Traffic summary cards
        let html = '<h2 style="font-size:16px;margin-bottom:12px">Per-BR Traffic Summary</h2>';
        html += '<div class="cards-grid">';
        for (const [br, info] of Object.entries(brs)) {
            html += `
            <div class="card">
                <div class="card-header">
                    <span class="card-label">${esc(br.toUpperCase())}</span>
                    <span class="card-sub">${info.session_count} sessions</span>
                </div>
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:8px">
                    <div>
                        <span class="label" style="font-size:10px;color:var(--text-muted);display:block">RX (from subs)</span>
                        <div class="mono" style="font-size:16px;font-weight:600;color:var(--info)">${formatBytes(info.rx_bytes)}</div>
                        <div class="card-sub">${formatNumber(info.rx_packets)} pkts</div>
                    </div>
                    <div>
                        <span class="label" style="font-size:10px;color:var(--text-muted);display:block">TX (to subs)</span>
                        <div class="mono" style="font-size:16px;font-weight:600;color:var(--success)">${formatBytes(info.tx_bytes)}</div>
                        <div class="card-sub">${formatNumber(info.tx_packets)} pkts</div>
                    </div>
                </div>
                ${info.drops > 0 ? `<div class="br-card-row mt-8"><span class="label">Drops</span><span class="mono text-danger">${formatNumber(info.drops)}</span></div>` : ''}
            </div>`;
        }
        html += '</div>';

        // Top sessions table
        const sessions = top.sessions || [];
        html += '<h2 style="font-size:16px;margin:24px 0 12px">Top Sessions by Traffic</h2>';
        if (sessions.length) {
            // Find max for bar visualization
            const maxBytes = sessions.length > 0 ? sessions[0].total_bytes : 1;
            html += `
            <div class="table-wrapper">
                <table>
                    <thead><tr>
                        <th>#</th><th>BR</th><th>Username</th><th>IP</th>
                        <th>RX</th><th>TX</th><th>Total</th><th>Rate Limit</th><th>Uptime</th><th>Bar</th>
                    </tr></thead>
                    <tbody>
                    ${sessions.map((s, i) => {
                        const pct = maxBytes > 0 ? (s.total_bytes / maxBytes * 100) : 0;
                        return `<tr>
                            <td>${i + 1}</td>
                            <td><span class="status-tag tag-info">${esc(s.br)}</span></td>
                            <td>${esc(s.username)}</td>
                            <td class="mono">${esc(s.ip)}</td>
                            <td class="mono" style="color:var(--info)">${formatBytes(s.rx_bytes)}</td>
                            <td class="mono" style="color:var(--success)">${formatBytes(s.tx_bytes)}</td>
                            <td class="mono" style="font-weight:600">${formatBytes(s.total_bytes)}</td>
                            <td class="mono">${esc(s.rate_limit || '-')}</td>
                            <td>${esc(s.uptime)}</td>
                            <td style="min-width:120px">
                                <div class="progress-bar progress-green" style="margin:0">
                                    <div class="progress-bar-fill" style="width:${pct}%"></div>
                                </div>
                            </td>
                        </tr>`;
                    }).join('')}
                    </tbody>
                </table>
            </div>`;
        } else {
            html += '<p class="text-muted">No active sessions</p>';
        }

        el.innerHTML = html;
    } catch (err) {
        document.getElementById('traffic-content').innerHTML = `<p class="text-danger">${esc(err.message)}</p>`;
    }
}

async function exportTrafficCSV() {
    try {
        const resp = await fetch('/api/traffic/export', {
            headers: { 'Authorization': `Bearer ${state.token}` },
        });
        if (!resp.ok) throw new Error('Export failed');
        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `bng_traffic_${Date.now()}.csv`;
        a.click();
        URL.revokeObjectURL(url);
        toast('success', 'CSV exported');
    } catch (err) {
        toast('error', err.message);
    }
}

// ===== Helpers =====
async function populateBRDropdown(selectId) {
    try {
        const data = await GET('/br');
        const sel = document.getElementById(selectId);
        if (sel) Object.keys(data.instances || {}).forEach(br => {
            const o = document.createElement('option'); o.value = br; o.textContent = br.toUpperCase(); sel.appendChild(o);
        });
    } catch {}
}

function openModal(title, bodyHtml) {
    document.getElementById('modal-title').textContent = title;
    document.getElementById('modal-body').innerHTML = bodyHtml;
    document.getElementById('modal-overlay').style.display = 'flex';
}

function closeModal() {
    document.getElementById('modal-overlay').style.display = 'none';
}

function toast(type, msg) {
    const container = document.getElementById('toast-container');
    const el = document.createElement('div');
    el.className = `toast toast-${type}`;
    el.textContent = msg;
    container.appendChild(el);
    setTimeout(() => { el.style.opacity = '0'; setTimeout(() => el.remove(), 300); }, 4000);
}

function esc(s) {
    if (s == null) return '';
    const d = document.createElement('div');
    d.textContent = String(s);
    return d.innerHTML;
}

function formatTime(ts) {
    if (!ts) return '';
    const d = new Date(ts * 1000);
    return d.toLocaleTimeString();
}

function formatDateTime(ts) {
    if (!ts) return '';
    const d = new Date(ts * 1000);
    return d.toLocaleString();
}

function formatTimeShort(ts) {
    if (!ts) return '';
    const d = new Date(ts * 1000);
    return `${d.getHours().toString().padStart(2, '0')}:${d.getMinutes().toString().padStart(2, '0')}`;
}

function formatDuration(secs) {
    if (!secs || secs < 0) return '0s';
    secs = Math.floor(secs);
    const d = Math.floor(secs / 86400);
    const h = Math.floor((secs % 86400) / 3600);
    const m = Math.floor((secs % 3600) / 60);
    const s = secs % 60;
    if (d > 0) return `${d}d ${h}h`;
    if (h > 0) return `${h}h ${m}m`;
    if (m > 0) return `${m}m ${s}s`;
    return `${s}s`;
}

function formatBytes(bytes) {
    if (!bytes) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0;
    while (bytes >= 1024 && i < units.length - 1) { bytes /= 1024; i++; }
    return `${bytes.toFixed(1)} ${units[i]}`;
}

// ===== User Management =====
async function loadUsers() {
    const page = document.getElementById('page-users');
    page.innerHTML = '<div class="skeleton" style="height:200px"></div>';
    try {
        const users = await GET('/users');
        renderUsers(users);
    } catch (err) {
        page.innerHTML = `<div class="page-header"><h1 class="page-title">User Management</h1></div>
        <p class="text-danger">Access denied or error: ${esc(err.message)}</p>`;
    }
}

function renderUsers(users) {
    const page = document.getElementById('page-users');
    const allPages = ['dashboard','sessions','trace','br-mgmt','alerts','history','radius','traffic','users'];
    
    let rows = users.map(u => `
        <tr>
            <td>${esc(u.username)}</td>
            <td><span class="status-tag ${u.role === 'admin' ? 'tag-up' : u.role === 'operator' ? 'tag-info' : 'tag-warn'}">${esc(u.role)}</span></td>
            <td style="white-space:normal;max-width:300px">${(u.allowed_pages || []).map(p => `<span class="status-tag tag-info" style="margin:2px;font-size:10px">${esc(p)}</span>`).join('')}</td>
            <td class="mono">${formatTime(u.created_at)}</td>
            <td>
                <div class="btn-group">
                    <button class="btn btn-sm" onclick="editUserModal(${u.id})">Edit</button>
                    ${u.username !== state.username ? `<button class="btn btn-sm btn-danger" onclick="deleteUser(${u.id}, '${esc(u.username)}')">Delete</button>` : ''}
                </div>
            </td>
        </tr>
    `).join('');

    page.innerHTML = `
        <div class="page-header">
            <h1 class="page-title">User Management</h1>
            <button class="btn btn-primary" onclick="createUserModal()">+ New User</button>
        </div>

        <div class="config-panel">
            <h3>Change My Password</h3>
            <div style="display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap">
                <div class="form-group" style="margin-bottom:0;min-width:200px">
                    <label>Current Password</label>
                    <input type="password" id="chpw-old" placeholder="Current password">
                </div>
                <div class="form-group" style="margin-bottom:0;min-width:200px">
                    <label>New Password</label>
                    <input type="password" id="chpw-new" placeholder="New password">
                </div>
                <button class="btn btn-primary" onclick="changeMyPassword()">Change Password</button>
            </div>
        </div>

        <div class="table-wrapper">
            <table>
                <thead><tr><th>Username</th><th>Role</th><th>Allowed Pages</th><th>Created</th><th>Actions</th></tr></thead>
                <tbody>${rows}</tbody>
            </table>
        </div>
    `;
}

function createUserModal() {
    const allPages = ['dashboard','sessions','trace','br-mgmt','alerts','history','radius','traffic','users'];
    const checkboxes = allPages.map(p => `
        <label style="display:inline-flex;align-items:center;gap:4px;margin:4px 8px 4px 0;font-size:13px">
            <input type="checkbox" class="new-user-page" value="${p}" ${p === 'dashboard' ? 'checked' : ''}> ${p}
        </label>
    `).join('');
    
    openModal('Create New User', `
        <div class="form-group">
            <label>Username</label>
            <input type="text" id="new-user-name" placeholder="Username">
        </div>
        <div class="form-group">
            <label>Password</label>
            <input type="password" id="new-user-pass" placeholder="Password">
        </div>
        <div class="form-group">
            <label>Role</label>
            <select id="new-user-role">
                <option value="viewer">Viewer (read-only)</option>
                <option value="operator">Operator (can manage sessions)</option>
                <option value="admin">Admin (full access)</option>
            </select>
        </div>
        <div class="form-group">
            <label>Allowed Pages</label>
            <div id="new-user-pages">${checkboxes}</div>
            <button class="btn btn-sm mt-8" onclick="document.querySelectorAll('.new-user-page').forEach(c=>c.checked=true)">Select All</button>
        </div>
        <div class="mt-16">
            <button class="btn btn-primary" onclick="createUser()">Create User</button>
        </div>
    `);
}

async function createUser() {
    const username = document.getElementById('new-user-name').value.trim();
    const password = document.getElementById('new-user-pass').value;
    const role = document.getElementById('new-user-role').value;
    const pages = Array.from(document.querySelectorAll('.new-user-page:checked')).map(c => c.value);
    
    if (!username || !password) { toast('Username and password required', 'error'); return; }
    try {
        await POST('/users', { username, password, role, allowed_pages: pages });
        toast('User created successfully', 'success');
        closeModal();
        loadUsers();
    } catch (err) {
        toast(err.message, 'error');
    }
}

async function editUserModal(userId) {
    try {
        const users = await GET('/users');
        const u = users.find(x => x.id === userId);
        if (!u) return;
        
        const allPages = ['dashboard','sessions','trace','br-mgmt','alerts','history','radius','traffic','users'];
        const checkboxes = allPages.map(p => `
            <label style="display:inline-flex;align-items:center;gap:4px;margin:4px 8px 4px 0;font-size:13px">
                <input type="checkbox" class="edit-user-page" value="${p}" ${(u.allowed_pages || []).includes(p) ? 'checked' : ''}> ${p}
            </label>
        `).join('');
        
        openModal(`Edit User: ${u.username}`, `
            <div class="form-group">
                <label>Role</label>
                <select id="edit-user-role">
                    <option value="viewer" ${u.role === 'viewer' ? 'selected' : ''}>Viewer (read-only)</option>
                    <option value="operator" ${u.role === 'operator' ? 'selected' : ''}>Operator (can manage sessions)</option>
                    <option value="admin" ${u.role === 'admin' ? 'selected' : ''}>Admin (full access)</option>
                </select>
            </div>
            <div class="form-group">
                <label>Allowed Pages</label>
                <div>${checkboxes}</div>
                <button class="btn btn-sm mt-8" onclick="document.querySelectorAll('.edit-user-page').forEach(c=>c.checked=true)">Select All</button>
            </div>
            <div class="form-group">
                <label>New Password (leave blank to keep current)</label>
                <input type="password" id="edit-user-pass" placeholder="Leave blank to keep">
            </div>
            <div class="mt-16">
                <button class="btn btn-primary" onclick="updateUser(${userId})">Save Changes</button>
            </div>
        `);
    } catch (err) {
        toast(err.message, 'error');
    }
}

async function updateUser(userId) {
    const role = document.getElementById('edit-user-role').value;
    const pages = Array.from(document.querySelectorAll('.edit-user-page:checked')).map(c => c.value);
    const password = document.getElementById('edit-user-pass').value;
    
    const body = { role, allowed_pages: pages };
    if (password) body.password = password;
    
    try {
        await PUT(`/users/${userId}`, body);
        toast('User updated successfully', 'success');
        closeModal();
        loadUsers();
    } catch (err) {
        toast(err.message, 'error');
    }
}

async function deleteUser(userId, username) {
    if (!confirm(`Delete user "${username}"? This cannot be undone.`)) return;
    try {
        const resp = await fetch(`/api/users/${userId}`, {
            method: 'DELETE',
            headers: { 'Authorization': `Bearer ${state.token}` }
        });
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            throw new Error(err.detail || 'Failed');
        }
        toast('User deleted', 'success');
        loadUsers();
    } catch (err) {
        toast(err.message, 'error');
    }
}

async function changeMyPassword() {
    const oldPw = document.getElementById('chpw-old').value;
    const newPw = document.getElementById('chpw-new').value;
    if (!oldPw || !newPw) { toast('Fill in both password fields', 'error'); return; }
    try {
        await POST('/auth/change-password', { old_password: oldPw, new_password: newPw });
        toast('Password changed successfully', 'success');
        document.getElementById('chpw-old').value = '';
        document.getElementById('chpw-new').value = '';
    } catch (err) {
        toast(err.message, 'error');
    }
}

// ===== Theme Switcher =====
function switchTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem('bng_theme', theme);
    // Update chart colors if charts exist
    updateChartColors(theme);
}

function toggleTheme() {
    const current = document.documentElement.getAttribute('data-theme') || 'dark';
    const next = current === 'dark' ? 'light' : 'dark';
    switchTheme(next);
}

function initTheme() {
    const saved = localStorage.getItem('bng_theme') || 'dark';
    document.documentElement.setAttribute('data-theme', saved);
}

function updateChartColors(theme) {
    const gridColor = theme === 'light' ? 'rgba(0,0,0,0.08)' : 'rgba(46,49,72,0.5)';
    const tickColor = theme === 'light' ? '#5a5f72' : '#6b7085';
    const legendColor = theme === 'light' ? '#5a5f72' : '#9095a8';
    for (const [name, chart] of Object.entries(state.charts)) {
        if (!chart) continue;
        try {
            if (chart.options.scales?.x) {
                chart.options.scales.x.grid.color = gridColor;
                chart.options.scales.x.ticks.color = tickColor;
            }
            if (chart.options.scales?.y) {
                chart.options.scales.y.grid.color = gridColor;
                chart.options.scales.y.ticks.color = tickColor;
            }
            if (chart.options.plugins?.legend?.labels) {
                chart.options.plugins.legend.labels.color = legendColor;
            }
            chart.update('none');
        } catch {}
    }
}

// ===== Init =====
// Apply theme immediately (before DOMContentLoaded to prevent flash)
(function() {
    const saved = localStorage.getItem('bng_theme') || 'dark';
    document.documentElement.setAttribute('data-theme', saved);
})();

document.addEventListener('DOMContentLoaded', () => {
    // Init theme selector
    initTheme();

    // Login form
    document.getElementById('login-form').addEventListener('submit', login);

    // Navigation
    document.querySelectorAll('.nav-link').forEach(link => {
        link.addEventListener('click', (e) => {
            e.preventDefault();
            const page = link.dataset.page;
            if (page) navigateTo(page);
            // Close mobile sidebar
            document.getElementById('sidebar').classList.remove('open');
        });
    });

    // Sidebar toggle (mobile)
    document.getElementById('sidebar-toggle').addEventListener('click', () => {
        document.getElementById('sidebar').classList.toggle('open');
    });

    // Logout
    document.getElementById('logout-btn').addEventListener('click', (e) => {
        e.preventDefault();
        logout();
    });

    // Modal close on overlay click
    document.getElementById('modal-overlay').addEventListener('click', (e) => {
        if (e.target === e.currentTarget) closeModal();
    });

    // Escape key closes modal
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') closeModal();
    });

    // Check if already logged in
    if (state.token) {
        showApp();
    }
});
