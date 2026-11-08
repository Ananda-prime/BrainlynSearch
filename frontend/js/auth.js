/* ═══════════════════════════════════════════════════════════════
   BRAINLYN AI SEARCH - AUTHENTICATION & PROFILE CONTROLLER
   ═══════════════════════════════════════════════════════════════ */

let currentUserProfile = null;

async function initProfile() {
    let name = "User";
    let role = "individual";

    try {
        const token = localStorage.getItem("access_token") || sessionStorage.getItem("access_token") || localStorage.getItem("token") || sessionStorage.getItem("token");
        if (token) {
            const res = await fetch('/api/me', {
                headers: { 'Authorization': `Bearer ${token}` }
            });
            if (res.ok) {
                const data = await res.json();
                if (data.success && data.user) {
                    currentUserProfile = data.user;
                    const u = data.user;
                    name = u.full_name || u.email.split('@')[0];
                    role = u.role || 'individual';
                    applyProfileToUI(name, role);
                    return currentUserProfile;
                }
            }
        }
    } catch (e) {
        console.error("Profile API fetch error:", e);
    }

    // Fallback: decode JWT payload directly from storage
    const token = localStorage.getItem("access_token") || sessionStorage.getItem("access_token") || localStorage.getItem("token") || sessionStorage.getItem("token");
    if (token) {
        try {
            const payload = JSON.parse(atob(token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/')));
            name = payload.name || payload.sub?.split('@')[0] || "User";
            role = payload.role || "individual";
            currentUserProfile = {
                email: payload.sub,
                full_name: payload.name,
                role: payload.role,
                tenant_id: payload.tenant_id,
                organization_name: payload.organization_name
            };
            applyProfileToUI(name, role);
            return currentUserProfile;
        } catch (e) {
            console.error("Token decode error:", e);
        }
    }

    applyProfileToUI(name, role);
    return null;
}

function applyProfileToUI(name, role) {
    let prettyRole = "Individual";
    if (role === "super_admin") prettyRole = "Super Admin";
    else if (role === "org_admin") prettyRole = "Org Admin";
    else if (role === "org_user") prettyRole = "Org User";

    const nameEl = document.getElementById("profile-name-val") || document.getElementById("profile-name");
    const roleEl = document.getElementById("profile-role-val") || document.getElementById("profile-role");
    const avatarCharEl = document.getElementById("profile-avatar-char");

    if (nameEl) nameEl.innerText = name;
    if (roleEl) {
        roleEl.innerText = prettyRole;
        roleEl.className = "profile-role";
    }
    if (avatarCharEl) {
        const char = name.trim().charAt(0).toUpperCase();
        avatarCharEl.innerText = char || 'A';
    }

    // Sync top navbar elements (user_dashboard.html)
    const topUserName = document.getElementById("topUserName");
    const topUserRole = document.getElementById("topUserRole");
    const topUserAvatar = document.getElementById("topUserAvatar");
    if (topUserName) topUserName.innerText = name;
    if (topUserRole) topUserRole.innerText = prettyRole;
    if (topUserAvatar) {
        const char = name.trim().charAt(0).toUpperCase();
        topUserAvatar.innerText = char || 'A';
    }

    // Unhide user management in sidebar if Super Admin or Org Admin
    if (role === 'super_admin' || role === 'org_admin') {
        const uNav = document.getElementById("menu-users") || document.getElementById("nav-item-users");
        if (uNav) uNav.style.display = "block";
    }
}

function handleLogout() {
    localStorage.removeItem('token');
    localStorage.removeItem('access_token');
    sessionStorage.removeItem('token');
    sessionStorage.removeItem('access_token');
    window.location.replace('/login');
}
