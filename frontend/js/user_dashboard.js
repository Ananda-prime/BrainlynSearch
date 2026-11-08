/* ═══════════════════════════════════════════════════════════════
   BRAINLYN AI SEARCH - MAIN USER DASHBOARD ORCHESTRATOR
   ═══════════════════════════════════════════════════════════════ */

let dashboardData = null;
let currentScope = 'user'; // 'user', 'admin'
let currentMainTab = 'analytics'; // 'analytics', 'users'

// Charts on Main Dashboard
let chartVolume = null;
let chartCases = null;
let chartRiskTrend = null;
let chartStates = null;

// User Management State
let adminUsersList = [];
let activeEditingUserId = null;
let activeDeletingUserId = null;
let isAllOrgsCollapsed = false;

async function fetchStats() {
    try {
        const resData = await apiGet('/api/stats/usage');
        if (resData && resData.success) {
            dashboardData = resData;
            renderDashboard();
        }
    } catch (err) {
        console.error("Dashboard stats error:", err);
        const loadingEl = document.getElementById('loading-view');
        const dashEl = document.getElementById('dashboard-view');
        if (loadingEl) loadingEl.style.display = 'none';
        if (dashEl) dashEl.style.display = 'block';
    }
}

function renderDashboard() {
    if (!dashboardData) return;

    const role = dashboardData.role || 'individual';
    const isSuperAdmin = (role === 'super_admin');
    const isOrgAdmin = (role === 'org_admin');
    const isAdmin = isSuperAdmin || isOrgAdmin;

    // Unhide User Management nav item in sidebar for Super Admin & Org Admin
    const menuUsers = document.getElementById('menu-users');
    const navUsers = document.getElementById('nav-item-users');
    if (menuUsers) menuUsers.style.display = isAdmin ? 'block' : 'none';
    if (navUsers) navUsers.style.display = isAdmin ? 'flex' : 'none';

    // Setup Scope Switching Tabs vs Single Workspace Header
    const tabsContainer = document.getElementById('scope-tabs-container');
    const indHeader = document.getElementById('individual-header');
    const tabAdmin = document.getElementById('tab-scope-admin');
    const tabUser = document.getElementById('tab-scope-user');

    if (isAdmin) {
        if (tabsContainer) tabsContainer.style.display = 'inline-flex';
        if (indHeader) indHeader.style.display = 'none';
        if (tabAdmin) {
            tabAdmin.style.display = 'block';
            tabAdmin.innerText = isSuperAdmin ? 'Super Admin Platform Console' : 'Org Admin Platform Console';
        }
    } else {
        if (tabsContainer) tabsContainer.style.display = 'none';
        if (indHeader) indHeader.style.display = 'flex';
    }

    const urlParams = new URLSearchParams(window.location.search);
    const requestedTab = urlParams.get('tab');

    if (isAdmin && (requestedTab === 'users' || requestedTab === 'admin')) {
        switchMainTab('users');
    } else {
        switchMainTab('analytics');
    }

    const loadingEl = document.getElementById('loading-view');
    const dashEl = document.getElementById('dashboard-view');
    if (loadingEl) loadingEl.style.display = 'none';
    if (dashEl) dashEl.style.display = 'flex';
}

function switchMainTab(tab) {
    currentMainTab = tab;

    const menuAnalytics = document.getElementById('menu-analytics');
    const menuUsers = document.getElementById('menu-users');
    if (menuAnalytics) menuAnalytics.classList.toggle('active', tab === 'analytics');
    if (menuUsers) menuUsers.classList.toggle('active', tab === 'users');

    const navAnalytics = document.getElementById('nav-item-analytics');
    const navUsers = document.getElementById('nav-item-users');
    if (navAnalytics) navAnalytics.classList.toggle('active', tab === 'analytics');
    if (navUsers) navUsers.classList.toggle('active', tab === 'users');

    // Update top navbar page title
    const navPageTitle = document.getElementById('navPageTitle');
    if (navPageTitle) {
        navPageTitle.innerText = tab === 'users' ? 'User Management' : 'Analytics & Console';
    }
    // Update navbar icon
    const navPageIcon = document.getElementById('navPageIcon');
    if (navPageIcon) navPageIcon.setAttribute('data-tab', tab);

    const indHeader = document.getElementById('individual-header');
    const tabsContainer = document.getElementById('scope-tabs-container');
    const metricsGrid = document.querySelector('.metrics-grid');
    const chartsSec = document.getElementById('analytics-charts-section');
    const orgCard = document.getElementById('org-members-card');
    const adminCard = document.getElementById('admin-console-card');
    const onboardingCard = document.getElementById('onboarding-welcome-card');

    const role = (dashboardData && dashboardData.role) || 'individual';
    const isSuperAdmin = (role === 'super_admin');
    const isOrgAdmin = (role === 'org_admin');
    const isAdmin = isSuperAdmin || isOrgAdmin;

    if (tab === 'users') {
        // Dedicated User Management workspace (Only user directory with full admin actions)
        if (indHeader) indHeader.style.display = 'none';
        if (tabsContainer) tabsContainer.style.display = 'none';
        if (metricsGrid) metricsGrid.style.display = 'none';
        if (chartsSec) chartsSec.style.display = 'none';
        if (orgCard) orgCard.style.display = 'none';
        if (onboardingCard) onboardingCard.style.display = 'none';
        if (adminCard) adminCard.style.display = 'block';

        const tableTitle = adminCard ? adminCard.querySelector('.table-title') : null;
        if (tableTitle) {
            tableTitle.innerText = isSuperAdmin 
                ? "Platform User Directory & Administration" 
                : `${dashboardData.org_name || 'Organization'} User Directory & Administration`;
        }

        history.pushState(null, '', '/user_dashboard.html?tab=users');
        fetchAdminUsers();
    } else {
        // Dedicated Analytics workspace
        history.pushState(null, '', '/user_dashboard.html');
        if (isAdmin) {
            switchScope('admin');
        } else {
            switchScope('user');
        }
    }
}

function switchScope(scope) {
    currentScope = scope;

    const role = (dashboardData && dashboardData.role) || 'individual';
    const isSuperAdmin = (role === 'super_admin');
    const isOrgAdmin = (role === 'org_admin');
    const isAdmin = isSuperAdmin || isOrgAdmin;

    const indHeader = document.getElementById('individual-header');
    const tabsContainer = document.getElementById('scope-tabs-container');
    const tabUser = document.getElementById('tab-scope-user');
    const tabAdmin = document.getElementById('tab-scope-admin');

    if (isAdmin) {
        if (tabsContainer) tabsContainer.style.display = 'inline-flex';
        if (indHeader) indHeader.style.display = 'none';
        if (tabAdmin) {
            tabAdmin.style.display = 'block';
            tabAdmin.innerText = isSuperAdmin ? 'Super Admin Platform Console' : 'Org Admin Platform Console';
        }
    } else {
        if (tabsContainer) tabsContainer.style.display = 'none';
        if (indHeader) indHeader.style.display = 'flex';
    }

    if (tabUser) tabUser.classList.toggle('active', scope === 'user');
    if (tabAdmin) tabAdmin.classList.toggle('active', scope === 'admin');

    const isConsoleView = (scope === 'admin');
    
    const metricsGrid = document.querySelector('.metrics-grid');
    const chartsSec = document.getElementById('analytics-charts-section');
    const orgCard = document.getElementById('org-members-card');
    const adminCard = document.getElementById('admin-console-card');

    if (metricsGrid) metricsGrid.style.display = 'grid';
    if (chartsSec) chartsSec.style.display = isConsoleView ? 'none' : 'block';
    if (orgCard) orgCard.style.display = 'none';
    if (adminCard) {
        adminCard.style.display = isConsoleView ? 'block' : 'none';
        const tableTitle = adminCard.querySelector('.table-title');
        if (tableTitle) {
            tableTitle.innerText = isSuperAdmin 
                ? "Platform User Analytics & Audit Trail" 
                : `${dashboardData.org_name || 'Organization'} User Analytics & Audit Trail`;
        }
    }

    updateTopMetrics();
    if (isConsoleView) {
        fetchAdminUsers();
    } else {
        // Re-render / resize charts
        requestAnimationFrame(() => {
            setTimeout(() => {
                if (dashboardData) initDashboardCharts(dashboardData);
            }, 60);
        });
    }
}

function updateTopMetrics() {
    if (!dashboardData) return;

    const role = dashboardData.role || 'individual';
    const isSuperAdmin = (role === 'super_admin');
    const isOrgAdmin = (role === 'org_admin');

    let totalSearches = 0;
    let reportsGenerated = 0;
    let reportsDownloaded = 0;
    let highRiskFound = 0;
    let highRiskPending = 0;
    let highRiskDisposed = 0;
    let apiQueries = 0;

    if (currentScope === 'admin') {
        if (isSuperAdmin && dashboardData.system_metrics) {
            totalSearches = dashboardData.system_metrics.total_searches || 0;
            highRiskFound = dashboardData.system_metrics.total_high_risk || 0;
            highRiskPending = dashboardData.system_metrics.total_high_risk_pending || 0;
            highRiskDisposed = dashboardData.system_metrics.total_high_risk_disposed || 0;
            reportsGenerated = dashboardData.metrics.reports_generated || 0;
            reportsDownloaded = dashboardData.metrics.reports_downloaded || 0;
            apiQueries = dashboardData.system_metrics.total_system_api_queries || (dashboardData.metrics && dashboardData.metrics.api_queries) || 0;
        } else if (dashboardData.org_metrics) {
            totalSearches = dashboardData.org_metrics.total_searches || 0;
            reportsGenerated = dashboardData.org_metrics.reports_generated || 0;
            reportsDownloaded = dashboardData.org_metrics.reports_downloaded || 0;
            highRiskFound = dashboardData.org_metrics.high_risk_found || 0;
            highRiskPending = dashboardData.org_metrics.high_risk_pending || 0;
            highRiskDisposed = dashboardData.org_metrics.high_risk_disposed || 0;
            apiQueries = dashboardData.org_metrics.org_api_queries || (dashboardData.metrics && dashboardData.metrics.api_queries) || 0;
        } else {
            totalSearches = dashboardData.metrics.total_searches || 0;
            reportsGenerated = dashboardData.metrics.reports_generated || 0;
            reportsDownloaded = dashboardData.metrics.reports_downloaded || 0;
            highRiskFound = dashboardData.metrics.high_risk_found || 0;
            highRiskPending = dashboardData.metrics.high_risk_pending || 0;
            highRiskDisposed = dashboardData.metrics.high_risk_disposed || 0;
            apiQueries = (dashboardData.metrics && dashboardData.metrics.api_queries) || 0;
        }
    } else {
        totalSearches = dashboardData.metrics.total_searches || 0;
        reportsGenerated = dashboardData.metrics.reports_generated || 0;
        reportsDownloaded = dashboardData.metrics.reports_downloaded || 0;
        highRiskFound = dashboardData.metrics.high_risk_found || 0;
        highRiskPending = dashboardData.metrics.high_risk_pending || 0;
        highRiskDisposed = dashboardData.metrics.high_risk_disposed || 0;
        apiQueries = (dashboardData.metrics && dashboardData.metrics.api_queries) || 0;
    }

    const sEl = document.getElementById('stat-searches');
    const rEl = document.getElementById('stat-reports');
    const hrEl = document.getElementById('stat-risk');
    const hrPendingEl = document.getElementById('stat-risk-pending');
    const hrDisposedEl = document.getElementById('stat-risk-disposed');
    const dEl = document.getElementById('stat-downloads');
    const apiEl = document.getElementById('stat-api-queries');
    const apiSubEl = document.getElementById('stat-api-sub');

    if (sEl) sEl.innerText = Number(totalSearches).toLocaleString();
    if (rEl) rEl.innerText = Number(reportsGenerated).toLocaleString();
    if (hrEl) hrEl.innerText = Number(highRiskFound).toLocaleString();
    if (hrPendingEl) hrPendingEl.innerText = Number(highRiskPending).toLocaleString();
    if (hrDisposedEl) hrDisposedEl.innerText = Number(highRiskDisposed).toLocaleString();
    if (dEl) dEl.innerText = Number(reportsDownloaded).toLocaleString();
    if (apiEl) apiEl.innerText = Number(apiQueries).toLocaleString();
    if (apiSubEl) apiSubEl.innerText = `${Number(apiQueries).toLocaleString()} / 10,000 Quota`;

    // Show or hide the Onboarding Quick Start banner if searches == 0
    const onboardingEl = document.getElementById('onboarding-welcome-card');
    if (onboardingEl) {
        if (totalSearches === 0 && currentScope !== 'admin') {
            onboardingEl.style.display = 'flex';
        } else {
            onboardingEl.style.display = 'none';
        }
    }
}

let cachedDashboardChartData = null;
let currentVolRangeDays = 7;
let currentRiskRangeDays = 14;

function setVolumeChartRange(days, btn) {
    currentVolRangeDays = days;
    const pills = document.querySelectorAll('#vol-range-pills .range-pill');
    pills.forEach(p => p.classList.remove('active'));
    if (btn) btn.classList.add('active');

    if (cachedDashboardChartData) {
        renderVolumeChart(cachedDashboardChartData, days);
    }
}

function setRiskChartRange(days, btn) {
    currentRiskRangeDays = days;
    const pills = document.querySelectorAll('#risk-range-pills .range-pill');
    pills.forEach(p => p.classList.remove('active'));
    if (btn) btn.classList.add('active');

    if (cachedDashboardChartData) {
        renderRiskChart(cachedDashboardChartData, days);
    }
}

function renderVolumeChart(data, days = 7) {
    const volCanvas = document.getElementById('chart-volume');
    if (!volCanvas) return;

    const ctxVolume = volCanvas.getContext('2d');
    let rawList = (data.search_volume || []);
    
    // Slice according to days selected
    let sliced = rawList.slice(-days);
    if (sliced.length === 0) {
        const now = new Date();
        for (let i = days - 1; i >= 0; i--) {
            const d = new Date();
            d.setDate(now.getDate() - i);
            sliced.push({
                date: d.toISOString().split('T')[0],
                count: 0
            });
        }
    }

    let dates = sliced.map(x => {
        const parts = x.date.split('-');
        return `${parts[2]}/${parts[1]}`;
    });
    let counts = sliced.map(x => x.count);

    // Summary calculation
    const totalInPeriod = counts.reduce((a, b) => a + b, 0);
    let peakCount = 0;
    let peakDate = '';
    sliced.forEach(item => {
        if (item.count > peakCount) {
            peakCount = item.count;
            const parts = item.date.split('-');
            peakDate = `${parts[2]}/${parts[1]}`;
        }
    });

    const summaryEl = document.getElementById('vol-chart-summary');
    if (summaryEl) {
        if (peakCount > 0) {
            summaryEl.innerText = `${totalInPeriod.toLocaleString()} searches in last ${days}D • Peak: ${peakCount} on ${peakDate}`;
        } else {
            summaryEl.innerText = `0 searches recorded in the last ${days} days`;
        }
    }

    if (chartVolume) chartVolume.destroy();
    chartVolume = new Chart(ctxVolume, {
        type: 'line',
        data: {
            labels: dates,
            datasets: [{
                label: 'Searches',
                data: counts,
                borderColor: '#6366f1',
                borderWidth: 2.8,
                pointBackgroundColor: '#6366f1',
                pointBorderColor: '#ffffff',
                pointBorderWidth: 1.5,
                pointRadius: days <= 14 ? 3.5 : 2.5,
                pointHoverRadius: 6,
                tension: 0.35,
                fill: true,
                backgroundColor: (context) => {
                    const ctx = context.chart.ctx;
                    const gradient = ctx.createLinearGradient(0, 0, 0, 230);
                    gradient.addColorStop(0, 'rgba(99, 102, 241, 0.32)');
                    gradient.addColorStop(1, 'rgba(99, 102, 241, 0.01)');
                    return gradient;
                }
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: { 
                legend: { display: false },
                tooltip: {
                    backgroundColor: 'rgba(15, 23, 42, 0.95)',
                    titleColor: '#94a3b8',
                    bodyColor: '#f8fafc',
                    borderColor: 'rgba(255, 255, 255, 0.1)',
                    borderWidth: 1,
                    padding: 10,
                    boxPadding: 4,
                    usePointStyle: true,
                    callbacks: {
                        label: function(context) {
                            return ` ${context.parsed.y} search${context.parsed.y === 1 ? '' : 'es'}`;
                        }
                    }
                }
            },
            scales: {
                x: { 
                    grid: { display: false }, 
                    ticks: { 
                        color: '#94a3b8', 
                        font: { size: 10 },
                        maxTicksLimit: Math.min(days, 7),
                        maxRotation: 0
                    } 
                },
                y: { 
                    grid: { color: 'rgba(255, 255, 255, 0.05)' },
                    ticks: { color: '#94a3b8', precision: 0, font: { size: 10 } },
                    beginAtZero: true
                }
            }
        }
    });
}

function renderRiskChart(data, days = 14) {
    const riskCanvas = document.getElementById('chart-risk');
    if (!riskCanvas) return;

    const ctxRisk = riskCanvas.getContext('2d');
    let riskRaw = (data.risk_trend || []);
    
    // Slice according to days selected
    let riskSliced = riskRaw.slice(-days);
    if (riskSliced.length === 0) {
        const now = new Date();
        for (let i = days - 1; i >= 0; i--) {
            const d = new Date();
            d.setDate(now.getDate() - i);
            riskSliced.push({
                date: d.toISOString().split('T')[0],
                high: 0,
                low: 0,
                medium: 0
            });
        }
    }

    let riskLabels = riskSliced.map(x => {
        const parts = x.date.split('-');
        return `${parts[2]}/${parts[1]}`;
    });
    let riskCriminal = riskSliced.map(x => x.high || 0);
    let riskCivil = riskSliced.map(x => (x.low || 0) + (x.medium || 0));

    const totalHigh = riskCriminal.reduce((a, b) => a + b, 0);
    const totalCivil = riskCivil.reduce((a, b) => a + b, 0);
    const totalScreened = totalHigh + totalCivil;
    const highPct = totalScreened > 0 ? Math.round((totalHigh / totalScreened) * 100) : 0;

    // Update header risk pills & dynamic ratio
    const pillHigh = document.getElementById('risk-pill-high');
    const pillLow = document.getElementById('risk-pill-low');
    const pillRatio = document.getElementById('risk-pill-ratio');
    const summaryEl = document.getElementById('risk-chart-summary');

    if (pillHigh) pillHigh.innerText = `${totalHigh} Criminal (High Risk)`;
    if (pillLow) pillLow.innerText = `${totalCivil} Civil (Clear)`;
    if (pillRatio) {
        pillRatio.innerText = totalScreened > 0 ? `${highPct}% High Risk Ratio` : 'No Activity in Range';
        pillRatio.style.color = highPct > 20 ? '#f87171' : '#34d399';
    }
    if (summaryEl) {
        summaryEl.innerText = `${totalScreened} litigation records tracked in the last ${days} days`;
    }

    if (chartRiskTrend) chartRiskTrend.destroy();
    chartRiskTrend = new Chart(ctxRisk, {
        type: 'bar',
        data: {
            labels: riskLabels,
            datasets: [
                {
                    label: 'Criminal Proceeding (High Risk)',
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
            plugins: { 
                legend: { 
                    position: 'bottom', 
                    labels: { color: '#94a3b8', boxWidth: 10, font: { size: 10 }, padding: 10 } 
                },
                tooltip: {
                    backgroundColor: 'rgba(15, 23, 42, 0.95)',
                    titleColor: '#94a3b8',
                    bodyColor: '#f8fafc',
                    borderColor: 'rgba(255, 255, 255, 0.1)',
                    borderWidth: 1,
                    padding: 10
                }
            },
            scales: {
                x: { 
                    stacked: true,
                    grid: { display: false }, 
                    ticks: { color: '#94a3b8', font: { size: 10 }, maxTicksLimit: Math.min(days, 7) } 
                },
                y: { 
                    stacked: true,
                    grid: { color: 'rgba(255, 255, 255, 0.05)' }, 
                    ticks: { color: '#94a3b8', font: { size: 10 }, precision: 0 },
                    beginAtZero: true
                }
            }
        }
    });
}

function initDashboardCharts(data) {
    if (!data) return;
    cachedDashboardChartData = data;

    // 1. Render Search Volume with current range (default 7D)
    renderVolumeChart(data, currentVolRangeDays);

    // 2. Case Types Donut with Center KPI
    const casesCanvas = document.getElementById('chart-cases');
    if (casesCanvas) {
        const ctxCases = casesCanvas.getContext('2d');
        const interactiveCount = (data.case_types && data.case_types.interactive) || 0;
        const batchCount = (data.case_types && data.case_types.batch) || 0;
        const totalQueries = interactiveCount + batchCount;

        // Update donut center overlay
        const donutValEl = document.getElementById('donut-kpi-val');
        if (donutValEl) donutValEl.innerText = totalQueries.toLocaleString();

        // Update custom legend
        const legInteractive = document.getElementById('legend-val-interactive');
        const legBatch = document.getElementById('legend-val-batch');
        if (legInteractive) {
            const pct = totalQueries > 0 ? Math.round((interactiveCount / totalQueries) * 100) : 0;
            legInteractive.innerText = `${interactiveCount.toLocaleString()} (${pct}%)`;
        }
        if (legBatch) {
            const pct = totalQueries > 0 ? Math.round((batchCount / totalQueries) * 100) : 0;
            legBatch.innerText = `${batchCount.toLocaleString()} (${pct}%)`;
        }

        let chartData = [interactiveCount, batchCount];
        let chartLabels = ['Interactive Search', 'Batch Screener'];
        let chartColors = ['#6366f1', '#a855f7'];

        if (totalQueries === 0) {
            chartData = [1];
            chartLabels = ['No searches logged yet'];
            chartColors = ['rgba(255, 255, 255, 0.08)'];
        }

        if (chartCases) chartCases.destroy();
        chartCases = new Chart(ctxCases, {
            type: 'doughnut',
            data: {
                labels: chartLabels,
                datasets: [{
                    data: chartData,
                    backgroundColor: chartColors,
                    borderWidth: 2,
                    borderColor: '#0f172a',
                    hoverOffset: 4
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        backgroundColor: 'rgba(15, 23, 42, 0.95)',
                        titleColor: '#94a3b8',
                        bodyColor: '#f8fafc',
                        borderColor: 'rgba(255, 255, 255, 0.1)',
                        borderWidth: 1,
                        padding: 10,
                        callbacks: {
                            label: function(context) {
                                if (totalQueries === 0) return ' No searches';
                                const val = context.parsed;
                                const pct = Math.round((val / totalQueries) * 100);
                                return ` ${context.label}: ${val} (${pct}%)`;
                            }
                        }
                    }
                },
                cutout: '74%'
            }
        });
    }

    // 3. Render Litigation Risk Progression with current range (default 14D)
    renderRiskChart(data, currentRiskRangeDays);

    // 4. Top Jurisdictions & Geographical Diligence
    const statesCanvas = document.getElementById('chart-states');
    if (statesCanvas) {
        const ctxStates = statesCanvas.getContext('2d');
        
        // Canonical state deduplication and normalization
        const stateMap = new Map();
        const INDIAN_CANONICAL_FRONTEND = {
            'tamilnadu': 'Tamil Nadu',
            'tamil nadu': 'Tamil Nadu',
            'andhra pradesh': 'Andhra Pradesh',
            'andhra': 'Andhra Pradesh',
            'telangana': 'Telangana',
            'karnataka': 'Karnataka',
            'kerala': 'Kerala',
            'maharashtra': 'Maharashtra',
            'delhi': 'Delhi',
            'uttar pradesh': 'Uttar Pradesh',
            'west bengal': 'West Bengal',
            'gujarat': 'Gujarat',
            'rajasthan': 'Rajasthan'
        };

        const normalizeState = (name) => {
            if (!name) return '';
            const key = String(name).trim().toLowerCase().replace(/[_-]+/g, ' ').replace(/\s+/g, ' ');
            if (INDIAN_CANONICAL_FRONTEND[key]) return INDIAN_CANONICAL_FRONTEND[key];
            const compact = key.replace(/\s+/g, '');
            if (INDIAN_CANONICAL_FRONTEND[compact]) return INDIAN_CANONICAL_FRONTEND[compact];
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
        const totalStateSearches = stateCounts.reduce((a, b) => a + b, 0);
        const activeStatesCount = stateNames.length;

        if (stateNames.length === 0) {
            stateNames = ['No Jurisdictions Logged'];
            stateCounts = [0];
        }

        const stateCountHeader = document.getElementById('states-total-count');
        const stateInquiriesHeader = document.getElementById('states-inquiries-count');
        const stateSummaryEl = document.getElementById('states-chart-summary');

        if (stateCountHeader) {
            stateCountHeader.innerText = `${activeStatesCount} Active State${activeStatesCount === 1 ? '' : 's'}`;
        }
        if (stateInquiriesHeader) {
            stateInquiriesHeader.innerText = `${totalStateSearches.toLocaleString()} Inquiries`;
        }
        if (stateSummaryEl) {
            stateSummaryEl.innerText = `${activeStatesCount} state jurisdictions accounting for ${totalStateSearches} total search queries`;
        }

        if (chartStates) chartStates.destroy();
        chartStates = new Chart(ctxStates, {
            type: 'bar',
            data: {
                labels: stateNames,
                datasets: [{
                    label: 'Searches',
                    data: stateCounts,
                    backgroundColor: (context) => {
                        const ctx = context.chart.ctx;
                        const gradient = ctx.createLinearGradient(0, 0, 300, 0);
                        gradient.addColorStop(0, '#6366f1');
                        gradient.addColorStop(1, '#a855f7');
                        return gradient;
                    },
                    borderRadius: 6,
                    barThickness: 16
                }]
            },
            options: {
                indexAxis: 'y',
                responsive: true,
                maintainAspectRatio: false,
                plugins: { 
                    legend: { display: false },
                    tooltip: {
                        backgroundColor: 'rgba(15, 23, 42, 0.95)',
                        titleColor: '#94a3b8',
                        bodyColor: '#f8fafc',
                        borderColor: 'rgba(255, 255, 255, 0.1)',
                        borderWidth: 1,
                        padding: 10,
                        callbacks: {
                            label: function(context) {
                                const val = context.parsed.x;
                                const pct = totalStateSearches > 0 ? Math.round((val / totalStateSearches) * 100) : 0;
                                return ` ${val} searches (${pct}% of state queries)`;
                            }
                        }
                    }
                },
                scales: {
                    x: { 
                        grid: { color: 'rgba(255, 255, 255, 0.05)' }, 
                        ticks: { color: '#94a3b8', precision: 0, font: { size: 10 } },
                        beginAtZero: true
                    },
                    y: { 
                        grid: { display: false }, 
                        ticks: { color: '#cbd5e1', font: { size: 11, weight: '500' } } 
                    }
                }
            }
        });
    }

    // 5. Case Disposition & Trial Lifecycle Strip
    const metrics = data.metrics || {};
    const highRiskPending = Number(metrics.high_risk_pending || 0);
    const highRiskDisposed = Number(metrics.high_risk_disposed || 0);
    const totalRiskCases = highRiskPending + highRiskDisposed;

    let pendingPct = 50;
    let disposedPct = 50;
    if (totalRiskCases > 0) {
        pendingPct = Math.round((highRiskPending / totalRiskCases) * 100);
        disposedPct = 100 - pendingPct;
    } else {
        // Fallback to general ratio if no criminal cases
        pendingPct = 30;
        disposedPct = 70;
    }

    const dispPendingEl = document.getElementById('disp-pct-pending');
    const dispDisposedEl = document.getElementById('disp-pct-disposed');
    const dispBarPending = document.getElementById('disp-bar-pending');
    const dispBarDisposed = document.getElementById('disp-bar-disposed');

    if (dispPendingEl) dispPendingEl.innerText = `${pendingPct}% (${highRiskPending})`;
    if (dispDisposedEl) dispDisposedEl.innerText = `${disposedPct}% (${highRiskDisposed})`;
    if (dispBarPending) dispBarPending.style.width = `${pendingPct}%`;
    if (dispBarDisposed) dispBarDisposed.style.width = `${disposedPct}%`;
}

/* ── SUPER ADMIN USER MANAGEMENT CONSOLE ── */
async function fetchAdminUsers() {
    try {
        const data = await apiGet('/api/admin/users');
        if (data && data.success) {
            adminUsersList = data.users || [];
            populateOrgFilterDropdown();
            renderAdminUsersTable();
        }
    } catch (err) {
        console.error("Fetch admin users error:", err);
    }
}

function populateOrgFilterDropdown() {
    const orgSelect = document.getElementById('filter-org');
    if (!orgSelect) return;
    
    orgSelect.innerHTML = '<option value="all">All Organizations / Domains</option>';
    const orgSet = new Set();
    adminUsersList.forEach(u => {
        const orgName = u.organization_name || 'Individual';
        orgSet.add(orgName);
    });

    Array.from(orgSet).sort().forEach(org => {
        const opt = document.createElement('option');
        opt.value = org;
        opt.innerText = org;
        orgSelect.appendChild(opt);
    });

    populateRoleFilterDropdown();
}

function populateRoleFilterDropdown() {
    const roleSelect = document.getElementById('filter-role');
    if (!roleSelect) return;

    const isOrgAdmin = (dashboardData && dashboardData.role === 'org_admin');
    const currentVal = roleSelect.value || 'all';

    if (isOrgAdmin) {
        roleSelect.innerHTML = `
            <option value="all">All Roles</option>
            <option value="org_admin">Org Admin</option>
            <option value="org_user">Org Member</option>
        `;
    } else {
        roleSelect.innerHTML = `
            <option value="all">All Roles</option>
            <option value="super_admin">Super Admin</option>
            <option value="org_admin">Org Admin</option>
            <option value="org_user">Org Member</option>
            <option value="individual">Individual</option>
        `;
    }

    if (Array.from(roleSelect.options).some(o => o.value === currentVal)) {
        roleSelect.value = currentVal;
    } else {
        roleSelect.value = 'all';
    }
}

function filterUsersUI() {
    renderAdminUsersTable();
}

function renderAdminUsersTable() {
    const tbody = document.getElementById('admin-users-tbody');
    if (!tbody) return;

    const searchInput = document.getElementById('search-users');
    const orgFilter = document.getElementById('filter-org');
    const roleFilter = document.getElementById('filter-role');
    const statusFilter = document.getElementById('filter-status');
    const subFilter = document.getElementById('filter-subscription');

    const searchVal = (searchInput ? searchInput.value : '').toLowerCase().trim();
    const orgVal = orgFilter ? orgFilter.value : 'all';
    const roleVal = roleFilter ? roleFilter.value : 'all';
    const statusVal = statusFilter ? statusFilter.value : 'all';
    const subVal = subFilter ? subFilter.value : 'all';

    let filtered = adminUsersList.filter(u => {
        const orgName = u.organization_name || 'Individual';
        if (orgVal !== 'all' && orgName !== orgVal) return false;
        if (roleVal !== 'all' && u.role !== roleVal) return false;
        if (statusVal !== 'all') {
            const isApp = statusVal === 'approved';
            if (Boolean(u.is_approved) !== isApp) return false;
        }
        if (subVal !== 'all') {
            const uSub = (u.subscription_status || '').toLowerCase();
            const isPurchased = (uSub === 'purchased' || uSub === 'subscribed');
            if (subVal === 'purchased' && !isPurchased) return false;
            if (subVal === 'unpurchased' && isPurchased) return false;
        }
        if (searchVal) {
            const mName = (u.full_name || '').toLowerCase().includes(searchVal);
            const mEmail = (u.email || '').toLowerCase().includes(searchVal);
            const mOrg = (u.organization_name || '').toLowerCase().includes(searchVal);
            const mDomain = (u.organization_domain || '').toLowerCase().includes(searchVal);
            return mName || mEmail || mOrg || mDomain;
        }
        return true;
    });

    tbody.innerHTML = '';
    if (filtered.length === 0) {
        tbody.innerHTML = `<tr><td colspan="10" style="text-align: center; color: var(--text-sub); padding: 32px;">No matching user accounts found.</td></tr>`;
        return;
    }

    // Group filtered users by organization
    const orgGroups = {};
    filtered.forEach(u => {
        const orgName = u.organization_name || 'INDIVIDUAL ACCOUNTS';
        if (!orgGroups[orgName]) {
            orgGroups[orgName] = {
                domain: u.organization_domain || '',
                users: [],
                totalSearches: 0,
                totalReports: 0
            };
        }
        orgGroups[orgName].users.push(u);
        orgGroups[orgName].totalSearches += (u.total_searches || 0);
        orgGroups[orgName].totalReports += (u.total_reports || 0);
    });

    const thSub = document.getElementById('th-subscription');
    const filterSub = document.getElementById('filter-subscription');
    const isAnalyticsMode = (currentMainTab === 'analytics');

    if (thSub) thSub.style.display = isAnalyticsMode ? 'none' : '';
    if (filterSub) filterSub.style.display = isAnalyticsMode ? 'none' : '';

    Object.keys(orgGroups).sort().forEach(orgKey => {
        const group = orgGroups[orgKey];
        const orgIdSlug = 'org-group-' + orgKey.replace(/[^a-zA-Z0-9]/g, '_');

        // Group Header Row
        const groupHeaderTr = document.createElement('tr');
        groupHeaderTr.style.background = 'rgba(99, 102, 241, 0.08)';
        groupHeaderTr.style.borderTop = '1px solid rgba(99, 102, 241, 0.2)';
        groupHeaderTr.style.borderBottom = '1px solid rgba(99, 102, 241, 0.2)';
        groupHeaderTr.style.cursor = 'pointer';
        groupHeaderTr.onclick = () => toggleOrgGroup(orgIdSlug);

        groupHeaderTr.innerHTML = `
            <td colspan="10" style="padding: 10px 14px; font-weight: 700; color: #a5b4fc; font-size: 0.85rem;">
                <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px;">
                    <div style="display: flex; align-items: center; gap: 8px;">
                        <span class="org-chevron-${orgIdSlug}">▼</span>
                        <span style="letter-spacing: 0.4px;">${orgKey.toUpperCase()}</span>
                        ${group.domain ? `<span style="font-size: 0.75rem; color: var(--text-sub); font-weight: normal;">• Domain: ${group.domain}</span>` : ''}
                        <span class="badge individual" style="font-size: 0.7rem; font-weight: 600;">
                            ${group.users.length} Users | ${group.totalSearches} Searches | ${group.totalReports} Reports
                        </span>
                    </div>
                    ${isAnalyticsMode ? `
                    <div style="display: flex; align-items: center; gap: 8px;">
                        <button class="action-btn" onclick="event.stopPropagation(); openBillingReportModal('organization', '${orgKey}')" style="padding: 4px 9px; font-size: 0.72rem; background: rgba(16, 185, 129, 0.15); border-color: rgba(16, 185, 129, 0.3); color: #34d399;" title="Inspect organization statement">
                            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline><line x1="16" y1="13" x2="8" y2="13"></line><line x1="16" y1="17" x2="8" y2="17"></line></svg>
                            <span>Org Billing</span>
                        </button>
                        <button class="action-btn" onclick="event.stopPropagation(); openAuditLogModal('searches', true, '${group.domain || orgKey}')" style="padding: 4px 9px; font-size: 0.72rem;" title="Audit all activity for this organization">
                            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"></circle><line x1="21" y1="21" x2="16.65" y2="16.65"></line></svg>
                            <span>Audit Export</span>
                        </button>
                    </div>
                    ` : ''}
                </div>
            </td>
        `;
        tbody.appendChild(groupHeaderTr);

        // User Rows
        group.users.forEach(u => {
            const tr = document.createElement('tr');
            tr.className = `org-user-row ${orgIdSlug}`;
            tr.style.transition = 'background 0.15s ease';

            const idTd = document.createElement('td');
            idTd.innerText = `#${u.id}`;
            idTd.style.color = 'var(--text-muted)';
            idTd.style.fontSize = '0.8rem';

            const userTd = document.createElement('td');
            userTd.innerHTML = `
                <div style="font-weight: 600; color: var(--text-main);">${u.full_name || 'N/A'}</div>
                <div style="font-size: 0.78rem; color: var(--text-muted);">${u.email}</div>
            `;

            const roleTd = document.createElement('td');
            const rBadge = document.createElement('span');
            rBadge.className = `badge ${u.role || 'individual'}`;
            rBadge.innerText = (u.role || 'individual').replace(/_/g, ' ').toUpperCase();
            roleTd.appendChild(rBadge);

            const orgTd = document.createElement('td');
            orgTd.innerText = u.organization_name || 'Individual';
            orgTd.style.fontSize = '0.82rem';

            const subTd = document.createElement('td');
            const uSub = (u.subscription_status || '').toLowerCase();
            const isPurchased = (uSub === 'purchased' || uSub === 'subscribed');
            
            const subBadge = document.createElement('span');
            subBadge.className = `badge ${isPurchased ? 'approved' : 'pending'}`;
            subBadge.style.cursor = 'pointer';
            subBadge.style.display = 'inline-flex';
            subBadge.style.alignItems = 'center';
            subBadge.style.gap = '5px';
            subBadge.style.fontSize = '0.74rem';
            subBadge.style.fontWeight = '700';
            subBadge.title = 'Click to toggle subscription access (Purchased vs Unpurchased)';
            subBadge.innerHTML = isPurchased 
                ? `<span style="color:#34d399">●</span> PURCHASED` 
                : `<span style="color:#fbbf24">●</span> UNPURCHASED`;
            subBadge.onclick = async (e) => {
                e.stopPropagation();
                const newStatus = isPurchased ? 'unpurchased' : 'purchased';
                await updateUserSubscription(u.id, newStatus);
            };
            subTd.appendChild(subBadge);

            if (isAnalyticsMode) {
                subTd.style.display = 'none';
            }

            const searchesTd = document.createElement('td');
            searchesTd.innerText = u.total_searches || 0;
            searchesTd.style.fontWeight = '600';

            const reportsTd = document.createElement('td');
            reportsTd.innerText = u.total_reports || 0;

            const statusTd = document.createElement('td');
            const statusBadge = document.createElement('span');
            statusBadge.className = `badge ${u.is_approved ? 'approved' : 'pending'}`;
            statusBadge.innerText = u.is_approved ? 'Approved' : 'Pending';
            statusTd.appendChild(statusBadge);

            const dateTd = document.createElement('td');
            dateTd.innerText = new Date(u.created_at).toLocaleDateString(undefined, {
                year: 'numeric',
                month: 'short',
                day: 'numeric'
            });
            dateTd.style.color = 'var(--text-sub)';

            const actionsTd = document.createElement('td');
            actionsTd.style.whiteSpace = 'nowrap';

            const btnStyle = 'padding: 5px 10px; font-size: 0.75rem; font-weight: 500; border-radius: 6px; display: inline-flex; align-items: center; gap: 5px;';

            if (isAnalyticsMode) {
                // Read-only / Analytics inspection mode (ONLY Analytics & Billing)
                const analyticsBtn = document.createElement('button');
                analyticsBtn.className = 'action-btn';
                analyticsBtn.style.cssText = btnStyle;
                analyticsBtn.innerHTML = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 20V10M12 20V4M6 20v-6"></path></svg><span>Analytics</span>`;
                analyticsBtn.onclick = () => openUserAnalyticsModal(u.email, u.full_name || u.email);

                const billingBtn = document.createElement('button');
                billingBtn.className = 'action-btn';
                billingBtn.style.cssText = btnStyle;
                billingBtn.innerHTML = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline><line x1="16" y1="13" x2="8" y2="13"></line><line x1="16" y1="17" x2="8" y2="17"></line></svg><span>Billing</span>`;
                billingBtn.onclick = () => openBillingReportModal('individual', u.email);

                actionsTd.appendChild(analyticsBtn);
                actionsTd.appendChild(document.createTextNode(' '));
                actionsTd.appendChild(billingBtn);
            } else {
                // User Management mode (Pure Administration: Role, Deactivate/Approve, Delete)
                const editBtn = document.createElement('button');
                editBtn.className = 'action-btn';
                editBtn.style.cssText = btnStyle;
                editBtn.innerHTML = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"></path><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"></path></svg><span>Role</span>`;
                editBtn.onclick = () => openRoleModal(u.id, u.email, u.role, u.organization_name, u.organization_domain, u.subscription_status);

                const toggleStatusBtn = document.createElement('button');
                toggleStatusBtn.className = `action-btn ${u.is_approved ? 'danger' : 'success'}`;
                toggleStatusBtn.style.cssText = btnStyle;
                toggleStatusBtn.innerHTML = u.is_approved 
                    ? `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"></circle><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"></line></svg><span>Deactivate</span>`
                    : `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"></polyline></svg><span>Approve</span>`;
                toggleStatusBtn.onclick = () => toggleUserStatus(u.id, !u.is_approved);

                const delBtn = document.createElement('button');
                delBtn.className = 'action-btn danger';
                delBtn.style.cssText = btnStyle;
                delBtn.innerHTML = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg><span>Delete</span>`;
                delBtn.onclick = () => openDeleteModal(u.id, u.email);

                actionsTd.appendChild(editBtn);
                actionsTd.appendChild(document.createTextNode(' '));
                actionsTd.appendChild(toggleStatusBtn);
                actionsTd.appendChild(document.createTextNode(' '));
                actionsTd.appendChild(delBtn);
            }

            tr.appendChild(idTd);
            tr.appendChild(userTd);
            tr.appendChild(roleTd);
            tr.appendChild(orgTd);
            tr.appendChild(subTd);
            tr.appendChild(searchesTd);
            tr.appendChild(reportsTd);
            tr.appendChild(statusTd);
            tr.appendChild(dateTd);
            tr.appendChild(actionsTd);

            tbody.appendChild(tr);
        });
    });
}

function toggleOrgGroup(orgIdSlug) {
    const rows = document.querySelectorAll('.org-user-row.' + orgIdSlug);
    const chevron = document.querySelector('.org-chevron-' + orgIdSlug);
    let shouldHide = false;
    rows.forEach(r => {
        if (r.style.display !== 'none') shouldHide = true;
    });
    rows.forEach(r => {
        r.style.display = shouldHide ? 'none' : '';
    });
    if (chevron) chevron.innerText = shouldHide ? '▶' : '▼';
}

function toggleAllOrgsCollapse() {
    isAllOrgsCollapsed = !isAllOrgsCollapsed;
    const btn = document.getElementById('btn-toggle-all-orgs');
    if (btn) {
        btn.innerText = isAllOrgsCollapsed ? 'Expand All Orgs' : 'Collapse All Orgs';
    }
    const allUserRows = document.querySelectorAll('.org-user-row');
    allUserRows.forEach(r => {
        r.style.display = isAllOrgsCollapsed ? 'none' : '';
    });
    const allChevrons = document.querySelectorAll('[class^="org-chevron-"]');
    allChevrons.forEach(ch => {
        ch.innerText = isAllOrgsCollapsed ? '▶' : '▼';
    });
}

/* ── ROLE & USER STATUS MODAL ACTIONS ── */
function openRoleModal(userId, email, currentRole, orgName, orgDomain, currentSubscription) {
    activeEditingUserId = userId;
    document.getElementById('modal-user-email').innerText = email;
    
    const roleSelect = document.getElementById('modal-role-select');
    const isOrgAdmin = (dashboardData && dashboardData.role === 'org_admin');

    if (isOrgAdmin) {
        // Limit options to org_admin and org_user
        roleSelect.innerHTML = `
            <option value="org_user">Organization Member (org_user)</option>
            <option value="org_admin">Organization Administrator (org_admin)</option>
        `;
        document.getElementById('modal-org-name').value = dashboardData.org_name || orgName || '';
        document.getElementById('modal-org-name').disabled = true;
        document.getElementById('modal-org-domain').value = orgDomain || '';
        document.getElementById('modal-org-domain').disabled = true;
    } else {
        roleSelect.innerHTML = `
            <option value="individual">Individual (Free Tier)</option>
            <option value="org_user">Organization Member (org_user)</option>
            <option value="org_admin">Organization Administrator (org_admin)</option>
            <option value="super_admin">Platform Super Admin (super_admin)</option>
        `;
        document.getElementById('modal-org-name').disabled = false;
        document.getElementById('modal-org-domain').disabled = false;
        document.getElementById('modal-org-name').value = (orgName === 'Individual' || orgName === 'INDIVIDUAL ACCOUNTS') ? '' : (orgName || '');
        document.getElementById('modal-org-domain').value = orgDomain || '';
    }

    roleSelect.value = currentRole || 'org_user';
    document.getElementById('modal-subscription-select').value = currentSubscription || (currentRole === 'individual' ? 'unpurchased' : 'purchased');
    document.getElementById('role-modal').classList.add('active');
}

function closeRoleModal() {
    document.getElementById('role-modal').classList.remove('active');
    activeEditingUserId = null;
}

async function submitRoleUpdate() {
    if (!activeEditingUserId) return;
    const role = document.getElementById('modal-role-select').value;
    const subscription = document.getElementById('modal-subscription-select').value;
    const orgName = document.getElementById('modal-org-name').value.trim();
    const orgDomain = document.getElementById('modal-org-domain').value.trim();

    try {
        const res = await apiPut(`/api/admin/users/${activeEditingUserId}/role`, {
            role,
            subscription_status: subscription,
            organization_name: orgName,
            organization_domain: orgDomain
        });
        
        if (res && res.success) {
            closeRoleModal();
            await fetchAdminUsers();
            await fetchStats();
        } else {
            alert((res && res.detail) || "Failed to update user");
        }
    } catch (err) {
        console.error(err);
        alert("Network error updating user: " + err.message);
    }
}

async function toggleUserStatus(userId, newApprovedStatus) {
    try {
        const res = await apiPut(`/api/admin/users/${userId}/status`, {
            is_approved: newApprovedStatus
        });
        if (res && res.success) {
            await fetchAdminUsers();
        } else {
            alert((res && res.detail) || "Failed to update user status");
        }
    } catch (err) {
        console.error(err);
        alert("Network error updating status: " + err.message);
    }
}

async function updateUserSubscription(userId, newStatus) {
    try {
        const user = adminUsersList.find(u => u.id === userId);
        const res = await apiPut(`/api/admin/users/${userId}/role`, {
            role: user ? user.role : 'individual',
            subscription_status: newStatus,
            organization_name: user ? user.organization_name : '',
            organization_domain: user ? user.organization_domain : ''
        });
        if (res && res.success) {
            if (user) {
                user.subscription_status = newStatus;
            }
            renderAdminUsersTable();
        } else {
            alert((res && res.detail) || "Failed to update subscription status");
        }
    } catch (err) {
        console.error(err);
        alert("Network error updating subscription: " + err.message);
    }
}

function openDeleteModal(userId, email) {
    activeDeletingUserId = userId;
    document.getElementById('modal-delete-user-email').innerText = email;
    document.getElementById('delete-modal').classList.add('active');
}

function closeDeleteModal() {
    document.getElementById('delete-modal').classList.remove('active');
    activeDeletingUserId = null;
}

async function submitDeleteUser() {
    if (!activeDeletingUserId) return;
    try {
        const res = await apiDelete(`/api/admin/users/${activeDeletingUserId}`);
        if (res && res.success) {
            closeDeleteModal();
            await fetchAdminUsers();
        } else {
            alert((res && res.detail) || "Failed to delete user");
        }
    } catch (err) {
        console.error(err);
        alert("Network error deleting user: " + err.message);
    }
}

/* ── SIDEBAR COLLAPSE / EXPAND CONTROLLER ── */
function toggleSidebar() {
    const sidebar = document.querySelector('.sidebar');
    const mainContent = document.querySelector('.main-content');
    const chevron = document.getElementById('sidebar-chevron-icon');
    
    if (!sidebar || !mainContent) return;
    
    const isCollapsed = sidebar.classList.toggle('collapsed');
    document.body.classList.toggle('sidebar-collapsed', isCollapsed);
    mainContent.classList.toggle('expanded', isCollapsed);
    
    if (chevron) {
        chevron.innerHTML = isCollapsed 
            ? '<polyline points="9 18 15 12 9 6"></polyline>' 
            : '<polyline points="15 18 9 12 15 6"></polyline>';
    }
    
    localStorage.setItem('sidebar_collapsed', isCollapsed ? 'true' : 'false');

    // Smoothly re-render charts to fit the new width
    setTimeout(() => {
        if (typeof chartVolume !== 'undefined' && chartVolume) chartVolume.resize();
        if (typeof chartCases !== 'undefined' && chartCases) chartCases.resize();
        if (typeof chartRiskTrend !== 'undefined' && chartRiskTrend) chartRiskTrend.resize();
        if (typeof chartStates !== 'undefined' && chartStates) chartStates.resize();
    }, 260);
}

function restoreSidebarState() {
    const saved = localStorage.getItem('sidebar_collapsed');
    if (saved === 'true') {
        const sidebar = document.querySelector('.sidebar');
        const mainContent = document.querySelector('.main-content');
        const chevron = document.getElementById('sidebar-chevron-icon');
        if (sidebar && mainContent) {
            sidebar.classList.add('collapsed');
            document.body.classList.add('sidebar-collapsed');
            mainContent.classList.add('expanded');
            if (chevron) {
                chevron.innerHTML = '<polyline points="9 18 15 12 9 6"></polyline>';
            }
        }
    }
}

window.addEventListener('popstate', () => {
    const urlParams = new URLSearchParams(window.location.search);
    const requestedTab = urlParams.get('tab');
    if (requestedTab === 'admin' || requestedTab === 'users') {
        switchScope('admin');
    } else {
        switchScope('user');
    }
});
