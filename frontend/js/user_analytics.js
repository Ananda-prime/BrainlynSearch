/* ═══════════════════════════════════════════════════════════════
   BRAINLYN AI SEARCH - INDIVIDUAL USER ANALYTICS CONTROLLER
   ═══════════════════════════════════════════════════════════════ */

let currentUserAnalyticsTargetEmail = null;
let currentUserAnalyticsData = null;
let userChartVolume = null;
let userChartCases = null;
let userChartRiskTrend = null;
let userChartStates = null;

/**
 * Opens the isolated User Usage & Performance Analytics Dashboard
 */
async function openUserAnalyticsModal(userEmail, userName = '') {
    const modal = document.getElementById('user-analytics-modal');
    if (!modal) return;

    currentUserAnalyticsTargetEmail = userEmail;

    // Reset modal header & metrics to loading state
    const char = (userName || userEmail).trim().charAt(0).toUpperCase();
    const avatarEl = document.getElementById('u-modal-avatar');
    const nameEl = document.getElementById('u-modal-name');
    const emailOrgEl = document.getElementById('u-modal-email-org');
    
    if (avatarEl) avatarEl.innerText = char || 'U';
    if (nameEl) nameEl.innerText = userName || userEmail;
    if (emailOrgEl) emailOrgEl.innerText = `${userEmail} • Loading analytics profile...`;

    document.getElementById('u-stat-searches').innerText = '...';
    document.getElementById('u-stat-reports').innerText = '...';
    document.getElementById('u-stat-risk').innerText = '...';
    document.getElementById('u-stat-downloads').innerText = '...';
    const uApiInit = document.getElementById('u-stat-api');
    if (uApiInit) uApiInit.innerText = '...';

    modal.classList.add('active');

    try {
        const data = await apiGet('/api/stats/usage', { target_user: userEmail });
        if (data && data.success) {
            currentUserAnalyticsData = data;
            renderUserAnalyticsModal(data, userEmail, userName);
        } else {
            alert((data && data.detail) || "Failed to load user analytics");
        }
    } catch (err) {
        console.error("User analytics fetch error:", err);
        alert("Unable to fetch user analytics: " + err.message);
    }
}

function renderUserAnalyticsModal(data, userEmail, userName) {
    const profile = data.target_profile || {};
    const displayName = profile.full_name || userName || userEmail.split('@')[0];
    const displayEmail = profile.email || userEmail;
    const orgText = profile.organization_name ? (profile.organization_name + (profile.organization_domain ? ` (${profile.organization_domain})` : '')) : 'Individual Account';
    const role = profile.role || 'individual';
    const isApproved = profile.is_approved !== undefined ? profile.is_approved : true;
    
    const char = displayName.trim().charAt(0).toUpperCase();
    document.getElementById('u-modal-avatar').innerText = char || 'U';
    document.getElementById('u-modal-name').innerText = displayName;
    document.getElementById('u-modal-email-org').innerText = `${displayEmail} • ${orgText}`;
    
    const roleBadge = document.getElementById('u-modal-role-badge');
    if (roleBadge) {
        roleBadge.className = `badge ${role}`;
        roleBadge.innerText = role.replace(/_/g, ' ').toUpperCase();
    }

    const statusBadge = document.getElementById('u-modal-status-badge');
    if (statusBadge) {
        statusBadge.className = `badge ${isApproved ? 'approved' : 'pending'}`;
        statusBadge.innerText = isApproved ? 'Approved' : 'Pending';
    }

    // 5 KPI Metrics
    const metrics = data.metrics || {};
    document.getElementById('u-stat-searches').innerText = Number(metrics.total_searches || 0).toLocaleString();
    document.getElementById('u-stat-reports').innerText = Number(metrics.reports_generated || 0).toLocaleString();
    document.getElementById('u-stat-risk').innerText = Number(metrics.high_risk_found || 0).toLocaleString();
    const uPendingEl = document.getElementById('u-stat-risk-pending');
    const uDisposedEl = document.getElementById('u-stat-risk-disposed');
    if (uPendingEl) uPendingEl.innerText = Number(metrics.high_risk_pending || 0).toLocaleString();
    if (uDisposedEl) uDisposedEl.innerText = Number(metrics.high_risk_disposed || 0).toLocaleString();
    document.getElementById('u-stat-downloads').innerText = Number(metrics.reports_downloaded || 0).toLocaleString();
    const uApiEl = document.getElementById('u-stat-api');
    if (uApiEl) uApiEl.innerText = Number(metrics.api_queries || 0).toLocaleString();
    const uApiSub = document.getElementById('u-stat-api-sub');
    if (uApiSub) uApiSub.innerText = `${Number(metrics.api_queries || 0).toLocaleString()} / 10,000 Quota`;

    // Initialize & render Charts
    initUserCharts(data);
}

function initUserCharts(data) {
    // 1. Search Volume Chart (Line)
    const volumeCanvas = document.getElementById('user-chart-volume');
    if (volumeCanvas) {
        const ctxVolume = volumeCanvas.getContext('2d');
        const dates = (data.search_volume || []).map(x => {
            const parts = x.date.split('-');
            return `${parts[2]}/${parts[1]}`;
        });
        const counts = (data.search_volume || []).map(x => x.count);

        if (userChartVolume) userChartVolume.destroy();
        userChartVolume = new Chart(ctxVolume, {
            type: 'line',
            data: {
                labels: dates,
                datasets: [{
                    label: 'Searches',
                    data: counts,
                    borderColor: '#6366f1',
                    borderWidth: 3,
                    pointBackgroundColor: '#6366f1',
                    pointBorderColor: 'rgba(255,255,255,0.1)',
                    pointHoverRadius: 6,
                    tension: 0.35,
                    fill: true,
                    backgroundColor: (context) => {
                        const ctx = context.chart.ctx;
                        const gradient = ctx.createLinearGradient(0, 0, 0, 260);
                        gradient.addColorStop(0, 'rgba(99, 102, 241, 0.28)');
                        gradient.addColorStop(1, 'rgba(99, 102, 241, 0)');
                        return gradient;
                    }
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: { legend: { display: false } },
                scales: {
                    x: { grid: { display: false }, ticks: { font: { size: 10 }, color: '#94a3b8', maxTicksLimit: 7, maxRotation: 0 } },
                    y: { 
                        grid: { color: 'rgba(255, 255, 255, 0.05)' },
                        ticks: { precision: 0, font: { size: 10 }, color: '#94a3b8' },
                        beginAtZero: true
                    }
                }
            }
        });
    }

    // 2. Case Types Donut
    const casesCanvas = document.getElementById('user-chart-cases');
    if (casesCanvas) {
        const ctxCases = casesCanvas.getContext('2d');
        const interactiveCount = (data.case_types && data.case_types.interactive) || 0;
        const batchCount = (data.case_types && data.case_types.batch) || 0;
        const totalQueries = interactiveCount + batchCount;

        if (userChartCases) userChartCases.destroy();
        userChartCases = new Chart(ctxCases, {
            type: 'doughnut',
            data: {
                labels: ['Interactive Search', 'Batch Screener'],
                datasets: [{
                    data: totalQueries > 0 ? [interactiveCount, batchCount] : [1],
                    backgroundColor: totalQueries > 0 ? ['#6366f1', '#a78bfa'] : ['rgba(255,255,255,0.08)'],
                    borderWidth: 2,
                    borderColor: '#1e293b'
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { position: 'bottom', labels: { boxWidth: 12, padding: 12, font: { size: 11 }, color: '#94a3b8' } }
                },
                cutout: '72%'
            }
        });
    }

    // 3. Litigation Risk Trend (Criminal vs Civil)
    const riskCanvas = document.getElementById('user-chart-risk');
    if (riskCanvas) {
        const ctxRisk = riskCanvas.getContext('2d');
        const riskSliced = (data.risk_trend || []).slice(-14);
        const riskLabels = riskSliced.map(x => {
            const parts = x.date.split('-');
            return `${parts[2]}/${parts[1]}`;
        });
        const riskCriminal = riskSliced.map(x => x.high || 0);
        const riskCivil = riskSliced.map(x => (x.low || 0) + (x.medium || 0));

        if (userChartRiskTrend) userChartRiskTrend.destroy();
        userChartRiskTrend = new Chart(ctxRisk, {
            type: 'bar',
            data: {
                labels: riskLabels,
                datasets: [
                    {
                        label: 'Criminal Litigation (High Risk)',
                        data: riskCriminal,
                        backgroundColor: 'rgba(239, 68, 68, 0.85)',
                        borderColor: '#ef4444',
                        borderWidth: 1,
                        borderRadius: 4,
                        stack: 'riskStack'
                    },
                    {
                        label: 'Civil Litigation (Low Risk)',
                        data: riskCivil,
                        backgroundColor: 'rgba(16, 185, 129, 0.85)',
                        borderColor: '#10b981',
                        borderWidth: 1,
                        borderRadius: 4,
                        stack: 'riskStack'
                    }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: { legend: { position: 'bottom', labels: { boxWidth: 10, font: { size: 10 }, color: '#94a3b8' } } },
                scales: {
                    x: { stacked: true, grid: { display: false }, ticks: { font: { size: 10 }, color: '#94a3b8', maxTicksLimit: 7 } },
                    y: { stacked: true, grid: { color: 'rgba(255, 255, 255, 0.05)' }, ticks: { precision: 0, font: { size: 10 }, color: '#94a3b8' }, beginAtZero: true }
                }
            }
        });
    }

    // 4. Top States (Horizontal Bar)
    const statesCanvas = document.getElementById('user-chart-states');
    if (statesCanvas) {
        const ctxStates = statesCanvas.getContext('2d');
        
        const stateMap = new Map();
        const normalizeState = (name) => {
            if (!name) return '';
            const key = String(name).trim().toLowerCase().replace(/[_-]+/g, ' ').replace(/\s+/g, ' ');
            if (key === 'tamilnadu' || key === 'tamil nadu') return 'Tamil Nadu';
            if (key === 'andhra' || key === 'andhra pradesh') return 'Andhra Pradesh';
            return key.split(' ').map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
        };

        (data.top_states || []).forEach(x => {
            if (!x || !x.state) return;
            const norm = normalizeState(x.state);
            const count = parseInt(x.count, 10) || 0;
            stateMap.set(norm, (stateMap.get(norm) || 0) + count);
        });

        const sortedStates = Array.from(stateMap.entries())
            .filter(([_, count]) => count > 0)
            .sort((a, b) => b[1] - a[1]);

        let stateNames = sortedStates.map(x => x[0]);
        let stateCounts = sortedStates.map(x => x[1]);
        
        if (stateNames.length === 0) {
            stateNames = ['No Data'];
            stateCounts = [0];
        }

        if (userChartStates) userChartStates.destroy();
        userChartStates = new Chart(ctxStates, {
            type: 'bar',
            data: {
                labels: stateNames,
                datasets: [{
                    data: stateCounts,
                    backgroundColor: '#a78bfa',
                    borderRadius: 6,
                    barThickness: 14
                }]
            },
            options: {
                indexAxis: 'y',
                responsive: true,
                maintainAspectRatio: false,
                plugins: { legend: { display: false } },
                scales: {
                    x: { grid: { color: 'rgba(255, 255, 255, 0.05)' }, ticks: { precision: 0, font: { size: 10 } } },
                    y: { grid: { display: false }, ticks: { font: { size: 10 } } }
                }
            }
        });
    }
}

function closeUserAnalyticsModal() {
    const modal = document.getElementById('user-analytics-modal');
    if (modal) modal.classList.remove('active');
}

function openUserAuditLogsFromAnalytics(metric = 'searches') {
    const email = currentUserAnalyticsTargetEmail;
    closeUserAnalyticsModal();
    if (typeof openAuditLogModal === 'function') {
        openAuditLogModal(metric, false, email);
    }
}

function openUserBillingFromAnalytics() {
    const email = currentUserAnalyticsTargetEmail;
    closeUserAnalyticsModal();
    if (email && typeof openBillingReportModal === 'function') {
        openBillingReportModal('individual', email);
    }
}
