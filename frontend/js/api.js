/* ═══════════════════════════════════════════════════════════════
   BRAINLYN AI SEARCH - API CLIENT UTILITIES
   ═══════════════════════════════════════════════════════════════ */

const API_BASE = '';

/**
 * Universal JSON Fetch Wrapper with automatic token handling
 */
async function apiRequest(endpoint, options = {}) {
    const url = endpoint.startsWith('http') ? endpoint : `${API_BASE}${endpoint}`;
    const token = localStorage.getItem('token') || sessionStorage.getItem('token') || sessionStorage.getItem('access_token') || localStorage.getItem('access_token');

    const headers = {
        'Accept': 'application/json',
        ...(options.headers || {})
    };

    if (token) {
        headers['Authorization'] = `Bearer ${token}`;
    }

    if (options.body && !(options.body instanceof FormData) && !headers['Content-Type']) {
        headers['Content-Type'] = 'application/json';
    }

    try {
        const response = await fetch(url, {
            ...options,
            headers
        });

        if (response.status === 401) {
            // Token expired or invalid
            console.warn("Session expired. Redirecting to login...");
            // If on dashboard, alert or redirect
        }

        const data = await response.json().catch(() => ({}));

        if (!response.ok) {
            const errorMsg = data.detail || data.message || `Request failed with status ${response.status}`;
            const error = new Error(errorMsg);
            error.status = response.status;
            error.data = data;
            throw error;
        }

        return data;
    } catch (err) {
        console.error(`API Error on [${options.method || 'GET'}] ${url}:`, err);
        throw err;
    }
}

async function apiGet(endpoint, params = null) {
    let url = endpoint;
    if (params) {
        const searchParams = new URLSearchParams();
        Object.entries(params).forEach(([k, v]) => {
            if (v !== undefined && v !== null) {
                searchParams.append(k, v);
            }
        });
        const qs = searchParams.toString();
        if (qs) url += `?${qs}`;
    }
    return apiRequest(url, { method: 'GET' });
}

async function apiPost(endpoint, body) {
    const isFormData = body instanceof FormData;
    return apiRequest(endpoint, {
        method: 'POST',
        body: isFormData ? body : JSON.stringify(body)
    });
}

async function apiPut(endpoint, body) {
    return apiRequest(endpoint, {
        method: 'PUT',
        body: JSON.stringify(body)
    });
}

async function apiDelete(endpoint) {
    return apiRequest(endpoint, { method: 'DELETE' });
}
