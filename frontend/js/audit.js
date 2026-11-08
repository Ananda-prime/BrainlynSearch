/* ═══════════════════════════════════════════════════════════════
   BRAINLYN AI SEARCH - ITEMIZED AUDIT LOG CONTROLLER
   ═══════════════════════════════════════════════════════════════ */

let activeAuditMetric = 'searches';
let rawAuditLogsList = [];
let activeAuditTargetUser = '';
let allMonthsCollapsedState = false;

/**
 * Open Itemized Audit Trail Modal
 */
async function openAuditLogModal(metricType = 'searches', preserveSearch = false, targetUser = '') {
    activeAuditMetric = metricType;
    activeAuditTargetUser = targetUser || '';
    
    const searchInput = document.getElementById('audit-search-input');
    if (searchInput) {
        if (targetUser) {
            searchInput.value = targetUser;
        } else if (!preserveSearch) {
            searchInput.value = '';
        }
    }

    const titleEl = document.getElementById('audit-modal-title');
    const subTitleEl = document.getElementById('audit-modal-subtitle');

    if (metricType === 'searches') {
        if (titleEl) titleEl.innerText = 'Total Searches Audit Trail';
        if (subTitleEl) subTitleEl.innerText = 'Itemized log of interactive & batch screener lookups';
    } else if (metricType === 'reports') {
        if (titleEl) titleEl.innerText = 'Generated Reports Audit Trail';
        if (subTitleEl) subTitleEl.innerText = 'Itemized history of report exports & batch file uploads';
    } else if (metricType === 'risk') {
        if (titleEl) titleEl.innerText = 'High Risk (Criminal) Audit Log';
        if (subTitleEl) subTitleEl.innerText = 'Itemized log of criminal proceedings and litigation alerts';
    } else if (metricType === 'downloads') {
        if (titleEl) titleEl.innerText = 'Downloaded Reports Audit Log';
        if (subTitleEl) subTitleEl.innerText = 'PDF and Excel report download events for billing audit';
    } else if (metricType === 'api') {
        if (titleEl) titleEl.innerText = 'Partner REST API v1 Audit Log';
        if (subTitleEl) subTitleEl.innerText = 'Itemized log of programmatic REST API search inquiries';
    } else {
        if (titleEl) titleEl.innerText = 'Itemized SaaS Audit Log';
        if (subTitleEl) subTitleEl.innerText = 'Detailed platform activity & lookup audit trail';
    }

    const scopeBadge = document.getElementById('audit-modal-scope-badge');
    if (scopeBadge) {
        if (targetUser) {
            scopeBadge.innerText = `User: ${targetUser}`;
            scopeBadge.className = 'badge individual';
        } else if (typeof currentScope !== 'undefined' && currentScope === 'admin') {
            scopeBadge.innerText = 'Super Admin Scope';
            scopeBadge.className = 'badge super_admin';
        } else if (typeof currentScope !== 'undefined' && currentScope === 'org') {
            scopeBadge.innerText = 'Organization Scope';
            scopeBadge.className = 'badge org_admin';
        } else {
            scopeBadge.innerText = 'Personal Scope';
            scopeBadge.className = 'badge individual';
        }
    }

    const modal = document.getElementById('audit-log-modal');
    if (modal) modal.classList.add('active');
    
    await fetchAuditLogs(targetUser);
}

function closeAuditLogModal() {
    const modal = document.getElementById('audit-log-modal');
    if (modal) modal.classList.remove('active');
    activeAuditTargetUser = '';
}

async function fetchAuditLogs(targetUser = '') {
    const tbody = document.getElementById('audit-log-table-body');
    if (!tbody) return;
    tbody.innerHTML = `<tr><td colspan="6" style="text-align: center; color: var(--text-sub); padding: 32px;">Loading audit logs...</td></tr>`;

    try {
        const queryParams = {
            metric_type: activeAuditMetric,
            scope: (typeof currentScope !== 'undefined') ? currentScope : 'user'
        };
        if (targetUser || activeAuditTargetUser) {
            queryParams['target_user'] = targetUser || activeAuditTargetUser;
        }
        const data = await apiGet('/api/stats/audit_logs', queryParams);
        if (data && data.success) {
            rawAuditLogsList = data.logs || [];
            populateAuditMonthFilter();
            renderAuditLogsTable();
        } else {
            const msg = (data && (data.detail || data.message)) || "Failed to fetch audit logs.";
            tbody.innerHTML = `<tr><td colspan="6" style="text-align: center; color: var(--error-red); padding: 24px;">${msg}</td></tr>`;
        }
    } catch (err) {
        console.error("Audit log error:", err);
        tbody.innerHTML = `<tr><td colspan="6" style="text-align: center; color: var(--error-red); padding: 24px;">Network error loading audit logs.</td></tr>`;
    }
}

function populateAuditMonthFilter() {
    const select = document.getElementById('audit-month-filter');
    if (!select) return;

    select.innerHTML = '<option value="all">All Months</option>';
    const monthsSet = new Set();

    rawAuditLogsList.forEach(log => {
        if (log.created_at) {
            const d = new Date(log.created_at);
            if (!isNaN(d.getTime())) {
                const yr = d.getFullYear();
                const mo = String(d.getMonth() + 1).padStart(2, '0');
                monthsSet.add(`${yr}-${mo}`);
            }
        }
    });

    Array.from(monthsSet).sort().reverse().forEach(mKey => {
        const parts = mKey.split('-');
        const d = new Date(parseInt(parts[0]), parseInt(parts[1]) - 1, 1);
        const label = d.toLocaleString('en-US', { month: 'long', year: 'numeric' });
        const opt = document.createElement('option');
        opt.value = mKey;
        opt.innerText = label;
        select.appendChild(opt);
    });
}

function filterAuditLogsUI() {
    renderAuditLogsTable();
}

function renderAuditLogsTable() {
    const tbody = document.getElementById('audit-log-table-body');
    const searchInput = document.getElementById('audit-search-input');
    const monthSelect = document.getElementById('audit-month-filter');
    const countEl = document.getElementById('audit-count-badge');
    
    if (!tbody) return;

    const searchTerm = (searchInput ? searchInput.value : '').toLowerCase().trim();
    const selectedMonth = monthSelect ? monthSelect.value : 'all';

    let filtered = rawAuditLogsList.filter(log => {
        if (selectedMonth !== 'all' && log.created_at) {
            const d = new Date(log.created_at);
            if (!isNaN(d.getTime())) {
                const yr = d.getFullYear();
                const mo = String(d.getMonth() + 1).padStart(2, '0');
                if (`${yr}-${mo}` !== selectedMonth) return false;
            }
        }
        if (searchTerm) {
            const matchUser = (log.user_email || '').toLowerCase().includes(searchTerm);
            const matchName = (log.full_name || '').toLowerCase().includes(searchTerm);
            const matchOrg = (log.organization_name || '').toLowerCase().includes(searchTerm);
            const matchQuery = (log.search_query || '').toLowerCase().includes(searchTerm);
            const matchState = (log.state_name || '').toLowerCase().includes(searchTerm);
            const matchDetails = (log.details || '').toLowerCase().includes(searchTerm);
            const matchRisk = (log.risk_level || '').toLowerCase().includes(searchTerm);
            const matchAction = (log.query_type || '').toLowerCase().includes(searchTerm);
            return matchUser || matchName || matchOrg || matchQuery || matchState || matchDetails || matchRisk || matchAction;
        }
        return true;
    });

    if (countEl) {
        countEl.innerText = `Showing ${filtered.length} of ${rawAuditLogsList.length} logs`;
    }

    tbody.innerHTML = '';
    if (filtered.length === 0) {
        tbody.innerHTML = `<tr><td colspan="6" style="text-align: center; color: var(--text-sub); padding: 32px;">No matching transaction audit records found.</td></tr>`;
        return;
    }

    // Group logs by month
    const groups = {};
    filtered.forEach(log => {
        let mKey = 'Recent / Unknown';
        let mLabel = 'Recent Records';
        if (log.created_at) {
            const d = new Date(log.created_at);
            if (!isNaN(d.getTime())) {
                mKey = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
                mLabel = d.toLocaleString('en-US', { month: 'long', year: 'numeric' }).toUpperCase();
            }
        }
        if (!groups[mKey]) {
            groups[mKey] = { label: mLabel, items: [] };
        }
        groups[mKey].items.push(log);
    });

    const sortedMonthKeys = Object.keys(groups).sort().reverse();

    sortedMonthKeys.forEach(mKey => {
        const group = groups[mKey];
        const groupClass = `month-group-${mKey.replace(/[^a-zA-Z0-9]/g, '_')}`;

        // Month Separator Header
        const headerTr = document.createElement('tr');
        headerTr.style.background = 'rgba(99, 102, 241, 0.08)';
        headerTr.style.borderTop = '1px solid rgba(99, 102, 241, 0.25)';
        headerTr.style.borderBottom = '1px solid rgba(99, 102, 241, 0.25)';
        headerTr.style.cursor = 'pointer';
        headerTr.style.userSelect = 'none';
        headerTr.onclick = () => toggleMonthGroup(groupClass);

        headerTr.innerHTML = `
            <td colspan="6" style="padding: 10px 14px; font-weight: 700; font-size: 0.85rem; color: #a5b4fc;">
                <div style="display: flex; justify-content: space-between; align-items: center;">
                    <div style="display: flex; align-items: center; gap: 8px;">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="color: #818cf8;"><rect x="3" y="4" width="18" height="18" rx="2" ry="2"></rect><line x1="16" y1="2" x2="16" y2="6"></line><line x1="8" y1="2" x2="8" y2="6"></line><line x1="3" y1="10" x2="21" y2="10"></line></svg>
                        <span>${group.label}</span>
                        <span style="font-size: 0.75rem; color: var(--text-sub); font-weight: normal;">• ${group.items.length} Events</span>
                    </div>
                    <div style="font-size: 0.76rem; color: var(--text-sub); display: flex; align-items: center; gap: 6px;">
                        <span class="month-toggle-label-${groupClass}">COLLAPSE</span>
                        <span class="month-toggle-icon-${groupClass}">▼</span>
                    </div>
                </div>
            </td>
        `;
        tbody.appendChild(headerTr);

        // Render line item rows
        group.items.forEach(log => {
            const tr = document.createElement('tr');
            tr.className = groupClass;
            tr.style.borderBottom = '1px solid rgba(255,255,255,0.05)';
            tr.style.transition = 'background 0.15s ease';
            tr.onmouseenter = () => tr.style.background = 'rgba(255,255,255,0.03)';
            tr.onmouseleave = () => tr.style.background = 'transparent';

            // 1. Timestamp
            const dateTd = document.createElement('td');
            dateTd.style.padding = '12px 14px';
            dateTd.style.color = 'var(--text-sub)';
            dateTd.style.whiteSpace = 'nowrap';
            dateTd.style.fontSize = '0.82rem';
            if (log.created_at) {
                const d = new Date(log.created_at);
                dateTd.innerText = !isNaN(d.getTime()) 
                    ? d.toLocaleString('en-US', { month: 'short', day: 'numeric', year: 'numeric', hour: '2-digit', minute: '2-digit' })
                    : log.created_at;
            } else {
                dateTd.innerText = 'N/A';
            }

            // 2. User / Org
            const userTd = document.createElement('td');
            userTd.style.padding = '12px 14px';
            const userName = log.full_name || log.user_email || 'Unknown User';
            const userOrg = log.organization_name ? ` (${log.organization_name})` : '';
            userTd.innerHTML = `
                <div style="font-weight: 600; color: var(--text-main); font-size: 0.85rem;">${userName}</div>
                <div style="font-size: 0.74rem; color: var(--text-muted);">${log.user_email || 'N/A'}${userOrg}</div>
            `;

            // 3. Action Type
            const actionTd = document.createElement('td');
            actionTd.style.padding = '12px 14px';
            let actionBadgeClass = 'badge individual';
            let actionLabel = log.query_type || 'SEARCH';
            if (actionLabel === 'interactive_search') {
                actionLabel = 'INTERACTIVE SEARCH';
            } else if (actionLabel === 'screener_query') {
                actionLabel = 'SCREENER LOOKUP';
            } else if (actionLabel === 'report_generated') {
                actionLabel = 'AI REPORT';
                actionBadgeClass = 'badge super_admin';
            } else if (actionLabel === 'report_downloaded') {
                actionLabel = 'DOWNLOAD';
                actionBadgeClass = 'badge approved';
            }
            actionTd.innerHTML = `<span class="${actionBadgeClass}">${actionLabel}</span>`;

            // 4. Query Target / Search Term
            const targetTd = document.createElement('td');
            targetTd.style.padding = '12px 14px';
            targetTd.style.fontWeight = '600';
            targetTd.style.color = '#93c5fd';
            targetTd.style.fontSize = '0.85rem';
            targetTd.style.maxWidth = '260px';
            targetTd.style.wordBreak = 'break-word';
            targetTd.innerText = log.search_query || 'Default Query';

            // 5. State / Details
            const detailsTd = document.createElement('td');
            detailsTd.style.padding = '12px 14px';
            detailsTd.style.fontSize = '0.8rem';
            detailsTd.style.color = 'var(--text-sub)';
            const stateStr = log.state_name ? `State: ${log.state_name}` : '';
            const detailsStr = log.details || '';
            detailsTd.innerText = [stateStr, detailsStr].filter(Boolean).join(' | ') || 'Default Params';

            // 6. Risk Level
            const riskTd = document.createElement('td');
            riskTd.style.padding = '12px 14px';
            riskTd.style.textAlign = 'center';
            const rLvl = (log.risk_level || 'low').toLowerCase();
            let riskBadgeClass = 'approved';
            let riskLabel = 'Civil (Low Risk)';
            if (rLvl === 'high') {
                riskBadgeClass = 'danger';
                const statusTag = log.case_status ? ` (${log.case_status.charAt(0).toUpperCase() + log.case_status.slice(1)})` : '';
                riskLabel = `Criminal${statusTag}`;
            } else if (rLvl === 'medium') {
                riskBadgeClass = 'pending';
                riskLabel = 'Review Needed';
            }
            riskTd.innerHTML = `<span class="badge ${riskBadgeClass}">${riskLabel}</span>`;

            tr.appendChild(dateTd);
            tr.appendChild(userTd);
            tr.appendChild(actionTd);
            tr.appendChild(targetTd);
            tr.appendChild(detailsTd);
            tr.appendChild(riskTd);

            tbody.appendChild(tr);
        });
    });

    // Reset toggle all button state
    allMonthsCollapsedState = false;
    const btn = document.getElementById('audit-toggle-all-btn');
    if (btn) btn.innerText = 'Collapse All';
}

function toggleMonthGroup(groupClass) {
    const rows = document.querySelectorAll('.' + groupClass);
    const labelEl = document.querySelector('.month-toggle-label-' + groupClass);
    const iconEl = document.querySelector('.month-toggle-icon-' + groupClass);

    let shouldCollapse = false;
    rows.forEach(r => {
        if (r.style.display !== 'none') {
            shouldCollapse = true;
        }
    });

    rows.forEach(r => {
        r.style.display = shouldCollapse ? 'none' : '';
    });

    if (labelEl) labelEl.innerText = shouldCollapse ? 'EXPAND' : 'COLLAPSE';
    if (iconEl) iconEl.innerText = shouldCollapse ? '▶' : '▼';
}

function toggleAllMonthsCollapse() {
    allMonthsCollapsedState = !allMonthsCollapsedState;
    const btn = document.getElementById('audit-toggle-all-btn');
    if (btn) {
        btn.innerText = allMonthsCollapsedState ? 'Expand All' : 'Collapse All';
    }

    const allMonthRows = document.querySelectorAll('[class^="month-group-"]');
    allMonthRows.forEach(r => {
        r.style.display = allMonthsCollapsedState ? 'none' : '';
    });

    const allLabels = document.querySelectorAll('[class^="month-toggle-label-"]');
    allLabels.forEach(l => {
        l.innerText = allMonthsCollapsedState ? 'EXPAND' : 'COLLAPSE';
    });

    const allIcons = document.querySelectorAll('[class^="month-toggle-icon-"]');
    allIcons.forEach(i => {
        i.innerText = allMonthsCollapsedState ? '▶' : '▼';
    });
}

function exportAuditLogsCSV() {
    if (!rawAuditLogsList || rawAuditLogsList.length === 0) {
        alert("No audit logs available to export.");
        return;
    }

    let csvContent = "data:text/csv;charset=utf-8,";
    csvContent += "ID,Timestamp,User Email,Full Name,Organization,Action Type,Search Query,State,Details,Risk Level\n";

    rawAuditLogsList.forEach(log => {
        const row = [
            log.id || '',
            `"${log.created_at || ''}"`,
            `"${log.user_email || ''}"`,
            `"${(log.full_name || '').replace(/"/g, '""')}"`,
            `"${(log.organization_name || '').replace(/"/g, '""')}"`,
            `"${log.query_type || ''}"`,
            `"${(log.search_query || '').replace(/"/g, '""')}"`,
            `"${(log.state_name || '').replace(/"/g, '""')}"`,
            `"${(log.details || '').replace(/"/g, '""')}"`,
            `"${log.risk_level || ''}"`
        ].join(",");
        csvContent += row + "\n";
    });

    const encodedUri = encodeURI(csvContent);
    const link = document.createElement("a");
    link.setAttribute("href", encodedUri);
    link.setAttribute("download", `Brainlyn_Audit_Trail_${activeAuditMetric}_${new Date().toISOString().slice(0,10)}.csv`);
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
}
