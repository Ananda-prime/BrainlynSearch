/* ═══════════════════════════════════════════════════════════════
   BRAINLYN AI SEARCH - EXECUTIVE SAAS BILLING CONTROLLER
   ═══════════════════════════════════════════════════════════════ */

let currentBillingReportData = null;
let currentBillingEntityType = 'organization';
let currentBillingTargetId = '';
let currentBillingMonth = 'all';
let rawBillingItemizedLogs = [];

// Platform Admin Rate Card Settings (Live Editable & State Retained)
const DEFAULT_BILLING_RATES = {
    search: 0.05,
    report: 0.25,
    api: 0.03,
    download: 0.00,
    tax_rate: 8.0
};
let activeBillingRates = { ...DEFAULT_BILLING_RATES };

function safeSetText(id, text) {
    const el = document.getElementById(id);
    if (el) el.innerText = text !== undefined && text !== null ? String(text) : '';
}

function safeSetHtml(id, html) {
    const el = document.getElementById(id);
    if (el) el.innerHTML = html !== undefined && html !== null ? String(html) : '';
}

/**
 * Open Executive SaaS Billing Report Modal
 */
async function openBillingReportModal(entityType = 'organization', targetId = '', billingMonth = 'all') {
    const modal = document.getElementById('billing-report-modal');
    if (!modal) return;

    currentBillingEntityType = entityType;
    currentBillingTargetId = targetId;
    currentBillingMonth = billingMonth;

    // Reset and show initial loading state
    safeSetText('inv-number', 'INV-SYNCING...');
    safeSetText('inv-date-gen', 'Reconciling logs...');
    safeSetText('inv-month-lbl', `Billed Period: ${billingMonth === 'all' ? 'All Time' : billingMonth}`);
    safeSetText('inv-billed-org', targetId || 'Target Client');
    safeSetText('inv-billed-domain', 'Domain: Loading...');
    safeSetText('inv-billed-tenant', 'Tenant ID: Loading...');
    safeSetText('inv-billed-admin', 'Admin: Loading...');
    safeSetText('inv-subtotal', '$0.00');
    safeSetText('inv-tax', '$0.00');
    safeSetText('inv-total', '$0.00');
    safeSetText('billing-audit-log-count', '0');
    
    safeSetHtml('inv-line-items-body', `
        <tr>
            <td colspan="4" style="text-align: center; color: var(--text-sub); padding: 28px;">
                <div style="display: flex; align-items: center; justify-content: center; gap: 10px;">
                    <div class="spinner" style="width: 22px; height: 22px; border-width: 2px;"></div>
                    <span>Auditing search transactions and computing billing rate cards...</span>
                </div>
            </td>
        </tr>
    `);

    // Reset audit logs accordion container
    const auditContainer = document.getElementById('billing-audit-logs-container');
    const chevron = document.getElementById('billing-audit-chevron');
    if (auditContainer) auditContainer.style.display = 'none';
    if (chevron) chevron.innerText = '▶';

    modal.classList.add('active');

    try {
        const queryParams = {
            entity_type: entityType,
            target_id: targetId || '',
            billing_month: billingMonth,
            rate_search: activeBillingRates.search,
            rate_report: activeBillingRates.report,
            rate_api: activeBillingRates.api
        };
        const data = await apiGet('/api/admin/billing/report', queryParams);
        if (data && data.success) {
            currentBillingReportData = data;
            renderBillingInvoiceModal(data);
        } else {
            alert((data && data.detail) || "Failed to load billing report");
        }
    } catch (err) {
        console.error("Billing report fetch error:", err);
        safeSetHtml('inv-line-items-body', `
            <tr>
                <td colspan="4" style="text-align: center; color: var(--error-red); padding: 24px;">
                    Failed to compute billing report: ${err.message}
                </td>
            </tr>
        `);
    }
}

function onBillingCycleChange(newCycle) {
    openBillingReportModal(currentBillingEntityType, currentBillingTargetId, newCycle);
}

function onCustomRateChange(key, val) {
    const num = parseFloat(val);
    activeBillingRates[key] = isNaN(num) ? 0 : Math.max(0, num);
    recalculateBillingFinancials();
}

function updateTaxRate(val) {
    const num = parseFloat(val);
    activeBillingRates.tax_rate = isNaN(num) ? 0 : Math.max(0, num);
    recalculateBillingFinancials();
}

function resetDefaultBillingRates() {
    activeBillingRates = { ...DEFAULT_BILLING_RATES };
    if (currentBillingReportData) {
        renderBillingInvoiceModal(currentBillingReportData);
    }
}

function recalculateBillingFinancials() {
    if (!currentBillingReportData) return;
    
    const items = currentBillingReportData.line_items || [];
    let subtotal = 0;
    const keys = ['search', 'report', 'api', 'download'];
    const units = ['/ search', '/ report', '/ query', '/ export'];

    items.forEach((item, idx) => {
        const vol = Number(item.volume) || 0;
        const key = keys[idx] || 'search';
        const rate = (activeBillingRates[key] !== undefined) ? activeBillingRates[key] : 0;
        const unit = units[idx] || '';
        
        const lineAmt = Math.round(vol * rate * 100) / 100;
        subtotal += lineAmt;
        
        item.rate = rate === 0 ? 'Included ($0.00)' : `$${rate.toFixed(2)} ${unit}`;
        item.amount = `$${lineAmt.toFixed(2)}`;
        
        const amtEl = document.getElementById(`inv-line-amt-${idx}`);
        if (amtEl) amtEl.innerText = item.amount;
    });

    const taxRate = activeBillingRates.tax_rate || 0;
    const taxAmt = Math.round(subtotal * (taxRate / 100) * 100) / 100;
    const totalAmt = Math.round((subtotal + taxAmt) * 100) / 100;

    currentBillingReportData.subtotal = `$${subtotal.toFixed(2)}`;
    currentBillingReportData.tax_rate = `${taxRate}%`;
    currentBillingReportData.tax_amount = `$${taxAmt.toFixed(2)}`;
    currentBillingReportData.total_amount = `$${totalAmt.toFixed(2)}`;

    safeSetText('inv-subtotal', currentBillingReportData.subtotal);
    safeSetText('inv-tax', currentBillingReportData.tax_amount);
    safeSetText('inv-total', currentBillingReportData.total_amount);
}

function renderBillingInvoiceModal(data) {
    safeSetText('inv-number', data.invoice_no || 'INV-202608-0000');
    safeSetText('inv-date-gen', data.date_generated ? `Date: ${data.date_generated}` : '');
    safeSetText('inv-month-lbl', `Billed Period: ${data.billing_month === 'all' ? 'All Time' : data.billing_month}`);
    
    // Populate Billing Cycle Dropdown if needed
    const selectEl = document.getElementById('billing-cycle-select');
    if (selectEl && selectEl.options.length <= 1) {
        const now = new Date();
        selectEl.innerHTML = '<option value="all">All Time (Aggregated)</option>';
        for (let i = 0; i < 6; i++) {
            const d = new Date(now.getFullYear(), now.getMonth() - i, 1);
            const val = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
            const label = d.toLocaleString('en-US', { month: 'long', year: 'numeric' });
            const opt = document.createElement('option');
            opt.value = val;
            opt.innerText = label;
            selectEl.appendChild(opt);
        }
    }
    if (selectEl) {
        selectEl.value = data.billing_month || 'all';
    }

    const billedTo = data.billed_to || {};
    const orgName = billedTo.organization_name || billedTo.full_name || 'Individual Account';
    safeSetText('inv-billed-org', orgName);
    safeSetText('inv-billed-domain', billedTo.organization_domain ? `Domain: ${billedTo.organization_domain}` : 'Domain: Individual Account');
    safeSetText('inv-billed-tenant', billedTo.tenant_id ? `Tenant ID: ${billedTo.tenant_id}` : `Account ID: ${billedTo.admin_email || 'N/A'}`);
    safeSetText('inv-billed-admin', billedTo.admin_email ? `Contact: ${billedTo.admin_email}` : '');
    
    const issuer = data.issuer || {};
    safeSetText('inv-issuer-company', issuer.company || 'Brainlyn AI Search Platform');
    safeSetText('inv-issuer-dept', issuer.department || 'Enterprise Billing Operations');
    safeSetText('inv-issuer-email', issuer.email || 'billing@brainlyn.com');
    safeSetText('inv-issuer-website', issuer.website || 'https://brainlyn.com');
    
    // Line Items with Interactive Admin Editable Rates
    const lineItemsConfig = [
        { key: 'search', defaultRate: 0.05, unit: '/ search' },
        { key: 'report', defaultRate: 0.25, unit: '/ report' },
        { key: 'api', defaultRate: 0.03, unit: '/ query' },
        { key: 'download', defaultRate: 0.00, unit: '/ export' }
    ];

    const tbody = document.getElementById('inv-line-items-body');
    if (tbody) {
        tbody.innerHTML = '';
        (data.line_items || []).forEach((item, idx) => {
            const config = lineItemsConfig[idx] || { key: `item_${idx}`, unit: '/ unit', defaultRate: 0.00 };
            const curRate = (activeBillingRates[config.key] !== undefined) ? activeBillingRates[config.key] : config.defaultRate;

            const tr = document.createElement('tr');
            tr.style.borderBottom = '1px solid rgba(255,255,255,0.06)';
            tr.style.transition = 'background 0.15s ease';
            tr.onmouseenter = () => tr.style.background = 'rgba(255,255,255,0.03)';
            tr.onmouseleave = () => tr.style.background = 'transparent';
            
            tr.innerHTML = `
                <td style="padding: 12px 16px; font-weight: 500; color: var(--text-main);">
                    ${item.description}
                </td>
                <td style="padding: 12px 16px; text-align: center; font-weight: 700; color: #a5b4fc;">
                    ${Number(item.volume).toLocaleString()}
                </td>
                <td style="padding: 8px 16px; text-align: right;">
                    <div style="display: inline-flex; align-items: center; justify-content: flex-end; gap: 4px;">
                        <span style="color: var(--text-sub); font-size: 0.85rem; font-weight: 600;">$</span>
                        <input type="number" step="0.01" min="0" value="${curRate.toFixed(2)}" 
                               id="inv-rate-input-${config.key}" 
                               class="rate-input" 
                               oninput="onCustomRateChange('${config.key}', this.value)" 
                               title="Platform Admin: Enter custom unit rate in USD">
                        <span style="color: var(--text-sub); font-size: 0.76rem; min-width: 48px; text-align: left;">${config.unit}</span>
                    </div>
                </td>
                <td style="padding: 12px 16px; text-align: right; font-weight: 700; color: #f8fafc;" id="inv-line-amt-${idx}">
                    ${item.amount}
                </td>
            `;
            tbody.appendChild(tr);
        });
    }

    const taxInput = document.getElementById('inv-tax-rate-input');
    if (taxInput) taxInput.value = activeBillingRates.tax_rate;
    
    recalculateBillingFinancials();

    // Itemized audit logs
    rawBillingItemizedLogs = data.itemized_logs || [];
    safeSetText('billing-audit-log-count', rawBillingItemizedLogs.length);
    renderBillingAuditLogsTable(rawBillingItemizedLogs);
}

function toggleBillingAuditLogsView() {
    const container = document.getElementById('billing-audit-logs-container');
    const chevron = document.getElementById('billing-audit-chevron');
    if (!container) return;
    const isHidden = container.style.display === 'none';
    container.style.display = isHidden ? 'block' : 'none';
    if (chevron) chevron.innerText = isHidden ? '▼' : '▶';
}

function filterBillingAuditLogs(q) {
    const query = (q || '').toLowerCase().trim();
    if (!query) {
        renderBillingAuditLogsTable(rawBillingItemizedLogs);
        return;
    }
    const filtered = rawBillingItemizedLogs.filter(log => {
        return (
            (log.user_email && log.user_email.toLowerCase().includes(query)) ||
            (log.search_query && log.search_query.toLowerCase().includes(query)) ||
            (log.risk_level && log.risk_level.toLowerCase().includes(query)) ||
            (log.query_type && log.query_type.toLowerCase().includes(query))
        );
    });
    renderBillingAuditLogsTable(filtered);
}

function renderBillingAuditLogsTable(logs) {
    const tbody = document.getElementById('billing-audit-logs-tbody');
    if (!tbody) return;
    tbody.innerHTML = '';
    
    if (!logs || logs.length === 0) {
        tbody.innerHTML = `
            <tr>
                <td colspan="5" style="text-align: center; color: var(--text-sub); padding: 20px;">
                    No audited transaction records found for this period.
                </td>
            </tr>
        `;
        return;
    }

    logs.slice(0, 100).forEach(log => {
        const tr = document.createElement('tr');
        tr.style.borderBottom = '1px solid rgba(255,255,255,0.04)';
        
        let riskColor = '#34d399';
        let riskLabel = (log.risk_level || 'LOW').toUpperCase();
        if (riskLabel === 'HIGH') riskColor = '#f87171';
        else if (riskLabel === 'MEDIUM') riskColor = '#fbbf24';
        
        tr.innerHTML = `
            <td style="padding: 8px 12px; color: var(--text-sub); white-space: nowrap;">
                ${log.created_at || 'N/A'}
            </td>
            <td style="padding: 8px 12px; font-weight: 500; color: #a5b4fc;">
                ${log.user_email || 'N/A'}
            </td>
            <td style="padding: 8px 12px; color: var(--text-sub);">
                ${log.query_type || 'N/A'}
            </td>
            <td style="padding: 8px 12px; max-width: 280px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-main);" title="${log.search_query || ''}">
                ${log.search_query || 'N/A'}
            </td>
            <td style="padding: 8px 12px; text-align: center;">
                <span style="font-size: 0.72rem; font-weight: 700; color: ${riskColor}; padding: 2px 6px; border-radius: 4px; background: rgba(255,255,255,0.05);">
                    ${riskLabel}
                </span>
            </td>
        `;
        tbody.appendChild(tr);
    });
}

function closeBillingReportModal() {
    const modal = document.getElementById('billing-report-modal');
    if (modal) modal.classList.remove('active');
}

function printInvoiceReport() {
    window.print();
}

function downloadInvoicePDF() {
    window.print();
}

function exportInvoiceCSV() {
    if (!currentBillingReportData) {
        alert("No billing invoice loaded to export.");
        return;
    }
    const data = currentBillingReportData;
    let csvContent = "data:text/csv;charset=utf-8,";
    
    // Header information
    csvContent += "=== BRAINLYN ENTERPRISE SAAS BILLING INVOICE ===\n";
    csvContent += `Invoice Number,${data.invoice_no}\n`;
    csvContent += `Date Generated,${data.date_generated}\n`;
    csvContent += `Billing Period,${data.billing_month === 'all' ? 'All Time' : data.billing_month}\n`;
    csvContent += `Billed Client,"${(data.billed_to && data.billed_to.organization_name) || 'Individual'}"\n`;
    csvContent += `Tenant ID,"${(data.billed_to && data.billed_to.tenant_id) || 'N/A'}"\n`;
    csvContent += `Admin Email,"${(data.billed_to && data.billed_to.admin_email) || 'N/A'}"\n\n`;
    
    // Line items section
    csvContent += "=== ITEMIZED USAGE CHARGES ===\n";
    csvContent += "Service Description,Audited Volume,Rate Card,Total Charge\n";
    (data.line_items || []).forEach(item => {
        const row = [
            `"${item.description}"`,
            item.volume,
            `"${item.rate}"`,
            `"${item.amount}"`
        ].join(",");
        csvContent += row + "\n";
    });
    
    csvContent += `\nSubtotal,${data.subtotal}\n`;
    csvContent += `Tax (${data.tax_rate || '8%'}),${data.tax_amount}\n`;
    csvContent += `Total Amount Due,${data.total_amount}\n\n`;

    // Audited Query Log Transactions
    if (data.itemized_logs && data.itemized_logs.length > 0) {
        csvContent += "=== AUDITED QUERY LOG TRANSACTIONS ===\n";
        csvContent += "Timestamp,User Email,Activity Type,Search Query,Risk Level\n";
        data.itemized_logs.forEach(log => {
            csvContent += [
                `"${log.created_at || ''}"`,
                `"${log.user_email || ''}"`,
                `"${log.query_type || ''}"`,
                `"${(log.search_query || '').replace(/"/g, '""')}"`,
                `"${log.risk_level || ''}"`
            ].join(",") + "\n";
        });
    }
    
    const encodedUri = encodeURI(csvContent);
    const link = document.createElement("a");
    link.setAttribute("href", encodedUri);
    link.setAttribute("download", `Brainlyn_Billing_Invoice_${data.invoice_no}.csv`);
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
}
