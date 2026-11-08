import os
import sys
import io
import re
import json
import difflib
import psycopg2
import time
import sqlite3
from concurrent.futures import ThreadPoolExecutor

# Ensure current module directory is in sys.path for relative imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
# Load environment variables explicitly from root .env before any service imports
env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".env")
load_dotenv(dotenv_path=env_path, override=True)
load_dotenv(override=True)

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Depends, BackgroundTasks, Request
from fastapi.responses import FileResponse, StreamingResponse, HTMLResponse, Response
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from elasticsearch import Elasticsearch
from fastapi.middleware.cors import CORSMiddleware
from psycopg2.extras import RealDictCursor
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles

try:
    from api.query_builder import build_es_query
    from api.cache import generate_cache_key, get_cached_result, set_cached_result
    from api.services.ecourts_service import sync_fetch_ecourts, LIVE_SYNC_ENABLED, fetch_case_report_by_cnr
    from api.services.similarity_engine import filter_and_score_ecourts_records
    from api.services.ingestion_worker import persist_ecourts_records, save_case_report, get_case_report_from_db
    from api.api_key_auth import get_api_key_client, generate_api_key, list_api_keys, revoke_api_key
except ImportError:
    from query_builder import build_es_query
    from cache import generate_cache_key, get_cached_result, set_cached_result
    from services.ecourts_service import sync_fetch_ecourts, LIVE_SYNC_ENABLED, fetch_case_report_by_cnr
    from services.similarity_engine import filter_and_score_ecourts_records
    from services.ingestion_worker import persist_ecourts_records, save_case_report, get_case_report_from_db
    from api_key_auth import get_api_key_client, generate_api_key, list_api_keys, revoke_api_key

# Shared thread pool for parallel eCourts API fetches (reused across all requests)
_ecourts_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="ecourts-sync")
_prefetch_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="page-prefetch")
_LOCAL_ES_OFFLINE_UNTIL = 0.0

def _prefetch_next_page(params: dict, user: str):
    """Background worker that prefetches the next page into cache so clicking 'Load More' is instant (0ms)."""
    try:
        next_params = dict(params)
        next_params["page"] = int(params.get("page", 1)) + 1
        next_key = generate_cache_key("search", next_params)
        if get_cached_result(next_key):
            return

        next_res = _proxy_to_live_api("/search", next_params, user)
        if next_res and next_res.get("success"):
            set_cached_result(next_key, next_res, ttl_seconds=1800)
            print(f"[Prefetch] Page {next_params['page']} prefetched into memory cache ahead of time!")
    except Exception as e:
        pass

app = FastAPI(title="Brainlynsearch Elasticsearch API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Project Root and Modular Frontend Directory
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FRONTEND_DIR = os.path.join(ROOT_DIR, "frontend")

# Mount Static Asset Directories
if os.path.exists(os.path.join(FRONTEND_DIR, "css")):
    app.mount("/css", StaticFiles(directory=os.path.join(FRONTEND_DIR, "css")), name="css")
if os.path.exists(os.path.join(FRONTEND_DIR, "js")):
    app.mount("/js", StaticFiles(directory=os.path.join(FRONTEND_DIR, "js")), name="js")
if os.path.exists(os.path.join(FRONTEND_DIR, "assets")):
    app.mount("/assets", StaticFiles(directory=os.path.join(FRONTEND_DIR, "assets")), name="assets")

# Mount entire frontend under /static and /frontend
if os.path.exists(FRONTEND_DIR):
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR, html=True), name="static")
    app.mount("/frontend", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")


# Authentication Schemas
class UserSignup(BaseModel):
    name: str
    email: str
    password: str
    signup_type: str = "individual"  # "individual", "org_create", "org_join"
    org_name: Optional[str] = None

class VerifyOtpRequest(BaseModel):
    email: str
    otp: str

class ResendOtpRequest(BaseModel):
    email: str

class UpdateRoleRequest(BaseModel):
    role: str
    organization_name: Optional[str] = None
    organization_domain: Optional[str] = None
    tenant_id: Optional[str] = None
    subscription_status: Optional[str] = None

class UpdateStatusRequest(BaseModel):
    is_approved: bool

class UpdateSubscriptionRequest(BaseModel):
    subscription_status: str  # "purchased", "unpurchased", "subscribed", "unsubscribed"

class CreateUserRequest(BaseModel):
    email: str
    password: Optional[str] = None
    full_name: Optional[str] = None
    role: str = "individual"
    organization_name: Optional[str] = None
    organization_domain: Optional[str] = None
    tenant_id: Optional[str] = None
    subscription_status: Optional[str] = "unpurchased"

class CreateOrgRequest(BaseModel):
    organization_name: str
    organization_domain: Optional[str] = None
    admin_email: Optional[str] = None
    admin_name: Optional[str] = None
    admin_password: Optional[str] = None


# SQLite database path for local user storage
SQLITE_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.db")

def get_sqlite_conn():
    conn = sqlite3.connect(SQLITE_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

_CRIMINAL_KEYWORDS_REGEX = re.compile(
    r'\b(CRIMINAL|CC|CRA|CRMP|CRMC|BAIL|SPL\.?\s?C|SESSIONS|FIR|POLICE|IPC|CRPC|POCSO|NDPS|SC\/ST|CALENDAR CASE)\b',
    re.IGNORECASE
)
_PARTY_CRIMINAL_REGEX = re.compile(
    r'\b(STATE OF|STATE VS|POLICE STATION)\b',
    re.IGNORECASE
)

def _is_criminal_record_py(rec: dict) -> bool:
    """Classifies a legal record as Criminal (True) or Civil (False)."""
    if not rec or not isinstance(rec, dict):
        return False
    ct = str(rec.get("case_type") or rec.get("caseType") or "").strip()
    if "CRIMINAL" in ct.upper() or _CRIMINAL_KEYWORDS_REGEX.search(ct):
        return True
    p = str(rec.get("party_name") or "").strip()
    if _PARTY_CRIMINAL_REGEX.search(p):
        return True
    acts = str(rec.get("acts_sections") or "")
    if re.search(r'\b(IPC|CRPC|INDIAN PENAL CODE|CODE OF CRIMINAL PROCEDURE)\b', acts, re.IGNORECASE):
        return True
    return False

def _matches_state_filter(requested_state: Optional[str], rec: dict) -> bool:
    """
    Robust state matching supporting:
      - Variations with/without spaces (e.g. 'tamilnadu' vs 'Tamil Nadu')
      - State code abbreviations (e.g. 'TN' vs 'Tamil Nadu', 'DL' vs 'Delhi')
      - Fallback inspection of court_name / establishment
      - If requested_state is provided, records from non-matching states are excluded
    """
    if not requested_state or not requested_state.strip():
        return True
    if not rec or not isinstance(rec, dict):
        return False
    
    req_clean = re.sub(r'[^a-zA-Z0-9]', '', requested_state.lower().strip())
    if not req_clean:
        return True

    code_to_name = {
        "ap": "andhrapradesh", "ar": "arunachalpradesh", "as": "assam",
        "br": "bihar", "ct": "chhattisgarh", "cg": "chhattisgarh",
        "ga": "goa", "gj": "gujarat", "hr": "haryana",
        "hp": "himachalpradesh", "jh": "jharkhand", "ka": "karnataka",
        "kl": "kerala", "mp": "madhyapradesh", "mh": "maharashtra",
        "mn": "manipur", "ml": "meghalaya", "mz": "mizoram",
        "nl": "nagaland", "od": "odisha", "or": "odisha",
        "pb": "punjab", "rj": "rajasthan", "sk": "sikkim",
        "tn": "tamilnadu", "ts": "telangana", "tr": "tripura",
        "up": "uttarpradesh", "uk": "uttarakhand", "ut": "uttarakhand",
        "wb": "westbengal", "dl": "delhi", "ch": "chandigarh",
        "jk": "jammukashmir", "la": "ladakh", "py": "puducherry"
    }
    name_to_code = {v: k for k, v in code_to_name.items()}

    candidates = {req_clean}
    if req_clean in code_to_name:
        candidates.add(code_to_name[req_clean])
    if req_clean in name_to_code:
        candidates.add(name_to_code[req_clean])

    rec_state_name = re.sub(r'[^a-zA-Z0-9]', '', str(rec.get("state_name") or rec.get("state") or "").lower())
    rec_state_code = re.sub(r'[^a-zA-Z0-9]', '', str(rec.get("state_code") or "").lower())
    rec_court_name = re.sub(r'[^a-zA-Z0-9]', '', str(rec.get("court_name") or rec.get("establishment") or "").lower())

    for c in candidates:
        if c and (c in rec_state_name or (len(rec_state_name) > 0 and rec_state_name in c)):
            return True
        if c and c == rec_state_code:
            return True

    for c in candidates:
        if len(c) >= 4 and c in rec_court_name:
            return True

    return False

def _matches_date_filter(req_date_str: str, rec: dict) -> bool:
    """
    Strict date and registration year matching supporting:
      - Single Year (e.g. '2026', '2023')
      - Year Range (e.g. '2018-2022', '2020 - 2024')
      - Exact Date (e.g. '2026-06-24', '24-06-2026')
    Checks registration_date, date_of_registration, filing_date, decision_date,
    and year indicators in case_number, filing_number, and CNR.
    """
    if not req_date_str or not req_date_str.strip():
        return True
    if not rec or not isinstance(rec, dict):
        return False

    req_str = req_date_str.strip()

    try:
        from api.query_builder import parse_date_range
    except ImportError:
        from query_builder import parse_date_range

    parsed = parse_date_range(req_str)
    start_date, end_date = parsed if parsed else (None, None)

    # 1. Inspect date candidate fields
    date_candidates = [
        str(rec.get("registration_date") or "").strip(),
        str(rec.get("date_of_registration") or "").strip(),
        str(rec.get("filing_date") or "").strip(),
        str(rec.get("decision_date") or "").strip()
    ]

    is_single_year = bool(re.match(r"^\d{4}$", req_str))
    req_year = req_str if is_single_year else None

    for d in date_candidates:
        if not d or d.lower() in ("none", "n/a", "null", ""):
            continue

        # Direct 4-digit year match inside date string
        if req_year:
            years = re.findall(r"\b(19\d\d|20\d\d)\b", d)
            if req_year in years:
                return True

        # Normalize DD-MM-YYYY or DD/MM/YYYY to YYYY-MM-DD
        d_norm = d[:10]
        if re.match(r"^\d{2}-\d{2}-\d{4}$", d_norm):
            p = d_norm.split("-")
            d_norm = f"{p[2]}-{p[1]}-{p[0]}"
        elif re.match(r"^\d{2}/\d{2}/\d{4}$", d_norm):
            p = d_norm.split("/")
            d_norm = f"{p[2]}-{p[1]}-{p[0]}"

        if start_date and end_date and re.match(r"^\d{4}-\d{2}-\d{2}$", d_norm):
            if start_date <= d_norm <= end_date:
                return True

    # 2. If single year was queried (e.g. '2026'), check case_number and CNR as fallback
    if req_year:
        case_no = str(rec.get("case_number") or rec.get("filing_number") or "").strip()
        if f"/{req_year}" in case_no or f"-{req_year}" in case_no or case_no.endswith(req_year):
            return True
        cnr = str(rec.get("cnr") or rec.get("diary_number") or "").strip().upper()
        if len(cnr) >= 16 and cnr[-4:] == req_year:
            return True

    return False

INDIAN_STATES_CANONICAL = {
    "andhra pradesh": "Andhra Pradesh",
    "andhra": "Andhra Pradesh",
    "ap": "Andhra Pradesh",
    "arunachal pradesh": "Arunachal Pradesh",
    "assam": "Assam",
    "bihar": "Bihar",
    "chhattisgarh": "Chhattisgarh",
    "goa": "Goa",
    "gujarat": "Gujarat",
    "haryana": "Haryana",
    "himachal pradesh": "Himachal Pradesh",
    "jharkhand": "Jharkhand",
    "karnataka": "Karnataka",
    "kerala": "Kerala",
    "madhya pradesh": "Madhya Pradesh",
    "maharashtra": "Maharashtra",
    "manipur": "Manipur",
    "meghalaya": "Meghalaya",
    "mizoram": "Mizoram",
    "nagaland": "Nagaland",
    "odisha": "Odisha",
    "orissa": "Odisha",
    "punjab": "Punjab",
    "rajasthan": "Rajasthan",
    "sikkim": "Sikkim",
    "tamil nadu": "Tamil Nadu",
    "tamilnadu": "Tamil Nadu",
    "tn": "Tamil Nadu",
    "telangana": "Telangana",
    "tripura": "Tripura",
    "uttar pradesh": "Uttar Pradesh",
    "up": "Uttar Pradesh",
    "uttarakhand": "Uttarakhand",
    "uttaranchal": "Uttarakhand",
    "west bengal": "West Bengal",
    "bengal": "West Bengal",
    "wb": "West Bengal",
    "delhi": "Delhi",
    "nct of delhi": "Delhi",
    "jammu and kashmir": "Jammu & Kashmir",
    "jammu & kashmir": "Jammu & Kashmir",
    "ladakh": "Ladakh",
    "chandigarh": "Chandigarh",
    "puducherry": "Puducherry",
    "pondicherry": "Puducherry"
}

def normalize_indian_state(state: Optional[str]) -> Optional[str]:
    if not state:
        return None
    raw = str(state).strip()
    cleaned = raw.lower().replace("_", " ").replace("-", " ")
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        return None
    if cleaned in INDIAN_STATES_CANONICAL:
        return INDIAN_STATES_CANONICAL[cleaned]
    compact = cleaned.replace(" ", "")
    if compact in INDIAN_STATES_CANONICAL:
        return INDIAN_STATES_CANONICAL[compact]
    return cleaned.title()

def log_query(
    user_email: str, 
    query_type: str, 
    state_name: Optional[str] = None, 
    risk_level: str = "low",
    search_query: Optional[str] = None,
    details: Optional[str] = None,
    case_status: Optional[str] = None
):
    conn = get_sqlite_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT tenant_id FROM users WHERE email = ?;", (user_email,))
        row = cur.fetchone()
        tenant_id = row["tenant_id"] if row else None
        
        status_val = case_status or ("pending" if risk_level == "high" else "clear")
        norm_state = normalize_indian_state(state_name) if state_name else None
        cur.execute(
            """
            INSERT INTO query_logs (user_email, tenant_id, query_type, state_name, risk_level, search_query, details, case_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (user_email, tenant_id, query_type, norm_state, risk_level, search_query, details, status_val)
        )
        conn.commit()
    except Exception as e:
        print(f"Error writing query log: {e}")
    finally:
        conn.close()

def init_sqlite_db():
    conn = sqlite3.connect(SQLITE_DB_PATH)
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                full_name TEXT,
                is_approved BOOLEAN DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        
        # Schema migration: check and add missing columns
        cur.execute("PRAGMA table_info(users);")
        columns = [row[1] for row in cur.fetchall()]
        
        if 'role' not in columns:
            cur.execute("ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'individual';")
        if 'tenant_id' not in columns:
            cur.execute("ALTER TABLE users ADD COLUMN tenant_id TEXT;")
        if 'organization_name' not in columns:
            cur.execute("ALTER TABLE users ADD COLUMN organization_name TEXT;")
        if 'organization_domain' not in columns:
            cur.execute("ALTER TABLE users ADD COLUMN organization_domain TEXT;")
        if 'subscription_status' not in columns:
            cur.execute("ALTER TABLE users ADD COLUMN subscription_status TEXT DEFAULT 'unpurchased';")
            cur.execute("UPDATE users SET subscription_status = 'purchased' WHERE role IN ('super_admin', 'org_admin', 'org_user');")
            
        # Create query_logs table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS query_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_email TEXT NOT NULL,
                tenant_id TEXT,
                query_type TEXT NOT NULL,
                state_name TEXT,
                risk_level TEXT DEFAULT 'low',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Add indexes for ultra-fast dashboard queries & metrics
        cur.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_users_tenant ON users(tenant_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_query_logs_email ON query_logs(user_email);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_query_logs_tenant ON query_logs(tenant_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_query_logs_type ON query_logs(query_type);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_query_logs_created ON query_logs(created_at);")

        # Schema migration: check and add missing query_logs columns
        cur.execute("PRAGMA table_info(query_logs);")
        ql_columns = [row[1] for row in cur.fetchall()]
        if 'search_query' not in ql_columns:
            cur.execute("ALTER TABLE query_logs ADD COLUMN search_query TEXT;")
        if 'details' not in ql_columns:
            cur.execute("ALTER TABLE query_logs ADD COLUMN details TEXT;")
        if 'case_status' not in ql_columns:
            cur.execute("ALTER TABLE query_logs ADD COLUMN case_status TEXT DEFAULT 'pending';")

        conn.commit()
    except Exception as e:
        print(f"Error initializing SQLite DB: {e}")
    finally:
        conn.close()

init_sqlite_db()

# In-memory OTP fallback store for when Redis is unavailable
# Structure: { "email": { "otp": "123456", "expires": timestamp } }
_otp_memory_store = {}
_redis_available = True  # Circuit-breaker: set False after first Redis failure

def _get_redis():
    """Return the redis_client only if Redis is currently considered available."""
    global _redis_available
    if not _redis_available:
        return None
    try:
        try:
            from api.cache import redis_client
        except ImportError:
            from cache import redis_client
        return redis_client
    except Exception:
        return None

def _store_otp(email: str, otp: str, ttl: int = 120):
    """Store OTP in Redis if available, else fall back to in-memory dict."""
    global _redis_available
    rc = _get_redis()
    if rc:
        try:
            rc.set(f"otp:{email}", otp, ex=ttl)
            return
        except Exception as redis_err:
            print(f"Redis OTP storage error: {redis_err}")
            _redis_available = False  # Circuit-breaker trips: skip Redis from now on
    # Fallback: in-memory store (instant)
    _otp_memory_store[email] = {"otp": otp, "expires": time.time() + ttl}
    print(f"[OTP MEMORY] Stored OTP for {email} (expires in {ttl}s)")

def _get_otp(email: str):
    """Retrieve OTP from Redis if available, else from in-memory dict."""
    global _redis_available
    rc = _get_redis()
    if rc:
        try:
            val = rc.get(f"otp:{email}")
            if val is not None:
                return val
        except Exception:
            _redis_available = False
    # Fallback: in-memory store
    entry = _otp_memory_store.get(email)
    if entry:
        if time.time() < entry["expires"]:
            return entry["otp"]
        else:
            del _otp_memory_store[email]
    return None

def _delete_otp(email: str):
    """Delete OTP from Redis and/or in-memory store."""
    global _redis_available
    rc = _get_redis()
    if rc:
        try:
            rc.delete(f"otp:{email}")
        except Exception:
            _redis_available = False
    _otp_memory_store.pop(email, None)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)

def get_current_user(request: Request, token: Optional[str] = Depends(oauth2_scheme)):
    actual_token = token
    if not actual_token:
        actual_token = request.query_params.get("token") or request.cookies.get("access_token")
        if not actual_token:
            auth_hdr = request.headers.get("Authorization") or request.headers.get("authorization")
            if auth_hdr and auth_hdr.lower().startswith("bearer "):
                actual_token = auth_hdr.split(" ", 1)[1].strip()

    if not actual_token:
        raise HTTPException(
            status_code=401,
            detail="Authentication token is missing",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        from api.auth import decode_access_token
    except ImportError:
        from auth import decode_access_token
    payload = decode_access_token(actual_token)
    if payload is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    email = payload.get("sub")
    if email is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid token payload",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return email

def get_current_super_admin(email: str = Depends(get_current_user)):
    conn = get_sqlite_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT role FROM users WHERE LOWER(email) = LOWER(?);", (email,))
        row = cur.fetchone()
        role = (row["role"] if row else None) or "individual"
        if role != "super_admin" and email.lower() not in ["anandavelan50@gmail.com", "admin@brainlyn.com"]:
            raise HTTPException(status_code=403, detail="Super Admin access required")
        return email
    finally:
        conn.close()

def get_current_admin_user(email: str = Depends(get_current_user)):
    conn = get_sqlite_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT role FROM users WHERE LOWER(email) = LOWER(?);", (email,))
        row = cur.fetchone()
        role = (row["role"] if row else None) or "individual"
        if role not in ["super_admin", "org_admin"] and email.lower() not in ["anandavelan50@gmail.com", "admin@brainlyn.com"]:
            raise HTTPException(status_code=403, detail="Admin authorization required (Super Admin or Org Admin)")
        return email
    finally:
        conn.close()

# Calculate the path to frontend files
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT_DIR = os.path.dirname(BASE_DIR)
FRONTEND_DIR = os.path.join(ROOT_DIR, "frontend")

INDEX_PATH = os.path.join(FRONTEND_DIR, "index.html")
DASHBOARD_PATH = os.path.join(FRONTEND_DIR, "dashboard.html")
USER_DASHBOARD_PATH = os.path.join(FRONTEND_DIR, "user_dashboard.html")
LOGIN_PATH = os.path.join(FRONTEND_DIR, "login.html")

INTERCEPTOR_SCRIPT = """
<script>
// Intercept fetch requests to add Authorization header
const originalFetch = window.fetch;
window.fetch = async function(...args) {
    let [resource, config] = args;
    if (!config) config = {};
    if (!config.headers) config.headers = {};
    
    const token = localStorage.getItem("access_token") || sessionStorage.getItem("access_token") || localStorage.getItem("token") || sessionStorage.getItem("token");
    if (token) {
        if (config.headers instanceof Headers) {
            config.headers.set("Authorization", `Bearer ${token}`);
        } else if (Array.isArray(config.headers)) {
            config.headers.push(["Authorization", `Bearer ${token}`]);
        } else {
            config.headers["Authorization"] = `Bearer ${token}`;
        }
    }
    
    const response = await originalFetch(resource, config);
    if (response.status === 401) {
        localStorage.removeItem("access_token");
        sessionStorage.removeItem("access_token");
        localStorage.removeItem("token");
        sessionStorage.removeItem("token");
        window.location.replace("/login");
    }
    return response;
};
</script>
"""

@app.get("/")
def read_root():
    # Read index.html dynamically to inject authentication & interceptor scripts
    if not os.path.exists(INDEX_PATH):
        raise HTTPException(status_code=404, detail="index.html not found")
        
    with open(INDEX_PATH, "r", encoding="utf-8") as f:
        html_content = f.read()
        
    check_auth_script = """
    <script>
    // Route protection: check for access token, validate JWT, and redirect if missing/expired
    function checkAuth() {
        const token = localStorage.getItem("access_token") || sessionStorage.getItem("access_token") || localStorage.getItem("token") || sessionStorage.getItem("token");
        if (!token) {
            window.location.replace("/login");
            return;
        }
        try {
            const parts = token.split('.');
            if (parts.length !== 3) {
                throw new Error("Invalid token format");
            }
            const payload = JSON.parse(atob(parts[1].replace(/-/g, '+').replace(/_/g, '/')));
            const currentTime = Math.floor(Date.now() / 1000);
            if (payload.exp && currentTime >= payload.exp) {
                throw new Error("Token expired");
            }
            document.cookie = "access_token=" + encodeURIComponent(token) + "; path=/; max-age=86400; SameSite=Lax";
        } catch (e) {
            localStorage.removeItem("access_token");
            sessionStorage.removeItem("access_token");
            localStorage.removeItem("token");
            sessionStorage.removeItem("token");
            window.location.replace("/login");
        }
    }
    checkAuth();
    
    // Listen for pageshow event (handles back button / bfcache restores)
    window.addEventListener("pageshow", function(event) {
        checkAuth();
    });
    </script>
    """
    
    # Inject checkAuth at the top of <head> to run synchronously before any DOM parsing/painting
    html_content = html_content.replace("<head>", f"<head>{check_auth_script}")
    # Inject fetch interceptor and UI buttons at the end of body
    html_content = html_content.replace("</body>", f"{INTERCEPTOR_SCRIPT}</body>")
    
    headers = {
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0"
    }
    return HTMLResponse(content=html_content, status_code=200, headers=headers)

@app.get("/index.html")
def read_index_page():
    return read_root()

@app.get("/dashboard.html")
def read_dashboard():
    # Read dashboard.html dynamically to inject authentication & interceptor scripts
    if not os.path.exists(DASHBOARD_PATH):
        raise HTTPException(status_code=404, detail="dashboard.html not found")
        
    with open(DASHBOARD_PATH, "r", encoding="utf-8") as f:
        html_content = f.read()
        
    check_auth_script = """
    <script>
    // Route protection: check for access token, validate JWT, and redirect if missing/expired
    function checkAuth() {
        const token = localStorage.getItem("access_token") || sessionStorage.getItem("access_token") || localStorage.getItem("token") || sessionStorage.getItem("token");
        if (!token) {
            window.location.replace("/login");
            return;
        }
        try {
            const parts = token.split('.');
            if (parts.length !== 3) {
                throw new Error("Invalid token format");
            }
            const payload = JSON.parse(atob(parts[1].replace(/-/g, '+').replace(/_/g, '/')));
            const currentTime = Math.floor(Date.now() / 1000);
            if (payload.exp && currentTime >= payload.exp) {
                throw new Error("Token expired");
            }
            document.cookie = "access_token=" + encodeURIComponent(token) + "; path=/; max-age=86400; SameSite=Lax";
        } catch (e) {
            localStorage.removeItem("access_token");
            sessionStorage.removeItem("access_token");
            localStorage.removeItem("token");
            sessionStorage.removeItem("token");
            window.location.replace("/login");
        }
    }
    checkAuth();
    
    // Listen for pageshow event (handles back button / bfcache restores)
    window.addEventListener("pageshow", function(event) {
        checkAuth();
    });
    </script>
    """
    # Inject checkAuth at the top of <head> to run synchronously before any DOM parsing/painting
    html_content = html_content.replace("<head>", f"<head>{check_auth_script}")
    # Inject fetch interceptor and UI buttons at the end of body
    html_content = html_content.replace("</body>", f"{INTERCEPTOR_SCRIPT}</body>")
    
    headers = {
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0"
    }
    return HTMLResponse(content=html_content, status_code=200, headers=headers)

@app.get("/user_dashboard.html")
def read_user_dashboard():
    # Read user_dashboard.html dynamically to inject authentication & interceptor scripts
    if not os.path.exists(USER_DASHBOARD_PATH):
        raise HTTPException(status_code=404, detail="user_dashboard.html not found")
        
    with open(USER_DASHBOARD_PATH, "r", encoding="utf-8") as f:
        html_content = f.read()
        
    check_auth_script = """
    <script>
    // Route protection: check for access token, validate JWT, and redirect if missing/expired
    function checkAuth() {
        const token = localStorage.getItem("access_token") || sessionStorage.getItem("access_token") || localStorage.getItem("token") || sessionStorage.getItem("token");
        if (!token) {
            window.location.replace("/login");
            return;
        }
        try {
            const parts = token.split('.');
            if (parts.length !== 3) {
                throw new Error("Invalid token format");
            }
            const payload = JSON.parse(atob(parts[1].replace(/-/g, '+').replace(/_/g, '/')));
            const currentTime = Math.floor(Date.now() / 1000);
            if (payload.exp && currentTime >= payload.exp) {
                throw new Error("Token expired");
            }
            document.cookie = "access_token=" + encodeURIComponent(token) + "; path=/; max-age=86400; SameSite=Lax";
        } catch (e) {
            localStorage.removeItem("access_token");
            sessionStorage.removeItem("access_token");
            localStorage.removeItem("token");
            sessionStorage.removeItem("token");
            window.location.replace("/login");
        }
    }
    checkAuth();
    
    // Listen for pageshow event (handles back button / bfcache restores)
    window.addEventListener("pageshow", function(event) {
        checkAuth();
    });
    </script>
    """
    
    # Inject checkAuth at the top of <head> to run synchronously before any DOM parsing/painting
    html_content = html_content.replace("<head>", f"<head>{check_auth_script}")
    # Inject fetch interceptor and UI buttons at the end of body
    html_content = html_content.replace("</body>", f"{INTERCEPTOR_SCRIPT}</body>")
    
    headers = {
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0"
    }
    return HTMLResponse(content=html_content, status_code=200, headers=headers)

@app.get("/login")
def read_login_page():
    if not os.path.exists(LOGIN_PATH):
        raise HTTPException(status_code=404, detail="login.html not found")
    return FileResponse(LOGIN_PATH)

@app.get("/login.html")
def read_login_html_page():
    if not os.path.exists(LOGIN_PATH):
        raise HTTPException(status_code=404, detail="login.html not found")
    return FileResponse(LOGIN_PATH)

@app.get("/{filename}.pdf")
@app.get("/orders/{filename}.pdf")
@app.get("/api/orders/{filename}.pdf")
def serve_pdf(
    filename: str,
    request: Request,
    cnr: Optional[str] = None,
    order_date: Optional[str] = None,
    order_type: Optional[str] = None,
    token: Optional[str] = None
):
    clean_fn = filename.strip()
    if clean_fn.lower().endswith(".pdf"):
        clean_fn = clean_fn[:-4]

    # 1. Check if a physical PDF file exists on disk in known directories
    candidate_paths = [
        os.path.join(ROOT_DIR, f"{clean_fn}.pdf"),
        os.path.join(ROOT_DIR, "orders", f"{clean_fn}.pdf"),
        os.path.join(FRONTEND_DIR, f"{clean_fn}.pdf"),
        os.path.join(ROOT_DIR, "downloads", f"{clean_fn}.pdf")
    ]
    for p in candidate_paths:
        if os.path.exists(p) and os.path.isfile(p):
            return FileResponse(p, media_type="application/pdf")

    # 2. Dynamic generation of Certified Judicial Order PDF
    try:
        try:
            from api.services.order_pdf_service import generate_order_pdf
        except ImportError:
            from services.order_pdf_service import generate_order_pdf

        pdf_bytes = generate_order_pdf(
            filename=f"{clean_fn}.pdf",
            cnr=cnr,
            order_date_hint=order_date,
            order_type_hint=order_type
        )
        if pdf_bytes:
            headers = {
                "Content-Disposition": f'inline; filename="{clean_fn}.pdf"',
                "Cache-Control": "public, max-age=3600"
            }
            return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)
    except Exception as e:
        print(f"[serve_pdf] Error generating PDF for {filename}: {e}")

    raise HTTPException(status_code=404, detail="Order document not found")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    from fastapi.responses import Response
    return Response(content=b"", media_type="image/x-icon")

# Add CORS Middleware to allow connections from your browser
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Allows your local file to connect
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configuration
ES_URL = os.getenv("ES_URL", "http://localhost:9201")
PG_URL = os.getenv("DATABASE_URL")
INDEX_PATTERN = "court_records,high_court_records,supreme_court_records,ecourts_live_records"

# Map ES indices back to Postgres tables for hydration
TABLE_MAPPING = {
    "court_records": "lexisearch.court_records",
    "high_court_records": "lexisearch.high_court_records",
    "supreme_court_records": "lexisearch.supreme_court_records",
    "ecourts_live_records": "lexisearch.ecourts_live_records"
}

es = Elasticsearch(ES_URL, request_timeout=0.2, max_retries=0)

def get_db_connection():
    conn_str = PG_URL
    if not conn_str:
        return None
    if conn_str.startswith("postgres://"):
        conn_str = conn_str.replace("postgres://", "postgresql://", 1)
    try:
        from urllib.parse import urlparse
        url = urlparse(conn_str)
        
        # Explicit connection arguments force libpq to ignore shell/env overrides
        conn_kwargs = {
            "user": url.username,
            "password": url.password,
            "host": url.hostname,
            "port": url.port or 5432,
            "database": url.path.lstrip('/'),
            "sslmode": "require"
        }
        return psycopg2.connect(**conn_kwargs)
    except Exception as e:
        print(f"DB Connection Error: {e}")
        return None

_LIVE_PROXY_OFFLINE_UNTIL = 0.0

CANONICAL_STATES_MAP = {
    "andhrapradesh": "Andhra Pradesh", "ap": "Andhra Pradesh",
    "arunachalpradesh": "Arunachal Pradesh", "ar": "Arunachal Pradesh",
    "assam": "Assam", "as": "Assam",
    "bihar": "Bihar", "br": "Bihar",
    "chhattisgarh": "Chhattisgarh", "ct": "Chhattisgarh", "cg": "Chhattisgarh",
    "goa": "Goa", "ga": "Goa",
    "gujarat": "Gujarat", "gj": "Gujarat",
    "haryana": "Haryana", "hr": "Haryana",
    "himachalpradesh": "Himachal Pradesh", "hp": "Himachal Pradesh",
    "jharkhand": "Jharkhand", "jh": "Jharkhand",
    "karnataka": "Karnataka", "ka": "Karnataka",
    "kerala": "Kerala", "kl": "Kerala",
    "madhyapradesh": "Madhya Pradesh", "mp": "Madhya Pradesh",
    "maharashtra": "Maharashtra", "mh": "Maharashtra",
    "manipur": "Manipur", "mn": "Manipur",
    "meghalaya": "Meghalaya", "ml": "Meghalaya",
    "mizoram": "Mizoram", "mz": "Mizoram",
    "nagaland": "Nagaland", "nl": "Nagaland",
    "odisha": "Odisha", "or": "Odisha",
    "punjab": "Punjab", "pb": "Punjab",
    "rajasthan": "Rajasthan", "rj": "Rajasthan",
    "sikkim": "Sikkim", "sk": "Sikkim",
    "tamilnadu": "Tamil Nadu", "tn": "Tamil Nadu",
    "telangana": "Telangana", "ts": "Telangana",
    "tripura": "Tripura", "tr": "Tripura",
    "uttarpradesh": "Uttar Pradesh", "up": "Uttar Pradesh",
    "uttarakhand": "Uttarakhand", "uk": "Uttarakhand", "ut": "Uttarakhand",
    "westbengal": "West Bengal", "wb": "West Bengal",
    "delhi": "Delhi", "dl": "Delhi",
    "chandigarh": "Chandigarh", "ch": "Chandigarh",
    "jammukashmir": "Jammu and Kashmir", "jk": "Jammu and Kashmir",
    "ladakh": "Ladakh", "la": "Ladakh",
    "puducherry": "Puducherry", "py": "Puducherry"
}

def _proxy_to_live_api(endpoint: str, payload: dict, user_email: Optional[str] = None) -> Optional[dict]:
    """Fallback helper to proxy requests to live server ONLY during local development when local ES is unreachable."""
    global _LIVE_PROXY_OFFLINE_UNTIL
    is_prod = os.getenv("NODE_ENV", "development").lower() in ["production", "prod"]
    if is_prod:
        return None  # Never proxy when running on the production VM

    # Circuit breaker: If live proxy recently timed out, skip immediately to prevent request blocking
    now = time.time()
    if now < _LIVE_PROXY_OFFLINE_UNTIL:
        return None

    try:
        try:
            from api.auth import create_access_token
        except ImportError:
            from auth import create_access_token
        import urllib.request
        import json
        
        # Prepare payload with canonical state name normalization for live Elasticsearch query
        payload_copy = dict(payload)
        if payload_copy.get("state"):
            clean_st = re.sub(r'[^a-zA-Z0-9]', '', str(payload_copy["state"]).lower())
            if clean_st in CANONICAL_STATES_MAP:
                payload_copy["state"] = CANONICAL_STATES_MAP[clean_st]

        token = create_access_token({"sub": user_email or "dev_user"})
        url = f"https://brainlyn.com{endpoint}"
        req_obj = urllib.request.Request(
            url,
            data=json.dumps(payload_copy).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}"
            }
        )
        print(f"[DEV FALLBACK] Local ES unreachable. Forwarding {endpoint} to live server ({url})...")
        with urllib.request.urlopen(req_obj, timeout=4.0) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode("utf-8"))
    except Exception as proxy_err:
        print(f"[LIVE PROXY FALLBACK FAILED] {proxy_err}. Circuit breaker active for 60s.")
        _LIVE_PROXY_OFFLINE_UNTIL = time.time() + 60.0  # Skip proxying for 60 seconds
    return None

class SearchRequest(BaseModel):
    name: str
    state: Optional[str] = None
    district: Optional[str] = None
    registration_date: Optional[str] = None
    cnr: Optional[str] = None
    court_type: Optional[str] = "all"
    page: int = 1
    size: Optional[int] = 20
    petitioner: Optional[bool] = False
    respondent: Optional[bool] = False
    threshold: Optional[float] = None

class SuggestRequest(BaseModel):
    name: str
    field: Optional[str] = "party_name"

@app.post("/search")
def search_records(
    req: SearchRequest, 
    background_tasks: BackgroundTasks = BackgroundTasks(),
    current_user: str = Depends(get_current_user),
    log_query_type: Optional[str] = "interactive_search"
):
    global _LOCAL_ES_OFFLINE_UNTIL
    start_time = time.time()
    
    if (not req.name or len(req.name.strip()) < 2) and (not req.cnr or len(req.cnr.strip()) < 5):
        raise HTTPException(status_code=400, detail="Must provide at least a name (2+ chars) or CNR (5+ chars)")
        
    if req.registration_date:
        try:
            from api.query_builder import parse_date_range
        except ImportError:
            from query_builder import parse_date_range
            
        if not parse_date_range(req.registration_date):
            raise HTTPException(
                status_code=400,
                detail="Please provide a valid year (e.g. 2021), year range (e.g. 2018-2022), or exact date (YYYY-MM-DD)."
            )
        
    try:
        cache_params = req.model_dump()
        cache_key = generate_cache_key("search", cache_params)
        
        cached_data = get_cached_result(cache_key)
        if cached_data:
            cached_data["cached"] = True
            cached_data["search_time_ms"] = round((time.time() - start_time) * 1000, 2)
            if log_query_type:
                q_term = req.name or req.cnr
                cached_recs = cached_data.get("data", []) if isinstance(cached_data, dict) else []
                crim_recs = [r for r in cached_recs if _is_criminal_record_py(r)]
                has_crim = len(crim_recs) > 0
                cached_risk = "high" if has_crim else "low"
                any_pend = any(
                    any(term in str(r.get("case_status") or r.get("status") or "").lower() for term in ("pending", "active", "hearing", "stage", "trial", "pre-trial"))
                    for r in crim_recs
                )
                cached_status = ("pending" if any_pend else "disposed") if has_crim else "clear"
                log_query(current_user, log_query_type, req.state, cached_risk, search_query=q_term, details="Cached Lookup", case_status=cached_status)
            return cached_data

        es_query = build_es_query(cache_params)
        page_size = getattr(req, 'size', 20) or 20
        from_index = (req.page - 1) * page_size
        
        # Master query key independent of page offset for instant pagination (<1ms)
        master_params = {k: v for k, v in cache_params.items() if k not in ("page", "size")}
        master_cache_key = generate_cache_key("master_search", master_params)

        # If user is loading subsequent pages (page 2, 3...) serve from instant in-memory cache
        if req.page > 1:
            cached_master = get_cached_result(master_cache_key)
            if cached_master and isinstance(cached_master, dict):
                master_records = cached_master.get("records", [])
                master_total_hits = cached_master.get("total_hits", len(master_records))
                if from_index < len(master_records):
                    page_records = master_records[from_index : from_index + page_size]
                    has_more_records = (from_index + page_size) < len(master_records) or master_total_hits > (from_index + page_size)
                    print(f"[Instant Pagination] Serving Page {req.page} ({len(page_records)} records) from memory cache (<1ms)")
                    return {
                        "success": True,
                        "data": page_records,
                        "total": len(master_records),
                        "total_indexed": max(master_total_hits, len(master_records)),
                        "page": req.page,
                        "hasMore": has_more_records,
                        "search_time_ms": round((time.time() - start_time) * 1000, 2),
                        "cached": True
                    }

        # Determine if we have a filter active (exclusive Petitioner or Respondent or Threshold or Date filter)
        is_petitioner_active = bool(req.petitioner) and not bool(req.respondent)
        is_respondent_active = bool(req.respondent) and not bool(req.petitioner)
        has_threshold_filter = req.threshold is not None and req.threshold > 0
        has_date_filter = bool(req.registration_date and req.registration_date.strip())
        is_filtered_search = is_petitioner_active or is_respondent_active or has_threshold_filter or has_date_filter

        es_size = 100 if is_filtered_search else 20
        es_from = 0 if is_filtered_search else from_index

        # 0. Select Target Index
        target_index = INDEX_PATTERN
        if req.court_type == "district":
            target_index = "court_records,ecourts_live_records"
        elif req.court_type == "high":
            target_index = "high_court_records,ecourts_live_records"
        elif req.court_type == "supreme":
            target_index = "supreme_court_records"

        # 0.5 Kick off eCourts API fetch in parallel only on initial search (page 1)
        _ecourts_future = None
        search_query_term = req.name.strip() if req.name and len(req.name.strip()) >= 2 else (req.cnr.strip() if req.cnr and len(req.cnr.strip()) >= 5 else None)
        if req.page == 1 and search_query_term and LIVE_SYNC_ENABLED:
            _ecourts_future = _ecourts_executor.submit(
                sync_fetch_ecourts, search_query_term, is_petitioner_active, is_respondent_active
            )

        # 1. Multi-Index Search
        all_hits = []
        total_hits = 0
        final_records = []
        
        now_ts = time.time()
        es_is_available = now_ts >= _LOCAL_ES_OFFLINE_UNTIL
        if es_is_available:
            try:
                es_response = es.search(
                    index=target_index,
                    ignore_unavailable=True,
                    query=es_query,
                    from_=es_from,
                    size=es_size,
                    source=["cnr", "diary_number", "case_type"],
                    track_total_hits=True
                )
                all_hits = es_response["hits"]["hits"]
                total_hits = es_response["hits"]["total"]["value"]
            except Exception as es_search_err:
                _LOCAL_ES_OFFLINE_UNTIL = time.time() + 60.0
                print(f"[Local ES Note] Local ES offline (circuit breaker 60s): {es_search_err}")
                es_is_available = False

        if not es_is_available or not all_hits:
            # Fetch historical search records from live VM fallback
            vm_payload = req.model_dump()
            vm_response = _proxy_to_live_api("/search", vm_payload, current_user)
            if vm_response and vm_response.get("success"):
                vm_data = vm_response.get("data", [])
                total_hits = vm_response.get("total", len(vm_data))
                for r in vm_data:
                    final_records.append(r)
                print(f"[VM Fallback] Fetched {len(vm_data)} records from live VM (Total in index: {total_hits})")
        
        if all_hits:
            max_score = (es_response.get("hits", {}).get("max_score") if 'es_response' in locals() else None) or 1.0
            
            # Group IDs by their source index to batch Postgres queries
            grouped_hits = {}
            for hit in all_hits:
                idx = hit["_index"]
                if idx not in grouped_hits: grouped_hits[idx] = []
                grouped_hits[idx].append(hit)

            conn = get_db_connection()
            if conn:
                try:
                    with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                        for idx, hits in grouped_hits.items():
                            table = TABLE_MAPPING.get(idx)
                            if not table: continue
                            
                            # Handle different ID columns (Supreme Court uses case_number/diary_number)
                            id_col = "cnr" if "supreme" not in table else "diary_number"
                            
                            def get_hit_id(hit):
                                if id_col == "diary_number":
                                    return hit["_source"].get("diary_number")
                                return hit["_source"].get("cnr") or hit["_source"].get("diary_number")

                            ids = [get_hit_id(h) for h in hits if get_hit_id(h)]
                            if not ids: continue

                            format_strings = ','.join(['%s'] * len(ids))
                            
                            cursor.execute(f"SELECT * FROM {table} WHERE {id_col} IN ({format_strings})", tuple(ids))
                            rows = cursor.fetchall()
                            
                            for row in rows:
                                # Normalize row to match our UI expectations
                                rec = dict(row)
                                if "state_code" in rec and "state_name" not in rec:
                                    rec["state_name"] = rec["state_code"]
                                # Normalize case_type to uppercase for consistent display
                                if rec.get("case_type"):
                                    rec["case_type"] = rec["case_type"].upper()
                                rec["source_index"] = idx
                                row_id = str(rec.get(id_col))
                                matching_hits = [h for h in hits if str(get_hit_id(h)) == row_id]
                                if matching_hits:
                                    hit = matching_hits[0]
                                    # Fallback: if DB row has no case_type, use ES indexed value
                                    if not rec.get("case_type") and hit["_source"].get("case_type"):
                                        rec["case_type"] = hit["_source"]["case_type"].upper()

                                    # Filter hydrated records by court_type if specified
                                    if req.court_type and req.court_type != "all" and rec.get("court_type") and rec.get("court_type") != req.court_type:
                                        continue
                                    # Filter hydrated records by state if specified
                                    if req.state and not _matches_state_filter(req.state, rec):
                                        continue

                                    # If pure CNR search without name, assign 100% confidence directly
                                    if (not req.name or not req.name.strip()) and req.cnr:
                                        rec["confidence"] = 100.0
                                        final_records.append(rec)
                                        continue

                                    # Absolute True String Distance Scoring with Band Rules
                                    import re
                                    import difflib
                                    
                                    query_name_str = str(req.name).lower().strip()
                                    party_name_str = str(rec.get("party_name", "")).lower().strip()
                                    
                                    def normalize_vs(s):
                                        patterns = [
                                            r'\bversus\b',
                                            r'\bv\.\s*s\.(?!\w)',
                                            r'\bv\.\s*s\b',
                                            r'\bv\s*/\s*s\b',
                                            r'(?<=[a-zA-Z])v/s(?=[a-zA-Z])',
                                            r'(?<=[a-zA-Z])v/s\b',
                                            r'\bv/s(?=[a-zA-Z])',
                                            r'\bvs\.(?!\w)',
                                            r'\bvs\b',
                                            r'(?<=[a-zA-Z])vs(?=[a-zA-Z])',
                                            r'(?<=[a-zA-Z])vs\b',
                                            r'\bvs(?=[a-zA-Z])',
                                            r'\bv\.(?!\w)',
                                            r'\bv\b'
                                        ]
                                        combined_pattern = '|'.join(patterns)
                                        return re.sub(combined_pattern, ' vs ', s, flags=re.IGNORECASE).strip()

                                    query_normalized = normalize_vs(query_name_str)
                                    party_normalized = normalize_vs(party_name_str)

                                    def get_band_score(q, t):
                                        if not q or not t:
                                            return 0.0
                                        q = q.strip()
                                        t = t.strip()
                                        if q == t:
                                            return 100.0
                                        if q in t or t in q:
                                            # Substring match (90-94% band based on noise)
                                            len_ratio = min(len(q), len(t)) / max(len(q), len(t), 1)
                                            return 90.0 + (4.9 * len_ratio)
                                        # Typos or fuzzy match (Cap at 89.9%)
                                        base = difflib.SequenceMatcher(None, q, t).ratio() * 100
                                        return 89.9 if base >= 90.0 else base

                                    # 1. Compare based on active filters
                                    if is_petitioner_active:
                                        p_parts = [p.strip() for p in party_normalized.split(' vs ') if p.strip()]
                                        petitioner_part = p_parts[0] if p_parts else ""
                                        q_parts = [q.strip() for q in query_normalized.split(' vs ') if q.strip()]
                                        q_name = q_parts[0] if q_parts else query_normalized
                                        
                                        score = get_band_score(q_name, petitioner_part)
                                        if score >= 40.0:
                                            rec["confidence"] = round(score, 1)
                                            final_records.append(rec)
                                    elif is_respondent_active:
                                        p_parts = [p.strip() for p in party_normalized.split(' vs ') if p.strip()]
                                        if len(p_parts) >= 2:
                                            respondent_part = p_parts[1]
                                            q_parts = [q.strip() for q in query_normalized.split(' vs ') if q.strip()]
                                            q_name = q_parts[0] if q_parts else query_normalized
                                            
                                            score = get_band_score(q_name, respondent_part)
                                            if score >= 40.0:
                                                rec["confidence"] = round(score, 1)
                                                final_records.append(rec)
                                    else:
                                        # Compare full normalized strings first
                                        best_score = get_band_score(query_normalized, party_normalized)
                                        
                                        # Check split-by-vs comparison if applicable
                                        q_parts = [p.strip() for p in query_normalized.split(' vs ') if p.strip()]
                                        p_parts = [p.strip() for p in party_normalized.split(' vs ') if p.strip()]
                                        
                                        if len(q_parts) >= 2 and len(p_parts) >= 2:
                                            score_aligned_1 = get_band_score(q_parts[0], p_parts[0])
                                            score_aligned_2 = get_band_score(q_parts[1], p_parts[1])
                                            avg_aligned = (score_aligned_1 + score_aligned_2) / 2.0
                                            if avg_aligned > best_score:
                                                best_score = avg_aligned
                                                
                                            score_swapped_1 = get_band_score(q_parts[0], p_parts[1])
                                            score_swapped_2 = get_band_score(q_parts[1], p_parts[0])
                                            avg_swapped = (score_swapped_1 + score_swapped_2) / 2.0
                                            if avg_swapped > best_score:
                                                best_score = avg_swapped
                                        elif len(p_parts) >= 2:
                                            for part in p_parts:
                                                sub_parties = [sp.strip() for sp in part.split(',') if sp.strip()]
                                                if len(sub_parties) > 1:
                                                    for sp in sub_parties:
                                                        score = get_band_score(query_normalized, sp)
                                                        if score > best_score:
                                                            best_score = score
                                                else:
                                                    score = get_band_score(query_normalized, part)
                                                    if score > best_score:
                                                        best_score = score
                                        elif len(q_parts) >= 2:
                                            for part in q_parts:
                                                score = get_band_score(part, party_normalized)
                                                if score > best_score:
                                                    best_score = score

                                        rec["confidence"] = round(best_score, 1)
                                        final_records.append(rec)
                    conn.close()
                except Exception as db_err:
                    print(f"Hydration Error: {db_err}")
                    raise HTTPException(
                        status_code=500,
                        detail=f"Database hydration error: {db_err}"
                    )

        # 2. Collect parallel eCourts results (API call was fired before ES search)
        live_records_to_persist = []
        if _ecourts_future:
            try:
                raw_live_hits = _ecourts_future.result(timeout=3.0)
                if raw_live_hits:
                    min_sim = float(os.getenv("ECOURTS_SIMILARITY_MIN_THRESHOLD", "75.0"))
                    scored_live_hits = filter_and_score_ecourts_records(
                        raw_results=raw_live_hits,
                        query_name=req.name,
                        is_petitioner=is_petitioner_active,
                        is_respondent=is_respondent_active,
                        min_similarity=min_sim
                    )
                    print(f"[eCourts Live Engine] {len(scored_live_hits)} of {len(raw_live_hits)} live records passed similarity threshold (>= {min_sim}%)")
                    for s_rec in scored_live_hits[:3]:
                        print(f"  -> Matched: '{s_rec.get('party_name', '')[:45]}' | Score: {s_rec.get('confidence')}% | Court: {s_rec.get('court_name', '')[:40]}")
                    
                    # Deduplicate against current ES hits and DB records via unique CNR
                    existing_cnrs = {str(h.get("_source", {}).get("cnr") or h.get("_source", {}).get("diary_number") or "").upper() for h in all_hits if h.get("_source")}
                    existing_cnrs.update({str(r.get("cnr") or r.get("diary_number") or "").upper() for r in final_records if r.get("cnr") or r.get("diary_number")})
                    
                    for live_rec in scored_live_hits:
                        live_cnr = str(live_rec.get("cnr") or "").upper()
                        if not live_cnr:
                            continue
                        # Apply state filter if user specified one
                        if req.state and not _matches_state_filter(req.state, live_rec):
                            continue
                        # Apply court_type filter if user specified one
                        if req.court_type and req.court_type != "all" and live_rec.get("court_type") != req.court_type:
                            continue
                        # Apply registration date / year filter if user specified one
                        if req.registration_date and not _matches_date_filter(req.registration_date, live_rec):
                            continue

                        if live_cnr in existing_cnrs:
                            # Upgrade existing record to Live Verified
                            for r in final_records:
                                if str(r.get("cnr") or r.get("diary_number") or "").upper() == live_cnr:
                                    r["source_index"] = "ecourts_live_records"
                                    r["source_table"] = "ecourts_live_records"
                                    r["is_live"] = True
                                    r["confidence"] = max(r.get("confidence", 0), live_rec.get("confidence", 0))
                                    if live_rec.get("next_hearing_date"):
                                        r["next_hearing_date"] = live_rec.get("next_hearing_date")
                                    if live_rec.get("case_status"):
                                        r["case_status"] = live_rec.get("case_status")
                                    if live_rec.get("court_name") and not r.get("court_name"):
                                        r["court_name"] = live_rec.get("court_name")
                                    if live_rec.get("establishment_name") and not r.get("establishment_name"):
                                        r["establishment_name"] = live_rec.get("establishment_name")
                                    if live_rec.get("state_name") and not r.get("state_name"):
                                        r["state_name"] = live_rec.get("state_name")
                                    if live_rec.get("district_name") and not r.get("district_name"):
                                        r["district_name"] = live_rec.get("district_name")
                                    break
                            live_records_to_persist.append(live_rec)
                        else:
                            final_records.append(live_rec)
                            live_records_to_persist.append(live_rec)
                            existing_cnrs.add(live_cnr)
            except Exception as ecourts_err:
                print(f"[eCourts Live Sync Warning] {ecourts_err}")

        # Schedule background persistence without blocking the search response
        if live_records_to_persist:
            if background_tasks:
                background_tasks.add_task(persist_ecourts_records, live_records_to_persist)
            else:
                import threading
                threading.Thread(target=persist_ecourts_records, args=(live_records_to_persist,), daemon=True).start()

        # If neither ES nor eCourts found records
        if not final_records and not all_hits:
            return {"success": True, "data": [], "total": 0, "search_time_ms": round((time.time() - start_time) * 1000, 2)}

        # Filter by threshold if requested
        if req.threshold is not None and req.threshold > 0:
            min_thresh_pct = req.threshold * 100.0 if req.threshold <= 1.0 else float(req.threshold)
            final_records = [r for r in final_records if r.get("confidence", 0) >= min_thresh_pct]

        # Filter by registration_date / year if requested
        if req.registration_date and req.registration_date.strip():
            final_records = [r for r in final_records if _matches_date_filter(req.registration_date.strip(), r)]

        # Filter by state if requested
        if req.state and req.state.strip():
            final_records = [r for r in final_records if _matches_state_filter(req.state.strip(), r)]

        # Sort final list: Prioritize Live Verified records first, then highest confidence
        final_records.sort(key=lambda x: (
            1 if (x.get("source_index") == "ecourts_live_records" or x.get("source_table") == "ecourts_live_records" or x.get("is_live")) else 0,
            x.get("confidence", 0)
        ), reverse=True)

        total_filtered = len(final_records)
        if is_filtered_search and all_hits:
            page_records = final_records[from_index : from_index + page_size]
        elif is_filtered_search and not all_hits and len(final_records) > page_size:
            # If combining VM hits with eCourts hits on page 1
            page_records = final_records[from_index : from_index + page_size] if from_index < len(final_records) else final_records[:page_size]
        else:
            page_records = final_records

        import datetime as dt

        def _sanitize_val(v):
            if isinstance(v, (dt.datetime, dt.date)):
                return v.isoformat()
            if v is None or isinstance(v, (str, int, float, bool)):
                return v
            if isinstance(v, (list, tuple, set)):
                return [_sanitize_val(item) for item in v]
            if isinstance(v, dict):
                return {str(dk): _sanitize_val(dv) for dk, dv in v.items()}
            return str(v)

        def sanitize_record(rec):
            """Convert non-JSON-serializable values to safe types recursively preserving dicts and lists."""
            if not isinstance(rec, dict):
                return rec
            return {k: _sanitize_val(v) for k, v in rec.items()}

        page_records = [sanitize_record(r) for r in page_records]

        total_indexed_hits = max(total_hits, total_filtered, len(final_records))
        effective_total = total_filtered if is_filtered_search else max(total_hits, len(final_records))

        # Determine if more records exist on the database / server
        has_more_records = False
        if 'vm_response' in locals() and vm_response and vm_response.get("hasMore") is not None:
            has_more_records = bool(vm_response.get("hasMore"))
        elif total_hits > (from_index + page_size):
            has_more_records = True
        elif effective_total > (from_index + page_size):
            has_more_records = True

        response_data = {
            "success": True,
            "data": page_records,
            "total": effective_total,
            "total_indexed": total_indexed_hits,
            "page": req.page,
            "hasMore": has_more_records,
            "search_time_ms": round((time.time() - start_time) * 1000, 2),
            "cached": False
        }
        
        # Log search query with criminal litigation risk and case status
        if log_query_type:
            criminal_recs = [r for r in page_records if _is_criminal_record_py(r)] if page_records else []
            has_criminal = len(criminal_recs) > 0
            risk_level = "high" if has_criminal else "low"
            
            any_pending = any(
                any(term in str(r.get("case_status") or r.get("status") or "").lower() for term in ("pending", "active", "hearing", "stage", "trial", "pre-trial"))
                for r in criminal_recs
            )
            c_status = ("pending" if any_pending else "disposed") if has_criminal else "clear"
                
            q_term = req.name or req.cnr
            d_str = f"Court: {req.court_type or 'all'}, Match Threshold: {req.threshold or 90}%"
            if has_criminal:
                d_str += f", Criminal ({c_status.capitalize()})"
            log_query(current_user, log_query_type, req.state, risk_level, search_query=q_term, details=d_str, case_status=c_status)
        
        set_cached_result(cache_key, response_data)
        if final_records:
            set_cached_result(master_cache_key, {"records": final_records, "total_hits": total_indexed_hits}, ttl_seconds=1800)

        # Predictive prefetch: Asynchronously fetch next page ahead of time so 'Load More' is instant
        if has_more_records and req.page < 10:
            _prefetch_executor.submit(_prefetch_next_page, req.model_dump(), current_user)

        return response_data
        
    except HTTPException as he:
        raise he
    except Exception as e:
        print(f"Search Error: {e}")
        fallback_res = _proxy_to_live_api("/search", req.model_dump(), current_user)
        if fallback_res is not None:
            if "total_indexed" not in fallback_res and "total" in fallback_res:
                fallback_res["total_indexed"] = fallback_res["total"]
            return fallback_res
        raise HTTPException(status_code=500, detail=str(e))

def _normalize_case_report_acts(val):
    if not val:
        return []
    if isinstance(val, str):
        val = val.strip()
        if (val.startswith("[") and val.endswith("]")) or (val.startswith("{") and val.endswith("}")):
            try:
                parsed = json.loads(val)
                return _normalize_case_report_acts(parsed)
            except Exception:
                pass
        if "|" in val:
            parts = [p.strip() for p in val.split("|", 1)]
            return [{"act": parts[0] or "Act Reference", "section": parts[1] if len(parts) > 1 else ""}]
        return [{"act": val, "section": ""}]
    if isinstance(val, list):
        res = []
        for x in val:
            if isinstance(x, dict):
                res.append({
                    "act": x.get("act") or x.get("actName") or x.get("description") or x.get("title") or "Act Reference",
                    "section": x.get("section") or x.get("sections") or x.get("sectionNumber") or ""
                })
            elif isinstance(x, str):
                res.extend(_normalize_case_report_acts(x))
        return res
    if isinstance(val, dict):
        return [{
            "act": val.get("act") or val.get("actName") or val.get("description") or val.get("title") or "Act Reference",
            "section": val.get("section") or val.get("sections") or val.get("sectionNumber") or ""
        }]
    return []

def _normalize_case_report_list(val, list_key=None):
    if not val:
        return []
    if isinstance(val, str):
        val = val.strip()
        if (val.startswith("[") and val.endswith("]")) or (val.startswith("{") and val.endswith("}")):
            try:
                parsed = json.loads(val)
                return _normalize_case_report_list(parsed, list_key=list_key)
            except Exception:
                pass
        return [{"title": val}]
    if isinstance(val, dict):
        if list_key and isinstance(val.get(list_key), list):
            return val[list_key]
        for candidate in ["files", "orders", "judgmentOrders", "interimOrders", "historyOfCaseHearings", "history", "data"]:
            if isinstance(val.get(candidate), list):
                return val[candidate]
        return [val]
    if isinstance(val, list):
        return val
    return []

def _normalize_case_report_strings(val):
    if not val:
        return []
    if isinstance(val, str):
        val = val.strip()
        if val.startswith("{") and val.endswith("}"):
            inner = val[1:-1].strip()
            return [x.strip().strip('"') for x in inner.split(",") if x.strip()]
        if val.startswith("[") and val.endswith("]"):
            try:
                parsed = json.loads(val)
                if isinstance(parsed, list):
                    return [str(x).strip() for x in parsed if str(x).strip()]
            except Exception:
                pass
        return [val] if val else []
    if isinstance(val, list):
        clean = []
        for x in val:
            if isinstance(x, str):
                s = x.strip().strip('"')
                if s.startswith("{") and s.endswith("}"):
                    clean.extend([p.strip().strip('"') for p in s[1:-1].split(",") if p.strip()])
                elif s:
                    clean.append(s)
            elif x is not None:
                clean.append(str(x))
        return clean
    return []

def _normalize_case_report_dict(val):
    if not val:
        return {}
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        try:
            parsed = json.loads(val)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return {}

@app.get("/api/case/report/{cnr}")
def get_case_report_endpoint(
    cnr: str,
    refresh: bool = False,
    current_user: str = Depends(get_current_user)
):
    """
    Returns the comprehensive case docket/report for a given CNR.
    1. Checks local DB cache (lexisearch.case_reports) unless refresh=True.
    2. Queries live eCourts Partner API if not cached or refresh requested.
    3. Upserts fresh data to lexisearch.case_reports and syncs status in lexisearch.ecourts_live_records.
    """
    clean_cnr = (cnr or "").strip().upper()
    if not clean_cnr or len(clean_cnr) < 5:
        raise HTTPException(status_code=400, detail="Invalid CNR format")

    # Check if caller is platform admin (super_admin / platform_admin)
    is_platform_admin = False
    try:
        conn = get_sqlite_conn()
        cur = conn.cursor()
        cur.execute("SELECT role FROM users WHERE LOWER(email) = LOWER(?);", (current_user,))
        u_row = cur.fetchone()
        u_role = (u_row["role"] if u_row else None) or "individual"
        if u_role in ["super_admin", "platform_admin"] or current_user.lower() in [
            "anandavelan50@gmail.com", "admin@brainlyn.com", "balasubramaniam.mohan@gmail.com"
        ]:
            is_platform_admin = True
        conn.close()
    except Exception:
        pass

    # 1. Check local DB cache if refresh is not forced
    if not refresh:
        cached_report = get_case_report_from_db(clean_cnr)
        if cached_report:
            raw_data = cached_report.get("raw_report")
            if isinstance(raw_data, str):
                try:
                    raw_data = json.loads(raw_data)
                except Exception:
                    raw_data = {}
            return {
                "success": True,
                "cached": True,
                "cnr": clean_cnr,
                "summary": {
                    "case_number": cached_report.get("case_number"),
                    "party_name": cached_report.get("party_name"),
                    "petitioner": cached_report.get("petitioner"),
                    "respondent": cached_report.get("respondent"),
                    "case_type": cached_report.get("case_type"),
                    "case_status": cached_report.get("case_status"),
                    "court_name": cached_report.get("court_name"),
                    "state_name": cached_report.get("state_name"),
                    "district_name": cached_report.get("district_name"),
                    "registration_date": str(cached_report.get("registration_date") or ""),
                    "filing_date": str(cached_report.get("filing_date") or ""),
                    "next_hearing_date": str(cached_report.get("next_hearing_date") or ""),
                    "decision_date": str(cached_report.get("decision_date") or ""),
                    "first_hearing_date": str(cached_report.get("first_hearing_date") or ""),
                    "judges": _normalize_case_report_strings(cached_report.get("judges")),
                    "petitioner_advocates": _normalize_case_report_strings(cached_report.get("petitioner_advocates")),
                    "respondent_advocates": _normalize_case_report_strings(cached_report.get("respondent_advocates"))
                },
                "hearing_history": _normalize_case_report_list(cached_report.get("hearing_history")),
                "orders": _normalize_case_report_list(cached_report.get("orders")),
                "files": _normalize_case_report_list(cached_report.get("files"), list_key="files"),
                "acts_sections": _normalize_case_report_acts(cached_report.get("acts_sections")),
                "ai_analysis": _normalize_case_report_dict(cached_report.get("ai_analysis")),
                "raw_report": raw_data if is_platform_admin else None,
                "updated_at": str(cached_report.get("updated_at") or "")
            }

    # 2. Fetch live docket from eCourts Partner Case API
    api_res = fetch_case_report_by_cnr(clean_cnr)
    if not api_res or (isinstance(api_res, dict) and api_res.get("error")):
        err_msg = (api_res or {}).get("message", "Case report not found on live eCourts portal")
        status_code = (api_res or {}).get("status_code", 404)
        
        # Check if DB has historical cached version to fall back to
        cached_fallback = get_case_report_from_db(clean_cnr)
        if cached_fallback:
            raw_data = cached_fallback.get("raw_report")
            if isinstance(raw_data, str):
                try:
                    raw_data = json.loads(raw_data)
                except Exception:
                    raw_data = {}
            return {
                "success": True,
                "cached": True,
                "cnr": clean_cnr,
                "warning": f"Live refresh failed ({err_msg}). Displaying cached report.",
                "summary": {
                    "case_number": cached_fallback.get("case_number"),
                    "party_name": cached_fallback.get("party_name"),
                    "petitioner": cached_fallback.get("petitioner"),
                    "respondent": cached_fallback.get("respondent"),
                    "case_type": cached_fallback.get("case_type"),
                    "case_status": cached_fallback.get("case_status"),
                    "court_name": cached_fallback.get("court_name"),
                    "state_name": cached_fallback.get("state_name"),
                    "district_name": cached_fallback.get("district_name"),
                    "registration_date": str(cached_fallback.get("registration_date") or ""),
                    "filing_date": str(cached_fallback.get("filing_date") or ""),
                    "next_hearing_date": str(cached_fallback.get("next_hearing_date") or ""),
                    "decision_date": str(cached_fallback.get("decision_date") or ""),
                    "first_hearing_date": str(cached_fallback.get("first_hearing_date") or ""),
                    "judges": _normalize_case_report_strings(cached_fallback.get("judges")),
                    "petitioner_advocates": _normalize_case_report_strings(cached_fallback.get("petitioner_advocates")),
                    "respondent_advocates": _normalize_case_report_strings(cached_fallback.get("respondent_advocates"))
                },
                "hearing_history": _normalize_case_report_list(cached_fallback.get("hearing_history")),
                "orders": _normalize_case_report_list(cached_fallback.get("orders")),
                "files": _normalize_case_report_list(cached_fallback.get("files"), list_key="files"),
                "acts_sections": _normalize_case_report_acts(cached_fallback.get("acts_sections")),
                "ai_analysis": _normalize_case_report_dict(cached_fallback.get("ai_analysis")),
                "raw_report": raw_data if is_platform_admin else None,
                "updated_at": str(cached_fallback.get("updated_at") or "")
            }
        raise HTTPException(status_code=status_code, detail=err_msg)

    # 3. Persist latest report and update database
    save_case_report(clean_cnr, api_res)

    # 4. Construct response
    data = api_res.get("data") or {}
    case_data = data.get("courtCaseData") or data.get("tribunalCaseData") or {}

    petitioners = case_data.get("petitioners") or []
    respondents = case_data.get("respondents") or []
    p_str = ", ".join(petitioners) if isinstance(petitioners, list) else str(petitioners)
    r_str = ", ".join(respondents) if isinstance(respondents, list) else str(respondents)
    party_title = f"{p_str} vs {r_str}" if (p_str and r_str) else (p_str or r_str or case_data.get("party_name") or "Unknown")

    return {
        "success": True,
        "cached": False,
        "cnr": clean_cnr,
        "summary": {
            "case_number": case_data.get("caseNumber"),
            "party_name": party_title,
            "petitioner": p_str or None,
            "respondent": r_str or None,
            "case_type": case_data.get("caseType"),
            "case_status": case_data.get("caseStatus"),
            "court_name": case_data.get("courtName"),
            "court_code": case_data.get("courtCode"),
            "state_name": case_data.get("state"),
            "district_name": case_data.get("district"),
            "registration_date": case_data.get("registrationDate"),
            "filing_date": case_data.get("filingDate"),
            "next_hearing_date": case_data.get("nextHearingDate"),
            "decision_date": case_data.get("decisionDate"),
            "first_hearing_date": case_data.get("firstHearingDate"),
            "case_duration_days": case_data.get("caseDurationDays"),
            "judges": _normalize_case_report_strings(case_data.get("judges")),
            "petitioner_advocates": _normalize_case_report_strings(case_data.get("petitionerAdvocates")),
            "respondent_advocates": _normalize_case_report_strings(case_data.get("respondentAdvocates"))
        },
        "hearing_history": _normalize_case_report_list(case_data.get("historyOfCaseHearings")),
        "orders": _normalize_case_report_list(case_data.get("judgmentOrders") or case_data.get("interimOrders")),
        "files": _normalize_case_report_list(data.get("files"), list_key="files"),
        "acts_sections": _normalize_case_report_acts(case_data.get("actsAndSections")),
        "ai_analysis": _normalize_case_report_dict(data.get("caseAiAnalysis")),
        "raw_report": api_res if is_platform_admin else None,
        "updated_at": str(time.strftime("%Y-%m-%dT%H:%M:%SZ"))
    }


# ──────────────────────────────────────────────────────────────────────────────
# Partner REST API v1 & Developer Key Endpoints
# ──────────────────────────────────────────────────────────────────────────────

class PartnerSearchRequest(BaseModel):
    name: Optional[str] = None
    cnr: Optional[str] = None
    state: Optional[str] = None
    district: Optional[str] = None
    court_type: Optional[str] = "all"
    registration_date: Optional[str] = None
    threshold: Optional[float] = 0.75
    petitioner: Optional[bool] = False
    respondent: Optional[bool] = False
    page: int = 1
    page_size: int = 20

class CreateApiKeyRequest(BaseModel):
    key_name: str
    organization_name: Optional[str] = None
    rate_limit_per_min: Optional[int] = 60

@app.post(
    "/api/v1/partner/search",
    tags=["Partner REST API v1"],
    summary="Search 7.9M+ Court & Live Verified Records",
    description="Unified legal search across all High Courts, District Courts, and Supreme Court with live verification. Authenticate with 'X-API-Key: bls_live_...' or 'Authorization: Bearer bls_live_...'."
)
def partner_search(
    req: PartnerSearchRequest,
    background_tasks: BackgroundTasks,
    client: dict = Depends(get_api_key_client)
):
    if (not req.name or len(req.name.strip()) < 2) and (not req.cnr or len(req.cnr.strip()) < 5):
        raise HTTPException(
            status_code=400,
            detail="Must provide at least a 'name' (2+ characters) or a 'cnr' (5+ characters)."
        )

    search_req = SearchRequest(
        name=req.name or "",
        cnr=req.cnr,
        state=req.state,
        district=req.district,
        registration_date=req.registration_date,
        court_type=req.court_type,
        page=req.page,
        size=req.page_size,
        petitioner=req.petitioner,
        respondent=req.respondent,
        threshold=req.threshold
    )

    res = search_records(
        req=search_req,
        background_tasks=background_tasks,
        current_user=client.get("user_email", "partner_api"),
        log_query_type="partner_api_search"
    )

    if isinstance(res, dict):
        res["partner"] = {
            "organization": client.get("organization_name"),
            "key_name": client.get("key_name"),
            "monthly_quota": client.get("monthly_quota"),
            "used_count": client.get("used_count")
        }
    return res

@app.get(
    "/api/v1/partner/case/{cnr}",
    tags=["Partner REST API v1"],
    summary="Get Case Docket & Hearing History by CNR",
    description="Retrieve comprehensive 39-field case dossier, hearing history, advocates, judges, and orders for a CNR. Authenticate with 'X-API-Key: bls_live_...'."
)
def partner_case_report(
    cnr: str,
    refresh: bool = False,
    client: dict = Depends(get_api_key_client)
):
    clean_cnr = (cnr or "").strip().upper()
    if not clean_cnr or len(clean_cnr) < 5:
        raise HTTPException(status_code=400, detail="Invalid CNR format")

    res = get_case_report_endpoint(
        cnr=clean_cnr,
        refresh=refresh,
        current_user=client.get("user_email", "partner_api")
    )
    if isinstance(res, dict):
        res["partner"] = {
            "organization": client.get("organization_name"),
            "key_name": client.get("key_name")
        }
    return res

# ── Developer Key Management Endpoints (Dashboard / UI) ──
@app.post(
    "/api/v1/developer/keys",
    tags=["Developer Portal"],
    summary="Generate a New Partner API Key",
    description="Creates a new 'bls_live_...' secret API key. The secret key is only displayed once upon creation."
)
def create_developer_key(
    req: CreateApiKeyRequest,
    current_user: str = Depends(get_current_user)
):
    if not req.key_name or len(req.key_name.strip()) < 2:
        raise HTTPException(status_code=400, detail="key_name is required (2+ chars)")

    org_name = req.organization_name
    if not org_name:
        try:
            conn = get_sqlite_conn()
            cur = conn.cursor()
            cur.execute("SELECT organization_name FROM users WHERE LOWER(email) = LOWER(?);", (current_user,))
            row = cur.fetchone()
            if row and row["organization_name"]:
                org_name = row["organization_name"]
            conn.close()
        except Exception:
            pass

    key_res = generate_api_key(
        user_email=current_user,
        key_name=req.key_name.strip(),
        organization_name=org_name or "Default Organization",
        rate_limit_per_min=req.rate_limit_per_min or 60
    )
    return {"success": True, "data": key_res}

@app.get(
    "/api/v1/developer/keys",
    tags=["Developer Portal"],
    summary="List Partner API Keys",
    description="Lists active and revoked keys for the authenticated user/organization (never exposes raw secret hashes)."
)
def get_developer_keys(
    current_user: str = Depends(get_current_user)
):
    keys = list_api_keys(current_user)
    return {"success": True, "data": keys}

@app.delete(
    "/api/v1/developer/keys/{key_id}",
    tags=["Developer Portal"],
    summary="Revoke an API Key",
    description="Deactivates a partner API key so it can no longer be used."
)
def delete_developer_key(
    key_id: int,
    current_user: str = Depends(get_current_user)
):
    revoked = revoke_api_key(current_user, key_id)
    if not revoked:
        raise HTTPException(status_code=404, detail="API Key not found or does not belong to your account")
    return {"success": True, "message": "API Key revoked successfully"}


@app.post("/suggest")
def suggest_names(req: SuggestRequest, current_user: str = Depends(get_current_user)):
    if not req.name or len(req.name.strip()) < 2:
        return {"success": True, "data": []}
        
    try:
        target_field = req.field if req.field in ["party_name", "state_name", "district_name"] else "party_name"
        
        # We suggest from the main court_records index primarily
        suggest_index = "court_records"
        
        if target_field == "party_name":
            response = es.search(
                index=suggest_index,
                ignore_unavailable=True,
                query={
                    "match": {
                        "party_name.ngram": {
                            "query": req.name,
                            "operator": "and"
                        }
                    }
                },
                size=15,
                source=["party_name"]
            )
            suggestions = list(set([hit["_source"]["party_name"] for hit in response["hits"]["hits"]]))
        else:
            response = es.search(
                index=suggest_index,
                ignore_unavailable=True,
                query={
                    "match": {
                        target_field: {
                            "query": req.name,
                            "operator": "and"
                        }
                    }
                },
                size=0,
                aggs={
                    "unique_values": {
                        "terms": {
                            "field": f"{target_field}.keyword" if target_field in ["state_name", "district_name"] else target_field,
                            "size": 10
                        }
                    }
                }
            )
            suggestions = [bucket["key"] for bucket in response["aggregations"]["unique_values"]["buckets"]]
            
        return {"success": True, "data": suggestions[:8]}
    except Exception as e:
        if "ConnectionError" in str(type(e)) or "Connection error" in str(e) or "10061" in str(e):
            pass
        else:
            print(f"Suggest Error: {e}")
        fallback_res = _proxy_to_live_api("/suggest", req.model_dump(), current_user)
        if fallback_res is not None:
            return fallback_res
        return {"success": True, "data": []}


# ─────────────────────────────────────────────────────────────
#  BATCH NAME SCREENER  —  NEW ENDPOINTS
# ─────────────────────────────────────────────────────────────




class ScreenerRequest(BaseModel):
    court_type: str = "all"
    threshold: float = 0.90


@app.post("/upload-screener")
async def upload_screener(
    file: UploadFile = File(...),
    court_type: str = Form("all"),
    threshold: float = Form(0.90),
    current_user: str = Depends(get_current_user)
):
    """
    Dynamic Excel parser + Elasticsearch name matching.
    - Uses identical ES phonetic search as Interactive Search for parity.
    - Supports court_type: all | district | high | supreme
    """
    try:
        import openpyxl
    except ImportError:
        raise HTTPException(status_code=500, detail="openpyxl not installed. Run: pip install openpyxl")

    content = await file.read()

    NAME_ALIASES = [
        "name", "party name", "entity name", "company name", "borrower name",
        "customer name", "client name", "vendor name", "contractor name",
        "counterparty name", "applicant name", "petitioner name", "respondent name",
        "litigant name", "accused name", "defendant name", "director name",
        "obligor name", "debtor name", "guarantor name", "individual name",
        "firm name", "organization name", "account name", "subject name", "target name",
        "borrower", "customer", "client", "vendor", "contractor",
        "counterparty", "applicant", "petitioner", "respondent", "litigant",
        "accused", "defendant", "director", "obligor", "debtor",
        "guarantor", "individual", "entity", "company", "party", "firm"
    ]
    PAN_ALIASES     = ["pan no", "pan number", "pan_no", "pancard", "tax id", "id number", "cin", "pan"]
    ADDRESS_ALIASES = ["address", "registered address", "residence", "addr", "location", "city", "state"]
    REGION_ALIASES  = ["region", "zone", "area", "branch", "territory", "district"]
    SL_ALIASES      = ["sl no", "s.no", "sno", "sr no", "serial no", "sr", "serial", "sl", "id", "#"]

    def _clean_header_cell(c: str) -> str:
        return re.sub(r'[^A-Z0-9]', ' ', str(c or '').upper()).strip()

    def find_col(headers_cleaned: list, aliases: list) -> Optional[int]:
        for alias in aliases:
            a_clean = _clean_header_cell(alias)
            for i, h in enumerate(headers_cleaned):
                if not h:
                    continue
                if a_clean == h or a_clean in h or h in a_clean:
                    return i
        return None

    try:
        filename_lower = (file.filename or "").lower()
        raw_rows = []

        if filename_lower.endswith(".csv"):
            import csv
            text_stream = io.StringIO(content.decode("utf-8", errors="replace"))
            reader = csv.reader(text_stream)
            raw_rows = [list(r) for r in reader if any(str(c).strip() for c in r)]
        else:
            wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
            ws = wb.active
            for row in ws.iter_rows(values_only=True):
                if any(c is not None and str(c).strip() for c in row):
                    raw_rows.append(list(row))
            wb.close()

        if not raw_rows:
            raise HTTPException(status_code=400, detail="Uploaded file is empty or contains no data.")

        header_idx = None
        name_col = None
        pan_col = None
        addr_col = None
        reg_col = None
        sl_col = None
        raw_headers = []

        # Search top 15 rows for the true table header (skips decorative titles / blank rows)
        for r_idx, row in enumerate(raw_rows[:15]):
            non_empty = [c for c in row if c is not None and str(c).strip()]
            if len(non_empty) <= 1 and (r_idx + 1 < len(raw_rows)) and len([c for c in raw_rows[r_idx + 1] if c is not None and str(c).strip()]) > 1:
                continue
            row_headers_cleaned = [_clean_header_cell(c) for c in row]
            found_name = find_col(row_headers_cleaned, NAME_ALIASES)
            if found_name is not None:
                header_idx = r_idx
                name_col = found_name
                raw_headers = [str(c).strip() if c is not None else "" for c in row]
                pan_col = find_col(row_headers_cleaned, PAN_ALIASES)
                addr_col = find_col(row_headers_cleaned, ADDRESS_ALIASES)
                reg_col = find_col(row_headers_cleaned, REGION_ALIASES)
                sl_col = find_col(row_headers_cleaned, SL_ALIASES)
                break

        # Fallback if no explicit header matched: check first column with textual names
        if name_col is None:
            row_0 = raw_rows[0]
            raw_headers = [str(c).strip() if c is not None else f"Column {i+1}" for i, c in enumerate(row_0)]
            for col_cand in range(len(row_0)):
                samples = [str(raw_rows[r][col_cand]).strip() for r in range(min(5, len(raw_rows))) if col_cand < len(raw_rows[r])]
                if any(len(s) > 2 and not s.isdigit() for s in samples):
                    name_col = col_cand
                    header_idx = 0
                    break
            if name_col is None:
                name_col = 0
                header_idx = 0

        entities = []
        for row in raw_rows[header_idx + 1:]:
            if not row or all(c is None or str(c).strip() == "" for c in row):
                continue
            raw_name = row[name_col] if name_col < len(row) else None
            name = str(raw_name).strip() if raw_name is not None else ""
            if not name or name.lower() in ("none", "nan", "n/a", "-", "null", "nil", ""):
                continue

            def safe_get(col):
                if col is None or col >= len(row): return ""
                v = row[col]
                return str(v).strip() if v is not None and str(v).lower() not in ("none", "nan", "n/a", "null") else ""

            entities.append({
                "sl_no":   safe_get(sl_col) or str(len(entities) + 1),
                "name":    name,
                "pan":     safe_get(pan_col),
                "address": safe_get(addr_col),
                "region":  safe_get(reg_col),
            })

        detected_columns = {
            "name":    raw_headers[name_col] if name_col is not None and name_col < len(raw_headers) else f"Column {name_col+1}",
            "pan":     raw_headers[pan_col]  if pan_col  is not None and pan_col < len(raw_headers)  else None,
            "address": raw_headers[addr_col] if addr_col is not None and addr_col < len(raw_headers) else None,
            "region":  raw_headers[reg_col]  if reg_col  is not None and reg_col < len(raw_headers)  else None,
            "sl_no":   raw_headers[sl_col]   if sl_col   is not None and sl_col < len(raw_headers)   else None,
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to parse file: {e}")

    if not entities:
        raise HTTPException(status_code=400, detail="No valid entity names found in uploaded file.")

    # ── ES Matching ──
    target_index = INDEX_PATTERN
    if court_type == "district":
        target_index = "court_records"
    elif court_type == "high":
        target_index = "high_court_records"
    elif court_type == "supreme":
        target_index = "supreme_court_records"

    results = []
    
    # Process queries (using sequential for stability)
    overall_has_criminal = False
    for entity in entities:
        name = entity["name"]
        
        # Call Interactive Search internally (pass current_user and set log_query_type=None)
        req = SearchRequest(name=name, court_type=court_type, page=1, size=50)
        search_res = search_records(req, current_user=current_user, log_query_type=None)
        
        matches = []
        max_entity_sim = 0.0
        state_name = None
        has_criminal = False
        has_pending_crim = False
        
        if search_res.get("success") and search_res.get("data"):
            for rec in search_res["data"]:
                confidence = rec.get("confidence", 0)
                if confidence > max_entity_sim:
                    max_entity_sim = confidence
                    state_name = rec.get("state_name")
                if confidence >= (threshold * 100):
                    is_crim = _is_criminal_record_py(rec)
                    if is_crim:
                        has_criminal = True
                        overall_has_criminal = True
                        rec_status = str(rec.get("case_status") or rec.get("status") or "").lower()
                        if any(term in rec_status for term in ("pending", "active", "hearing", "stage", "trial", "pre-trial")):
                            has_pending_crim = True
                    matches.append({
                        "id":                rec.get("cnr") or rec.get("diary_number") or str(rec.get("id")),
                        "cnr":               rec.get("cnr") or rec.get("diary_number") or "",
                        "party_name":        rec.get("party_name", ""),
                        "case_type":         rec.get("case_type") or rec.get("caseType") or ("Criminal Case" if is_crim else "Civil Proceeding"),
                        "acts_sections":     rec.get("acts_sections") or [],
                        "is_criminal":       is_crim,
                        "registration_date": str(rec.get("registration_date") or rec.get("date_of_registration") or ""),
                        "state_name":        rec.get("state_name", ""),
                        "district_name":     rec.get("district_name", ""),
                        "court_name":        rec.get("court_name", ""),
                        "source_index":      rec.get("source_index", ""),
                        "similarity":        confidence,
                        "disposal_is_final": rec.get("disposal_is_final", False),
                        "disposal_nature":   rec.get("disposal_nature", ""),
                        "case_number":       rec.get("case_number", ""),
                    })
        
        # Risk assessment: High Risk if at least 1 criminal case found; Low Risk otherwise
        entity_risk = "high" if has_criminal else "low"
        entity_status = ("pending" if has_pending_crim else "disposed") if has_criminal else "clear"
            
        # Log this specific query under screener_query
        log_query(current_user, "screener_query", state_name, entity_risk, search_query=name, details=f"Batch File: {file.filename}, Row {entity['sl_no']}", case_status=entity_status)
        
        results.append({
            "sl_no":        entity["sl_no"],
            "query_name":   name,
            "pan":          entity["pan"],
            "address":      entity["address"],
            "region":       entity["region"],
            "match_count":  len(matches),
            "matches":      matches,
            "has_criminal": has_criminal,
            "risk_level":   entity_risk
        })
        
    # Log the complete batch upload summary (High Risk if any criminal case found in batch, else Low Risk)
    overall_risk = "high" if overall_has_criminal else "low"
    overall_status = "pending" if overall_has_criminal else "clear"
    log_query(current_user, "screener_upload", None, overall_risk, case_status=overall_status)

    return {
        "success":          True,
        "file_name":        file.filename,
        "total_entities":   len(entities),
        "total_matched":    sum(1 for r in results if r["match_count"] > 0),
        "results":          results,
        "court_type":       court_type,
        "threshold_pct":    round(threshold * 100, 1),
        "detected_columns": detected_columns,
        "overall_risk":     overall_risk
    }


class ReportRequest(BaseModel):
    query_name: str
    pan:        Optional[str] = ""
    address:    Optional[str] = ""
    region:     Optional[str] = ""
    matches:    List[dict]    = []


class CompleteReportRequest(BaseModel):
    results: List[ReportRequest]


@app.post("/download-screener-report")
def download_screener_report(req: ReportRequest, current_user: str = Depends(get_current_user)):
    """Generate and stream a styled .xlsx report for a single screened entity."""
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter
    except ImportError:
        raise HTTPException(status_code=500, detail="openpyxl not installed")

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Screening Report"

    # ── Colour palette ──
    DARK_BG    = "0F121D"
    PURPLE     = "7C3AED"
    GREEN      = "059669"
    ORANGE     = "D97706"
    WHITE      = "FFFFFF"
    LIGHT_GREY = "1E2235"
    MID_GREY   = "374151"

    def cell_style(cell, bold=False, fg=WHITE, bg=None, size=11, align="left", wrap=False):
        cell.font      = Font(name="Calibri", bold=bold, color=fg, size=size)
        cell.alignment = Alignment(horizontal=align, vertical="center", wrap_text=wrap)
        if bg:
            cell.fill = PatternFill("solid", fgColor=bg)

    thin = Side(style="thin", color="374151")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    # ── Title banner ──
    ws.merge_cells("A1:H1")
    ws["A1"] = "BRAINLYNSEARCH AI — Name Screening Verification Report"
    cell_style(ws["A1"], bold=True, fg=WHITE, bg=PURPLE, size=14, align="center")
    ws.row_dimensions[1].height = 32

    # ── Entity info block ──
    has_crim = any(m.get("is_criminal") or _is_criminal_record_py(m) for m in req.matches)
    info_rows = [
        ("Searched Name", req.query_name),
        ("PAN Number",    req.pan or "N/A"),
        ("Address",       req.address or "N/A"),
        ("Region",        req.region or "N/A"),
        ("Total Matches", str(len(req.matches))),
        ("Risk Profile",  "HIGH RISK (Criminal Proceeding Identified)" if has_crim else "LOW RISK (Civil Only / No Criminal Cases)")
    ]
    for i, (label, value) in enumerate(info_rows, start=2):
        ws[f"A{i}"] = label
        ws[f"B{i}"] = value
        cell_style(ws[f"A{i}"], bold=True, fg="A78BFA", bg=LIGHT_GREY)
        val_fg = "F87171" if label == "Risk Profile" and has_crim else ("34D399" if label == "Risk Profile" else WHITE)
        cell_style(ws[f"B{i}"], bold=(label == "Risk Profile"), fg=val_fg, bg=DARK_BG)
        ws.merge_cells(f"B{i}:I{i}")
    ws.row_dimensions[8].height = 4  # spacer

    # ── Match table header ──
    header_row = 9
    headers = ["#", "Party Name (DB)", "CNR", "Case Type", "Similarity %",
               "Registration Date", "State", "District", "Court Name"]
    col_widths = [4, 38, 22, 16, 14, 18, 16, 18, 22]

    for col_idx, (h, w) in enumerate(zip(headers, col_widths), start=1):
        cell = ws.cell(row=header_row, column=col_idx, value=h)
        cell_style(cell, bold=True, fg=WHITE, bg=MID_GREY, align="center")
        cell.border = border
        ws.column_dimensions[get_column_letter(col_idx)].width = w
    ws.row_dimensions[header_row].height = 22

    # ── Match rows ──
    for r_idx, match in enumerate(req.matches, start=1):
        row = header_row + r_idx
        sim = match.get("similarity", 0)
        sim_color = GREEN if sim >= 95 else (ORANGE if sim >= 90 else "DC2626")
        row_bg = DARK_BG if r_idx % 2 == 0 else LIGHT_GREY

        cType = "District Court"
        if match.get("source_index") == "high_court_records":
            cType = "High Court"
        elif match.get("source_index") == "supreme_court_records":
            cType = "Supreme Court"

        is_crim_row = match.get("is_criminal") or _is_criminal_record_py(match)
        case_type_val = match.get("case_type") or ("CRIMINAL" if is_crim_row else "CIVIL")

        values = [
            r_idx,
            match.get("party_name", ""),
            match.get("cnr", ""),
            case_type_val,
            f"{sim}%",
            match.get("registration_date", ""),
            match.get("state_name", ""),
            match.get("district_name", ""),
            cType,
        ]
        for col_idx, val in enumerate(values, start=1):
            cell = ws.cell(row=row, column=col_idx, value=val)
            fg = "F87171" if col_idx == 4 and is_crim_row else (sim_color if col_idx == 5 else WHITE)
            cell_style(cell, fg=fg, bg=row_bg, align="center" if col_idx in (1, 4, 5, 6) else "left", wrap=(col_idx == 2))
            cell.border = border
        ws.row_dimensions[row].height = 18

    # ── Stream the workbook ──
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    safe_name = req.query_name.replace(" ", "_")[:40]
    filename  = f"{safe_name}_Screening_Report.xlsx"

    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

@app.post("/download-complete-screener-report")
def download_complete_screener_report(req: CompleteReportRequest, current_user: str = Depends(get_current_user)):
    """Generate and stream a styled .xlsx report for ALL screened entities in the batch."""
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter
    except ImportError:
        raise HTTPException(status_code=500, detail="openpyxl not installed")

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Complete Batch Summary"

    DARK_BG    = "0F121D"
    PURPLE     = "7C3AED"
    GREEN      = "059669"
    ORANGE     = "D97706"
    WHITE      = "FFFFFF"
    LIGHT_GREY = "1E2235"
    MID_GREY   = "374151"

    def cell_style(cell, bold=False, fg=WHITE, bg=None, size=11, align="left", wrap=False):
        cell.font      = Font(name="Calibri", bold=bold, color=fg, size=size)
        cell.alignment = Alignment(horizontal=align, vertical="center", wrap_text=wrap)
        if bg:
            cell.fill = PatternFill("solid", fgColor=bg)

    thin = Side(style="thin", color="374151")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    ws.merge_cells("A1:J1")
    ws["A1"] = "BRAINLYNSEARCH AI — Complete Batch Screening Summary"
    cell_style(ws["A1"], bold=True, fg=WHITE, bg=PURPLE, size=14, align="center")
    ws.row_dimensions[1].height = 32

    header_row = 3
    headers = ["#", "Searched Name", "Party Name (DB)", "CNR", "Case Type", "Similarity %",
               "Registration Date", "State", "District", "Court Name"]
    col_widths = [5, 28, 38, 22, 16, 14, 18, 16, 18, 22]

    for col_idx, (h, w) in enumerate(zip(headers, col_widths), start=1):
        cell = ws.cell(row=header_row, column=col_idx, value=h)
        cell_style(cell, bold=True, fg=WHITE, bg=MID_GREY, align="center")
        cell.border = border
        ws.column_dimensions[get_column_letter(col_idx)].width = w
    ws.row_dimensions[header_row].height = 22

    current_row = header_row + 1
    
    for r_idx, entity in enumerate(req.results, start=1):
        row_bg = DARK_BG if r_idx % 2 == 0 else LIGHT_GREY
        searched_name = entity.query_name
        
        if not entity.matches:
            # Add a single row indicating No Match
            values = [
                r_idx, searched_name, "No Match Found", "—", "—", "—", "—", "—", "—", "—"
            ]
            for col_idx, val in enumerate(values, start=1):
                cell = ws.cell(row=current_row, column=col_idx, value=val)
                cell_style(cell, fg=WHITE, bg=row_bg, align="center" if col_idx in (1, 5, 6) else "left", wrap=True)
                cell.border = border
            ws.row_dimensions[current_row].height = 18
            current_row += 1
        else:
            # Add a row for each match
            for m_idx, match in enumerate(entity.matches):
                sim = match.get("similarity", 0)
                sim_color = GREEN if sim >= 95 else (ORANGE if sim >= 90 else "DC2626")
                
                cType = "District Court"
                if match.get("source_index") == "high_court_records":
                    cType = "High Court"
                elif match.get("source_index") == "supreme_court_records":
                    cType = "Supreme Court"

                is_crim_row = match.get("is_criminal") or _is_criminal_record_py(match)
                case_type_val = match.get("case_type") or ("CRIMINAL" if is_crim_row else "CIVIL")

                values = [
                    r_idx if m_idx == 0 else "",  # Only show index on first match
                    searched_name if m_idx == 0 else "", # Only show searched name on first match
                    match.get("party_name", ""),
                    match.get("cnr", ""),
                    case_type_val,
                    f"{sim}%",
                    match.get("registration_date", ""),
                    match.get("state_name", ""),
                    match.get("district_name", ""),
                    cType,
                ]
                for col_idx, val in enumerate(values, start=1):
                    cell = ws.cell(row=current_row, column=col_idx, value=val)
                    fg = "F87171" if col_idx == 5 and is_crim_row else (sim_color if col_idx == 6 else WHITE)
                    cell_style(cell, fg=fg, bg=row_bg, align="center" if col_idx in (1, 5, 6) else "left", wrap=(col_idx in (2, 3)))
                    cell.border = border
                ws.row_dimensions[current_row].height = 18
                current_row += 1

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    filename = "Complete_Batch_Screener_Report.xlsx"

    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
@app.get("/cache-stats")
def get_cache_stats(current_user: str = Depends(get_current_user)):
    """Admin endpoint to visually check Redis cache status."""
    try:
        try:
            from api.cache import redis_client
        except ImportError:
            from cache import redis_client
            
        if not redis_client:
            return {"status": "error", "message": "Redis is not connected."}
        
        keys = redis_client.keys("search:*")
        return {
            "status": "online",
            "total_cached_searches": len(keys),
            "sample_keys": [k.decode('utf-8') if isinstance(k, bytes) else k for k in keys[:10]]
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/clear-cache")
def clear_cache(current_user: str = Depends(get_current_user)):
    """Admin endpoint to clear all search cache from Redis."""
    try:
        try:
            from api.cache import redis_client
        except ImportError:
            from cache import redis_client
            
        if not redis_client:
            return {"status": "error", "message": "Redis is not connected."}
        
        keys = redis_client.keys("search:*")
        if keys:
            redis_client.delete(*keys)
        return {"status": "success", "message": f"Cleared {len(keys)} cached search queries."}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/cache-dump")
def cache_dump(current_user: str = Depends(get_current_user)):
    """Admin endpoint to see the full content of all items currently cached in Redis."""
    try:
        try:
            from api.cache import redis_client
        except ImportError:
            from cache import redis_client
            
        if not redis_client:
            return {"status": "error", "message": "Redis is not connected."}
        
        keys = redis_client.keys("search:*")
        dump = {}
        for k in keys:
            key_str = k.decode('utf-8') if isinstance(k, bytes) else k
            val = redis_client.get(k)
            try:
                dump[key_str] = json.loads(val)
            except Exception:
                dump[key_str] = val
                
        return {
            "total_cached": len(keys),
            "cache_contents": dump
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

# ─────────────────────────────────────────────────────────────
#  AUTHENTICATION ENDPOINTS
# ─────────────────────────────────────────────────────────────

@app.get("/login")
def get_login_page():
    login_html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "login.html")
    if not os.path.exists(login_html_path):
        raise HTTPException(status_code=404, detail="Login page template not found")
    return FileResponse(login_html_path)

@app.get("/legal_bg.png")
def get_background_image():
    bg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "legal_bg.png")
    if not os.path.exists(bg_path):
        raise HTTPException(status_code=404, detail="Background image not found")
    return FileResponse(bg_path, media_type="image/png")

@app.get("/dashboard_bg.png")
def get_dashboard_background_image():
    bg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard_bg.png")
    if not os.path.exists(bg_path):
        raise HTTPException(status_code=404, detail="Dashboard background image not found")
    return FileResponse(bg_path, media_type="image/png")

@app.post("/api/auth/signup")
def signup(req: UserSignup):
    try:
        from api.auth import hash_password, generate_otp, send_otp_email
    except ImportError:
        from auth import hash_password, generate_otp, send_otp_email
        
    email = req.email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Invalid email address format.")
        
    # Domain extraction
    domain = email.split("@")[-1]
    
    # Validation based on signup_type
    signup_type = req.signup_type
    org_name = req.org_name
    
    # Exclude public email domains for organization signups
    public_domains = {
        "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", 
        "live.com", "icloud.com", "aol.com", "zoho.com", "mail.com", "yandex.com", "rediffmail.com"
    }
    
    if signup_type in ("org_create", "org_join"):
        if domain in public_domains:
            raise HTTPException(
                status_code=400, 
                detail=f"Public email domains (like {domain}) cannot be used to register an organization. Please use a work email."
            )
            
    conn = get_sqlite_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE email = ?;", (email,))
        if cur.fetchone():
            raise HTTPException(status_code=400, detail="An account with this email already exists.")
            
        role = "individual"
        tenant_id = None
        organization_name = None
        organization_domain = None
        
        if signup_type == "org_create":
            if not org_name or not org_name.strip():
                raise HTTPException(status_code=400, detail="Organization Name is required to create an organization.")
            
            # Check if domain is already registered
            cur.execute("SELECT id FROM users WHERE organization_domain = ? LIMIT 1;", (domain,))
            if cur.fetchone():
                raise HTTPException(status_code=400, detail=f"An organization with the domain '{domain}' is already registered.")
            
            import uuid
            # Generate a unique 8-character tenant ID
            tenant_id = f"org_{uuid.uuid4().hex[:8]}"
            role = "org_admin"
            organization_name = org_name.strip()
            organization_domain = domain
            
        elif signup_type == "org_join":
            # Search for the org_admin of this domain to map tenant_id and org_name
            cur.execute(
                "SELECT tenant_id, organization_name FROM users WHERE organization_domain = ? AND role = 'org_admin' LIMIT 1;",
                (domain,)
            )
            org = cur.fetchone()
            if not org:
                raise HTTPException(
                    status_code=400,
                    detail=f"No organization is registered with the email domain '{domain}'. Please ask your administrator to create it first."
                )
            tenant_id = org["tenant_id"]
            organization_name = org["organization_name"]
            organization_domain = domain
            role = "org_user"
            
        pwd_hash = hash_password(req.password)
        cur.execute(
            """
            INSERT INTO users (email, password_hash, full_name, is_approved, role, tenant_id, organization_name, organization_domain)
            VALUES (?, ?, ?, 0, ?, ?, ?, ?);
            """,
            (email, pwd_hash, req.name, role, tenant_id, organization_name, organization_domain)
        )
        conn.commit()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {e}")
    finally:
        conn.close()
        
    otp = generate_otp()
    _store_otp(email, otp, ttl=60)
        
    send_otp_email(email, otp)
    return {"status": "success", "message": "Verification code sent to email."}

@app.post("/api/auth/verify-otp")
def verify_otp(req: VerifyOtpRequest):
    email = req.email.strip().lower()
    otp = req.otp.strip()
    
    stored_otp = _get_otp(email)
    if not stored_otp:
        raise HTTPException(status_code=400, detail="OTP has expired or does not exist. Please request a new code.")
        
    if stored_otp != otp:
        raise HTTPException(status_code=400, detail="Incorrect verification code. Please try again.")
        
    conn = get_sqlite_conn()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE users SET is_approved = 1 WHERE email = ?;", (email,))
        conn.commit()
        _delete_otp(email)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error during approval: {e}")
    finally:
        conn.close()
        
    return {"status": "success", "message": "Email verified and account activated."}

@app.post("/api/auth/resend-otp")
def resend_otp(req: ResendOtpRequest):
    try:
        from api.auth import generate_otp, send_otp_email
    except ImportError:
        from auth import generate_otp, send_otp_email
        
    email = req.email.strip().lower()
    
    conn = get_sqlite_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE email = ?;", (email,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="User not found.")
    finally:
        conn.close()
        
    otp = generate_otp()
    _store_otp(email, otp, ttl=60)
        
    send_otp_email(email, otp)
    return {"status": "success", "message": "New verification code sent to email."}

@app.post("/api/auth/login")
def login(form_data: OAuth2PasswordRequestForm = Depends()):
    try:
        from api.auth import verify_password, create_access_token
    except ImportError:
        from auth import verify_password, create_access_token
        
    email = form_data.username.strip().lower()
    password = form_data.password
    
    conn = get_sqlite_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT full_name, role, tenant_id, organization_name, password_hash, is_approved FROM users WHERE email = ?;",
            (email,)
        )
        user = cur.fetchone()
        if not user:
            raise HTTPException(status_code=400, detail="Invalid email or password.")
            
        if not verify_password(password, user["password_hash"]):
            raise HTTPException(status_code=400, detail="Invalid email or password.")
            
        if not user["is_approved"]:
            raise HTTPException(status_code=403, detail="Your account registration is still pending verification.")
    finally:
        conn.close()
        
    access_token = create_access_token(data={
        "sub": email,
        "name": user["full_name"],
        "role": user["role"],
        "tenant_id": user["tenant_id"],
        "organization_name": user["organization_name"]
    })
    return {"access_token": access_token, "token_type": "bearer"}

@app.get("/api/me")
@app.get("/api/auth/me")
def get_current_user_profile(current_user: str = Depends(get_current_user)):
    conn = get_sqlite_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, email, full_name, role, tenant_id, organization_name, organization_domain, subscription_status FROM users WHERE LOWER(email) = LOWER(?);",
            (current_user,)
        )
        user = cur.fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="User profile not found.")
        
        user_dict = dict(user)
        sub_status = user_dict.get("subscription_status")
        if not sub_status:
            sub_status = "purchased" if user_dict.get("role") in ["super_admin", "org_admin", "org_user"] else "unpurchased"
        user_dict["subscription_status"] = sub_status

        return {"success": True, "user": user_dict}
    finally:
        conn.close()

class LogEventRequest(BaseModel):
    report_name: Optional[str] = None
    search_query: Optional[str] = None
    details: Optional[str] = None
    risk_level: Optional[str] = "low"

@app.post("/api/stats/log_report")
def log_report(req: Optional[LogEventRequest] = None, current_user: str = Depends(get_current_user)):
    conn = get_sqlite_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT tenant_id FROM users WHERE email = ?;", (current_user,))
        user_row = cur.fetchone()
        tenant_id = user_row["tenant_id"] if user_row else None
        
        rep_name = req.report_name if req else None
        q_term = req.search_query if req else None
        d_str = req.details if req else "SaaS Report Export"
        r_lvl = (req.risk_level if req and req.risk_level in ["high", "medium", "low"] else "low")

        cur.execute(
            """
            INSERT INTO query_logs (user_email, tenant_id, query_type, risk_level, search_query, details) 
            VALUES (?, ?, ?, ?, ?, ?);
            """,
            (current_user, tenant_id, "report_generated", r_lvl, q_term or rep_name, d_str)
        )
        conn.commit()
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()

@app.post("/api/stats/log_download")
def log_download(req: Optional[LogEventRequest] = None, current_user: str = Depends(get_current_user)):
    conn = get_sqlite_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT tenant_id FROM users WHERE email = ?;", (current_user,))
        user_row = cur.fetchone()
        tenant_id = user_row["tenant_id"] if user_row else None

        rep_name = req.report_name if req else None
        q_term = req.search_query if req else None
        d_str = req.details if req else "SaaS PDF/Excel Download"

        cur.execute(
            """
            INSERT INTO query_logs (user_email, tenant_id, query_type, search_query, details) 
            VALUES (?, ?, ?, ?, ?);
            """,
            (current_user, tenant_id, "report_downloaded", q_term or rep_name, d_str)
        )
        conn.commit()
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()

@app.get("/api/stats/audit_logs")
def get_audit_logs(
    metric_type: Optional[str] = "all",
    scope: Optional[str] = "user",
    query: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
    current_user: str = Depends(get_current_user)
):
    conn = get_sqlite_conn()
    try:
        cur = conn.cursor()
        
        # Check current user role and tenant safely
        cur.execute("SELECT role, tenant_id, organization_name FROM users WHERE LOWER(email) = LOWER(?);", (current_user,))
        user_row = cur.fetchone()
            
        user_role = (user_row["role"] if user_row and user_row["role"] else "individual").lower()
        user_tenant = user_row["tenant_id"] if user_row else None
        
        where_clauses = []
        params = []
        
        # Scope Enforcement
        if scope == "admin":
            if user_role != "super_admin":
                raise HTTPException(status_code=403, detail="Super Admin permission required for platform audit logs.")
        elif scope == "org":
            if user_role not in ["org_admin", "super_admin"]:
                raise HTTPException(status_code=403, detail="Org Admin permission required for organization audit logs.")
            where_clauses.append("(q.tenant_id = ? OR u.tenant_id = ?)")
            params.extend([user_tenant, user_tenant])
        else: # "user"
            where_clauses.append("LOWER(q.user_email) = LOWER(?)")
            params.append(current_user)
            
        # Metric Type Filtering
        if metric_type == "searches":
            where_clauses.append("q.query_type IN ('interactive_search', 'screener_query')")
        elif metric_type == "reports":
            where_clauses.append("q.query_type IN ('report_generated', 'screener_upload')")
        elif metric_type == "risk":
            where_clauses.append("q.risk_level = 'high'")
        elif metric_type == "downloads":
            where_clauses.append("q.query_type = 'report_downloaded'")
        elif metric_type == "api":
            where_clauses.append("q.query_type = 'partner_api_search'")

        # Text Query Filtering (search by name, email, query term, details, state)
        if query and query.strip():
            q_clean = f"%{query.strip().lower()}%"
            where_clauses.append("""
                (LOWER(q.user_email) LIKE ? OR 
                 LOWER(COALESCE(u.full_name, '')) LIKE ? OR 
                 LOWER(COALESCE(u.organization_name, '')) LIKE ? OR 
                 LOWER(COALESCE(q.search_query, '')) LIKE ? OR 
                 LOWER(COALESCE(q.details, '')) LIKE ? OR 
                 LOWER(COALESCE(q.state_name, '')) LIKE ?)
            """)
            params.extend([q_clean] * 6)

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        
        # Total Count
        cur.execute(f"""
            SELECT COUNT(*) 
            FROM query_logs q
            LEFT JOIN users u ON LOWER(q.user_email) = LOWER(u.email)
            {where_sql};
        """, params)
        total_count = cur.fetchone()[0]

        # Log Items Query
        cur.execute(f"""
            SELECT 
                q.id,
                q.user_email,
                q.tenant_id,
                q.query_type,
                q.state_name,
                q.risk_level,
                q.search_query,
                q.details,
                q.created_at,
                u.full_name,
                u.organization_name,
                u.organization_domain,
                u.role
            FROM query_logs q
            LEFT JOIN users u ON LOWER(q.user_email) = LOWER(u.email)
            {where_sql}
            ORDER BY q.created_at DESC
            LIMIT ? OFFSET ?;
        """, params + [limit, offset])

        rows = cur.fetchall()
        logs = [dict(r) for r in rows]

        return {
            "success": True,
            "metric_type": metric_type,
            "scope": scope,
            "total": total_count,
            "logs": logs
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch audit logs: {e}")
    finally:
        conn.close()

def get_current_super_admin(current_user: str = Depends(get_current_user)):
    conn = get_sqlite_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT role FROM users WHERE LOWER(email) = LOWER(?);", (current_user,))
        row = cur.fetchone()
        role = (row["role"] if row else None) or "individual"
        if role != "super_admin" and current_user.lower() not in ["anandavelan50@gmail.com", "balasubramaniam.mohan@gmail.com"]:
            raise HTTPException(status_code=403, detail="Access denied. Super Admin authorization required.")
        return current_user
    finally:
        conn.close()

def get_current_admin_user(current_user: str = Depends(get_current_user)):
    conn = get_sqlite_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT role FROM users WHERE LOWER(email) = LOWER(?);", (current_user,))
        row = cur.fetchone()
        role = (row["role"] if row else None) or "individual"
        if role not in ["super_admin", "org_admin"] and current_user.lower() not in ["anandavelan50@gmail.com", "balasubramaniam.mohan@gmail.com", "admin@brainlyn.com"]:
            raise HTTPException(status_code=403, detail="Access denied. Admin authorization required.")
        return current_user
    finally:
        conn.close()

@app.get("/api/me")
def get_me(current_user: str = Depends(get_current_user)):
    conn = get_sqlite_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, email, full_name, is_approved, role, tenant_id, organization_name, organization_domain, subscription_status, created_at FROM users WHERE email = ?;",
            (current_user,)
        )
        user = cur.fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="User profile not found.")
        
        user_role = user["role"] or "individual"
        sub_status = user["subscription_status"] if user["subscription_status"] is not None else ("purchased" if user_role in ["super_admin", "org_admin", "org_user"] else "unpurchased")

        return {
            "success": True,
            "user": {
                "id": user["id"],
                "email": user["email"],
                "full_name": user["full_name"],
                "role": user_role,
                "tenant_id": user["tenant_id"],
                "organization_name": user["organization_name"],
                "organization_domain": user["organization_domain"],
                "subscription_status": sub_status,
                "is_approved": bool(user["is_approved"]),
                "created_at": user["created_at"]
            }
        }
    finally:
        conn.close()

@app.get("/api/stats/usage")
def get_usage_stats(
    target_user: Optional[str] = None,
    current_user: str = Depends(get_current_user)
):
    conn = get_sqlite_conn()
    try:
        cur = conn.cursor()
        
        # Get caller details
        cur.execute("SELECT role, tenant_id, organization_name FROM users WHERE email = ?;", (current_user,))
        caller_row = cur.fetchone()
        if not caller_row:
            raise HTTPException(status_code=404, detail="User not found.")
            
        caller_role = caller_row["role"] or "individual"
        caller_tenant_id = caller_row["tenant_id"]
        
        target_profile = None
        user_to_query = current_user
        is_targeted = bool(target_user and target_user.strip())

        if is_targeted:
            clean_target = target_user.strip().lower()
            if caller_role not in ["super_admin", "org_admin"] and current_user.lower() not in ["anandavelan50@gmail.com", "admin@brainlyn.com"]:
                raise HTTPException(status_code=403, detail="Permission denied to inspect other user analytics")
            
            cur.execute("""
                SELECT id, email, full_name, role, organization_name, organization_domain, tenant_id, is_approved, created_at 
                FROM users 
                WHERE LOWER(email) = ?;
            """, (clean_target,))
            t_row = cur.fetchone()
            if not t_row:
                raise HTTPException(status_code=404, detail=f"Target user '{target_user}' not found.")
            
            # If org_admin, ensure target belongs to their tenant
            if caller_role == "org_admin":
                if t_row["tenant_id"] != caller_tenant_id and (not t_row["organization_name"] or (caller_row["organization_name"] and t_row["organization_name"].lower() != caller_row["organization_name"].lower())):
                    raise HTTPException(status_code=403, detail="Permission denied to inspect users outside your organization.")
                if t_row["role"] == "super_admin":
                    raise HTTPException(status_code=403, detail="Permission denied: Org Admin cannot view Super Admin data.")
                
            user_to_query = t_row["email"]
            target_profile = {
                "id": t_row["id"],
                "email": t_row["email"],
                "full_name": t_row["full_name"] or t_row["email"].split('@')[0],
                "role": t_row["role"] or "individual",
                "organization_name": t_row["organization_name"] or "Individual Account",
                "organization_domain": t_row["organization_domain"],
                "tenant_id": t_row["tenant_id"],
                "is_approved": bool(t_row["is_approved"]),
                "created_at": t_row["created_at"]
            }
            role = t_row["role"] or "individual"
            tenant_id = t_row["tenant_id"]
            org_name = t_row["organization_name"]
            is_org = False
            is_super_admin = False
        else:
            role = caller_role
            tenant_id = caller_tenant_id
            org_name = caller_row["organization_name"]
            is_org_admin = (role == "org_admin")
            is_super_admin = (role == "super_admin")
            is_org = bool(tenant_id) and (is_org_admin or is_super_admin)
        
        # Base filter for the active target user
        user_filter = "LOWER(user_email) = LOWER(?)"
        user_param = (user_to_query,)
        
        org_filter = "tenant_id = ?"
        org_param = (tenant_id,)
        
        # Retrieve personal metrics
        cur.execute(
            f"SELECT COUNT(*) FROM query_logs WHERE {user_filter} AND query_type IN ('interactive_search', 'screener_query');",
            user_param
        )
        total_searches = cur.fetchone()[0]

        cur.execute(
            f"SELECT COUNT(*) FROM query_logs WHERE {user_filter} AND query_type = 'partner_api_search';",
            user_param
        )
        api_queries = cur.fetchone()[0]
        
        cur.execute(
            f"SELECT COUNT(*) FROM query_logs WHERE {user_filter} AND query_type IN ('report_generated', 'screener_upload');",
            user_param
        )
        reports_generated = cur.fetchone()[0]
        
        cur.execute(
            f"SELECT COUNT(*) FROM query_logs WHERE {user_filter} AND query_type = 'report_downloaded';",
            user_param
        )
        reports_downloaded = cur.fetchone()[0]
        
        cur.execute(
            f"SELECT COUNT(*) FROM query_logs WHERE {user_filter} AND query_type IN ('interactive_search', 'screener_query', 'partner_api_search') AND risk_level = 'high';",
            user_param
        )
        high_risk_found = cur.fetchone()[0]

        cur.execute(
            f"SELECT COUNT(*) FROM query_logs WHERE {user_filter} AND query_type IN ('interactive_search', 'screener_query', 'partner_api_search') AND risk_level = 'high' AND case_status = 'disposed';",
            user_param
        )
        high_risk_disposed = cur.fetchone()[0]
        high_risk_pending = max(0, high_risk_found - high_risk_disposed)
        
        # Organization Metrics (only for org_admin or super_admin with a tenant_id when not targeting specific individual)
        org_searches = 0
        org_api_queries = 0
        org_reports = 0
        org_downloads = 0
        org_high_risk = 0
        org_high_risk_pending = 0
        org_high_risk_disposed = 0
        member_usage = []
        
        if is_org and not is_targeted:
            if caller_role == "org_admin":
                cur.execute(
                    f"""SELECT COUNT(*) FROM query_logs q 
                        JOIN users u ON LOWER(q.user_email) = LOWER(u.email)
                        WHERE (q.tenant_id = ? OR LOWER(u.organization_name) = LOWER(?)) 
                          AND u.role IN ('org_admin', 'org_user')
                          AND q.query_type IN ('interactive_search', 'screener_query');""",
                    (tenant_id, (org_name or "").lower())
                )
                org_searches = cur.fetchone()[0]

                cur.execute(
                    f"""SELECT COUNT(*) FROM query_logs q 
                        JOIN users u ON LOWER(q.user_email) = LOWER(u.email)
                        WHERE (q.tenant_id = ? OR LOWER(u.organization_name) = LOWER(?)) 
                          AND u.role IN ('org_admin', 'org_user')
                          AND q.query_type = 'partner_api_search';""",
                    (tenant_id, (org_name or "").lower())
                )
                org_api_queries = cur.fetchone()[0]
                
                cur.execute(
                    f"""SELECT COUNT(*) FROM query_logs q 
                        JOIN users u ON LOWER(q.user_email) = LOWER(u.email)
                        WHERE (q.tenant_id = ? OR LOWER(u.organization_name) = LOWER(?)) 
                          AND u.role IN ('org_admin', 'org_user')
                          AND q.query_type IN ('report_generated', 'screener_upload');""",
                    (tenant_id, (org_name or "").lower())
                )
                org_reports = cur.fetchone()[0]
                
                cur.execute(
                    f"""SELECT COUNT(*) FROM query_logs q 
                        JOIN users u ON LOWER(q.user_email) = LOWER(u.email)
                        WHERE (q.tenant_id = ? OR LOWER(u.organization_name) = LOWER(?)) 
                          AND u.role IN ('org_admin', 'org_user')
                          AND q.query_type = 'report_downloaded';""",
                    (tenant_id, (org_name or "").lower())
                )
                org_downloads = cur.fetchone()[0]
                
                cur.execute(
                    f"""SELECT COUNT(*) FROM query_logs q 
                        JOIN users u ON LOWER(q.user_email) = LOWER(u.email)
                        WHERE (q.tenant_id = ? OR LOWER(u.organization_name) = LOWER(?)) 
                          AND u.role IN ('org_admin', 'org_user')
                          AND q.query_type IN ('interactive_search', 'screener_query') 
                          AND q.risk_level = 'high';""",
                    (tenant_id, (org_name or "").lower())
                )
                org_high_risk = cur.fetchone()[0]

                cur.execute(
                    f"""SELECT COUNT(*) FROM query_logs q 
                        JOIN users u ON LOWER(q.user_email) = LOWER(u.email)
                        WHERE (q.tenant_id = ? OR LOWER(u.organization_name) = LOWER(?)) 
                          AND u.role IN ('org_admin', 'org_user')
                          AND q.query_type IN ('interactive_search', 'screener_query') 
                          AND q.risk_level = 'high'
                          AND q.case_status = 'disposed';""",
                    (tenant_id, (org_name or "").lower())
                )
                org_high_risk_disposed = cur.fetchone()[0]
                org_high_risk_pending = max(0, org_high_risk - org_high_risk_disposed)
                
                cur.execute(
                    """
                    SELECT email, full_name, role, created_at 
                    FROM users 
                    WHERE (tenant_id = ? OR LOWER(organization_name) = LOWER(?)) 
                      AND role IN ('org_admin', 'org_user')
                    ORDER BY role DESC, full_name ASC;
                    """,
                    (tenant_id, (org_name or "").lower())
                )
                members = cur.fetchall()
            else:
                cur.execute(
                    f"SELECT COUNT(*) FROM query_logs WHERE {org_filter} AND query_type IN ('interactive_search', 'screener_query');",
                    org_param
                )
                org_searches = cur.fetchone()[0]

                cur.execute(
                    f"SELECT COUNT(*) FROM query_logs WHERE {org_filter} AND query_type = 'partner_api_search';",
                    org_param
                )
                org_api_queries = cur.fetchone()[0]
                
                cur.execute(
                    f"SELECT COUNT(*) FROM query_logs WHERE {org_filter} AND query_type IN ('report_generated', 'screener_upload');",
                    org_param
                )
                org_reports = cur.fetchone()[0]
                
                cur.execute(
                    f"SELECT COUNT(*) FROM query_logs WHERE {org_filter} AND query_type = 'report_downloaded';",
                    org_param
                )
                org_downloads = cur.fetchone()[0]
                
                cur.execute(
                    f"SELECT COUNT(*) FROM query_logs WHERE {org_filter} AND query_type IN ('interactive_search', 'screener_query', 'partner_api_search') AND risk_level = 'high';",
                    org_param
                )
                org_high_risk = cur.fetchone()[0]

                cur.execute(
                    f"SELECT COUNT(*) FROM query_logs WHERE {org_filter} AND query_type IN ('interactive_search', 'screener_query', 'partner_api_search') AND risk_level = 'high' AND case_status = 'disposed';",
                    org_param
                )
                org_high_risk_disposed = cur.fetchone()[0]
                org_high_risk_pending = max(0, org_high_risk - org_high_risk_disposed)
                
                cur.execute(
                    """
                    SELECT email, full_name, role, created_at 
                    FROM users 
                    WHERE tenant_id = ? 
                    ORDER BY role DESC, full_name ASC;
                    """,
                    org_param
                )
                members = cur.fetchall()
            for m in members:
                cur.execute(
                    "SELECT COUNT(*) FROM query_logs WHERE user_email = ? AND query_type IN ('interactive_search', 'screener_query');",
                    (m["email"],)
                )
                m_searches = cur.fetchone()[0]

                cur.execute(
                    "SELECT COUNT(*) FROM query_logs WHERE user_email = ? AND query_type = 'partner_api_search';",
                    (m["email"],)
                )
                m_api = cur.fetchone()[0]
                
                member_usage.append({
                    "email": m["email"],
                    "name": m["full_name"],
                    "role": m["role"] or "org_user",
                    "searches": m_searches,
                    "api_queries": m_api,
                    "joined_at": m["created_at"]
                })

        # Super Admin Platform-Wide Metrics
        system_metrics = None
        if is_super_admin and not is_targeted:
            cur.execute("SELECT COUNT(*) FROM users;")
            total_users_cnt = cur.fetchone()[0]
            
            cur.execute("SELECT COUNT(DISTINCT tenant_id) FROM users WHERE tenant_id IS NOT NULL AND tenant_id != '';")
            active_orgs_cnt = cur.fetchone()[0]
            
            cur.execute("SELECT COUNT(*) FROM users WHERE is_approved = 0;")
            pending_approvals_cnt = cur.fetchone()[0]
            
            cur.execute("SELECT COUNT(*) FROM query_logs WHERE query_type IN ('interactive_search', 'screener_query');")
            total_system_searches = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM query_logs WHERE query_type = 'partner_api_search';")
            total_system_api = cur.fetchone()[0]
            
            cur.execute("SELECT COUNT(*) FROM query_logs WHERE query_type IN ('interactive_search', 'screener_query', 'partner_api_search') AND risk_level = 'high';")
            total_system_high_risk = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM query_logs WHERE query_type IN ('interactive_search', 'screener_query', 'partner_api_search') AND risk_level = 'high' AND case_status = 'disposed';")
            total_system_high_risk_disposed = cur.fetchone()[0]
            total_system_high_risk_pending = max(0, total_system_high_risk - total_system_high_risk_disposed)

            system_metrics = {
                "total_users": total_users_cnt,
                "active_orgs": active_orgs_cnt,
                "pending_approvals": pending_approvals_cnt,
                "total_searches": total_system_searches,
                "total_api_queries": total_system_api,
                "total_system_api_queries": total_system_api,
                "total_high_risk": total_system_high_risk,
                "total_high_risk_pending": total_system_high_risk_pending,
                "total_high_risk_disposed": total_system_high_risk_disposed
            }
                
        # Time-series data for "Search Volume (30 days)"
        import datetime
        today = datetime.date.today()
        dates_list = [today - datetime.timedelta(days=i) for i in range(29, -1, -1)]
        search_volume_by_day = {d.strftime("%Y-%m-%d"): 0 for d in dates_list}
        
        cur.execute(
            f"""
            SELECT date(created_at) as log_date, COUNT(*) as cnt 
            FROM query_logs 
            WHERE {user_filter} AND query_type IN ('interactive_search', 'screener_query', 'partner_api_search')
            GROUP BY log_date;
            """,
            user_param
        )
        for row in cur.fetchall():
            d_str = row["log_date"]
            if d_str in search_volume_by_day:
                search_volume_by_day[d_str] = row["cnt"]
                
        # Case Type Donut
        cur.execute(
            f"""
            SELECT query_type, COUNT(*) as cnt 
            FROM query_logs 
            WHERE {user_filter} AND query_type IN ('interactive_search', 'screener_query', 'partner_api_search')
            GROUP BY query_type;
            """,
            user_param
        )
        case_types = {"interactive": 0, "batch": 0, "api": 0}
        for row in cur.fetchall():
            if row["query_type"] == "interactive_search":
                case_types["interactive"] = row["cnt"]
            elif row["query_type"] == "screener_query":
                case_types["batch"] = row["cnt"]
            elif row["query_type"] == "partner_api_search":
                case_types["api"] = row["cnt"]
                
        # Risk Trend
        risk_trend = {d.strftime("%Y-%m-%d"): {"high": 0, "medium": 0, "low": 0} for d in dates_list}
        cur.execute(
            f"""
            SELECT date(created_at) as log_date, risk_level, COUNT(*) as cnt 
            FROM query_logs 
            WHERE {user_filter} AND query_type IN ('interactive_search', 'screener_query', 'partner_api_search')
            GROUP BY log_date, risk_level;
            """,
            user_param
        )
        for row in cur.fetchall():
            d_str = row["log_date"]
            r_lvl = row["risk_level"]
            if d_str in risk_trend:
                risk_trend[d_str][r_lvl] = row["cnt"]
                
        # Top States with Canonical Normalization & Merge
        cur.execute(
            f"""
            SELECT state_name, COUNT(*) as cnt 
            FROM query_logs 
            WHERE {user_filter} AND state_name IS NOT NULL AND state_name != ''
            GROUP BY state_name;
            """,
            user_param
        )
        merged_states: Dict[str, int] = {}
        for row in cur.fetchall():
            norm = normalize_indian_state(row["state_name"])
            if norm:
                merged_states[norm] = merged_states.get(norm, 0) + row["cnt"]

        sorted_states = sorted(merged_states.items(), key=lambda x: x[1], reverse=True)[:5]
        top_states = [{ "state": s, "count": c } for s, c in sorted_states]
        
        return {
            "success": True,
            "role": role,
            "tenant_id": tenant_id,
            "org_name": org_name,
            "target_profile": target_profile,
            "metrics": {
                "total_searches": total_searches,
                "api_queries": api_queries,
                "reports_generated": reports_generated,
                "reports_downloaded": reports_downloaded,
                "high_risk_found": high_risk_found,
                "high_risk_pending": high_risk_pending,
                "high_risk_disposed": high_risk_disposed
            },
            "org_metrics": {
                "total_searches": org_searches,
                "org_api_queries": org_api_queries,
                "api_queries": org_api_queries,
                "reports_generated": org_reports,
                "reports_downloaded": org_downloads,
                "high_risk_found": org_high_risk,
                "high_risk_pending": org_high_risk_pending,
                "high_risk_disposed": org_high_risk_disposed
            } if is_org else None,
            "system_metrics": system_metrics,
            "search_volume": [
                {"date": d, "count": cnt} for d, cnt in search_volume_by_day.items()
            ],
            "case_types": case_types,
            "risk_trend": [
                {"date": d, "high": val["high"], "medium": val["medium"], "low": val["low"]} 
                for d, val in risk_trend.items()
            ],
            "top_states": top_states,
            "members": member_usage
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch usage stats: {e}")
    finally:
        conn.close()

@app.get("/api/stats/audit_logs")
def get_audit_logs(
    metric_type: str = "searches",
    scope: str = "user",
    target_user: Optional[str] = None,
    current_user: str = Depends(get_current_user)
):
    conn = get_sqlite_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT role, tenant_id, organization_name, organization_domain FROM users WHERE LOWER(email) = LOWER(?);", (current_user,))
        u_row = cur.fetchone()
        if not u_row:
            raise HTTPException(status_code=404, detail="User not found")

        role = u_row["role"] or "individual"
        tenant_id = u_row["tenant_id"]
        org_name = u_row["organization_name"]
        org_dom = u_row["organization_domain"]

        where_clauses = []
        params = []

        if target_user and target_user.strip():
            # If a specific user is targeted by super_admin or org_admin
            if role == "org_admin":
                cur.execute("SELECT role, tenant_id, organization_name FROM users WHERE LOWER(email) = LOWER(?);", (target_user.strip(),))
                t_user = cur.fetchone()
                if not t_user or t_user["role"] == "super_admin":
                    raise HTTPException(status_code=403, detail="Permission denied: Org Admin cannot view Super Admin audit logs.")
                if t_user["tenant_id"] != tenant_id and (not t_user["organization_name"] or t_user["organization_name"].lower() != (org_name or "").lower()):
                    raise HTTPException(status_code=403, detail="Permission denied to view audit logs outside your organization.")
            where_clauses.append("LOWER(q.user_email) = LOWER(?)")
            params.append(target_user.strip())
        elif scope == "admin" and role == "super_admin":
            pass  # Platform wide logs for super admin
        elif scope == "org" or (scope == "admin" and role == "org_admin"):
            if role == "org_admin":
                where_clauses.append("(q.tenant_id = ? OR LOWER(u.organization_name) = LOWER(?) OR LOWER(u.organization_domain) = LOWER(?))")
                where_clauses.append("u.role IN ('org_admin', 'org_user')")
                params.extend([tenant_id or "", (org_name or "").lower(), (org_dom or "").lower()])
            elif role == "super_admin" and (tenant_id or org_name or org_dom):
                where_clauses.append("(q.tenant_id = ? OR LOWER(u.organization_name) = LOWER(?) OR LOWER(u.organization_domain) = LOWER(?))")
                params.extend([tenant_id or "", (org_name or "").lower(), (org_dom or "").lower()])
            else:
                where_clauses.append("LOWER(q.user_email) = LOWER(?)")
                params.append(current_user)
        else:
            where_clauses.append("LOWER(q.user_email) = LOWER(?)")
            params.append(current_user)

        if metric_type == "searches":
            where_clauses.append("q.query_type IN ('interactive_search', 'screener_query')")
        elif metric_type == "reports":
            where_clauses.append("q.query_type IN ('report_generated', 'screener_upload')")
        elif metric_type == "risk":
            where_clauses.append("q.risk_level = 'high'")
        elif metric_type == "downloads":
            where_clauses.append("q.query_type = 'report_downloaded'")
        elif metric_type == "api":
            where_clauses.append("q.query_type = 'partner_api_search'")

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        cur.execute(f"""
            SELECT 
                q.id, q.user_email, q.tenant_id, q.query_type, q.state_name, 
                q.risk_level, q.case_status, q.search_query, q.details, q.created_at,
                u.full_name, u.organization_name
            FROM query_logs q
            LEFT JOIN users u ON LOWER(q.user_email) = LOWER(u.email)
            {where_sql}
            ORDER BY q.created_at DESC
            LIMIT 500;
        """, params)

        rows = cur.fetchall()
        logs = [dict(r) for r in rows]

        return {
            "success": True,
            "metric_type": metric_type,
            "scope": scope,
            "count": len(logs),
            "logs": logs
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch audit logs: {e}")
    finally:
        conn.close()

# ─────────────────────────────────────────────────────────────
#  ADMIN & ORG ADMIN USER MANAGEMENT ENDPOINTS
# ─────────────────────────────────────────────────────────────

@app.get("/api/admin/users")
def get_all_users(admin_user: str = Depends(get_current_admin_user)):
    conn = get_sqlite_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT role, tenant_id, organization_name, organization_domain FROM users WHERE LOWER(email) = LOWER(?);", (admin_user,))
        caller = cur.fetchone()
        if not caller:
            raise HTTPException(status_code=404, detail="Admin user not found")
        
        caller_role = caller["role"] or "individual"
        caller_tenant = caller["tenant_id"]
        caller_org_name = caller["organization_name"]

        where_clause = ""
        params = []
        if caller_role == "org_admin":
            if not caller_tenant and not caller_org_name:
                raise HTTPException(status_code=400, detail="Org Admin is not assigned to an organization.")
            where_clause = "WHERE (u.tenant_id = ? OR LOWER(u.organization_name) = LOWER(?)) AND u.role IN ('org_admin', 'org_user')"
            params = [caller_tenant or "", (caller_org_name or "").lower()]

        cur.execute(f"""
            SELECT 
                u.id, 
                u.email, 
                u.full_name, 
                u.role, 
                u.tenant_id, 
                u.organization_name, 
                u.organization_domain, 
                u.is_approved, 
                u.subscription_status,
                u.created_at,
                COALESCE(SUM(CASE WHEN q.query_type IN ('interactive_search', 'screener_query') THEN 1 ELSE 0 END), 0) AS total_searches,
                COALESCE(SUM(CASE WHEN q.query_type IN ('report_generated', 'screener_upload') THEN 1 ELSE 0 END), 0) AS total_reports
            FROM users u
            LEFT JOIN query_logs q ON LOWER(u.email) = LOWER(q.user_email)
            {where_clause}
            GROUP BY u.id
            ORDER BY u.created_at DESC;
        """, params)
        rows = cur.fetchall()
        users = []
        org_map = {}

        for r in rows:
            u_dict = dict(r)
            u_dict["is_approved"] = bool(u_dict["is_approved"])
            u_dict["role"] = u_dict["role"] or "individual"
            u_dict["subscription_status"] = u_dict.get("subscription_status") if u_dict.get("subscription_status") is not None else ("purchased" if u_dict["role"] in ["super_admin", "org_admin", "org_user"] else "unpurchased")
            users.append(u_dict)

            # Grouping into Organizations / Domains
            org_key = u_dict["organization_name"] or u_dict["organization_domain"] or u_dict["tenant_id"] or "Individual Accounts"
            if org_key not in org_map:
                display_name = u_dict["organization_name"] or u_dict["organization_domain"] or u_dict["tenant_id"] or "Individual Accounts"
                org_map[org_key] = {
                    "organization_name": display_name,
                    "organization_domain": u_dict["organization_domain"] or None,
                    "tenant_id": u_dict["tenant_id"] or None,
                    "member_count": 0,
                    "total_searches": 0,
                    "total_reports": 0,
                    "members": []
                }
            
            org_map[org_key]["member_count"] += 1
            org_map[org_key]["total_searches"] += u_dict["total_searches"]
            org_map[org_key]["total_reports"] += u_dict["total_reports"]
            org_map[org_key]["members"].append(u_dict)

        organizations = list(org_map.values())

        return {
            "success": True,
            "total": len(users),
            "users": users,
            "organizations": organizations
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch users: {e}")
    finally:
        conn.close()

@app.get("/api/admin/billing/report")
def get_billing_report(
    entity_type: str = "organization",
    target_id: Optional[str] = None,
    billing_month: Optional[str] = "all",
    rate_search: float = 0.05,
    rate_report: float = 0.25,
    rate_api: float = 0.03,
    rate_risk: Optional[float] = None,
    current_user: str = Depends(get_current_admin_user)
):
    conn = get_sqlite_conn()
    try:
        cur = conn.cursor()
        import random, datetime

        cur.execute("SELECT role, tenant_id, organization_name, organization_domain FROM users WHERE LOWER(email) = LOWER(?);", (current_user,))
        caller_rec = cur.fetchone()
        caller_role = caller_rec["role"] if caller_rec else "individual"

        where_clauses = []
        params = []

        billed_to = {
            "entity_type": entity_type,
            "organization_name": "Individual Account",
            "organization_domain": None,
            "tenant_id": None,
            "admin_email": target_id or "N/A",
            "full_name": "Individual Billed Account"
        }

        user_emails = []
        if entity_type == "organization" and target_id:
            clean_target = target_id.strip()
            role_filter = "AND role IN ('org_admin', 'org_user')" if caller_role == "org_admin" else ""
            cur.execute(f"""
                SELECT email, organization_name, organization_domain, tenant_id, full_name, role 
                FROM users 
                WHERE (tenant_id = ? OR LOWER(organization_domain) = LOWER(?) OR LOWER(organization_name) = LOWER(?)) {role_filter};
            """, (clean_target, clean_target, clean_target))
            org_users = cur.fetchall()
            for u_r in org_users:
                user_emails.append(u_r["email"].lower())
                if u_r["role"] in ["org_admin", "super_admin"] or not billed_to["tenant_id"]:
                    billed_to["tenant_id"] = u_r["tenant_id"] or clean_target
                    billed_to["organization_name"] = u_r["organization_name"] or clean_target
                    billed_to["organization_domain"] = u_r["organization_domain"]
                    billed_to["admin_email"] = u_r["email"]
                    billed_to["full_name"] = u_r["full_name"] or u_r["email"]

            if not billed_to["tenant_id"]:
                billed_to["organization_name"] = clean_target
                billed_to["tenant_id"] = clean_target

            if user_emails:
                placeholders = ",".join(["?"] * len(user_emails))
                where_clauses.append(f"(q.tenant_id = ? OR LOWER(q.user_email) IN ({placeholders}))")
                params.append(clean_target)
                params.extend(user_emails)
            else:
                where_clauses.append("q.tenant_id = ?")
                params.append(clean_target)

        elif entity_type == "individual" and target_id:
            clean_target = target_id.strip().lower()
            cur.execute("SELECT email, full_name, role, organization_name, organization_domain, tenant_id FROM users WHERE LOWER(email) = ?;", (clean_target,))
            u_row = cur.fetchone()
            if u_row:
                if caller_role == "org_admin" and u_row["role"] == "super_admin":
                    raise HTTPException(status_code=403, detail="Permission denied: Org Admin cannot view Super Admin billing.")
                billed_to["admin_email"] = u_row["email"]
                billed_to["full_name"] = u_row["full_name"] or u_row["email"]
                billed_to["organization_name"] = u_row["organization_name"] or "Individual Account"
                billed_to["organization_domain"] = u_row["organization_domain"]
                billed_to["tenant_id"] = u_row["tenant_id"]
            
            where_clauses.append("LOWER(q.user_email) = ?")
            params.append(clean_target)

        # Billing Month Filter
        if billing_month and billing_month != "all":
            where_clauses.append("strftime('%Y-%m', q.created_at) = ?")
            params.append(billing_month)

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        # Fetch Aggregated Counts using fast indexed queries
        cur.execute(f"""
            SELECT 
                SUM(CASE WHEN q.query_type IN ('interactive_search', 'screener_query') THEN 1 ELSE 0 END) as total_searches,
                SUM(CASE WHEN q.query_type IN ('report_generated', 'screener_upload') THEN 1 ELSE 0 END) as total_reports,
                SUM(CASE WHEN q.query_type = 'partner_api_search' THEN 1 ELSE 0 END) as total_api,
                SUM(CASE WHEN q.query_type = 'report_downloaded' THEN 1 ELSE 0 END) as total_downloads
            FROM query_logs q
            {where_sql};
        """, params)

        summary_row = cur.fetchone()
        searches_cnt = (summary_row["total_searches"] or 0) if summary_row else 0
        reports_cnt = (summary_row["total_reports"] or 0) if summary_row else 0
        api_cnt = (summary_row["total_api"] or 0) if summary_row else 0
        downloads_cnt = (summary_row["total_downloads"] or 0) if summary_row else 0

        search_amount = round(searches_cnt * rate_search, 2)
        report_amount = round(reports_cnt * rate_report, 2)
        api_amount = round(api_cnt * rate_api, 2)
        subtotal = round(search_amount + report_amount + api_amount, 2)
        tax_rate = 0.08
        tax_amount = round(subtotal * tax_rate, 2)
        total_amount = round(subtotal + tax_amount, 2)

        now = datetime.datetime.now()
        inv_no = f"INV-{now.strftime('%Y%m')}-{random.randint(1000, 9999)}"

        line_items = [
            {
                "description": "Interactive & Screener Database Searches",
                "volume": searches_cnt,
                "rate": f"${rate_search:.2f} / search",
                "amount": f"${search_amount:.2f}"
            },
            {
                "description": "AI Legal Analysis Reports Generated",
                "volume": reports_cnt,
                "rate": f"${rate_report:.2f} / report",
                "amount": f"${report_amount:.2f}"
            },
            {
                "description": "Partner REST API v1 Enterprise Queries",
                "volume": api_cnt,
                "rate": f"${rate_api:.2f} / query",
                "amount": f"${api_amount:.2f}"
            },
            {
                "description": "PDF / Excel Audit Export Access",
                "volume": downloads_cnt,
                "rate": "Included",
                "amount": "$0.00"
            }
        ]

        # Fetch Itemized Audit Logs for Reconciliation
        cur.execute(f"""
            SELECT 
                q.id, q.user_email, q.query_type, q.state_name, q.risk_level, 
                q.search_query, q.details, q.created_at
            FROM query_logs q
            {where_sql}
            ORDER BY q.created_at DESC
            LIMIT 100;
        """, params)

        rows = cur.fetchall()
        itemized_logs = [dict(r) for r in rows]

        return {
            "success": True,
            "invoice_no": inv_no,
            "date_generated": now.strftime("%B %d, %Y"),
            "billing_month": billing_month,
            "billed_to": billed_to,
            "issuer": {
                "company": "Brainlyn AI Search Platform",
                "department": "Enterprise Billing Operations",
                "email": "billing@brainlyn.com",
                "website": "https://brainlyn.com"
            },
            "line_items": line_items,
            "subtotal": f"${subtotal:.2f}",
            "tax_rate": "8%",
            "tax_amount": f"${tax_amount:.2f}",
            "total_amount": f"${total_amount:.2f}",
            "itemized_logs": itemized_logs
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to generate billing report: {e}")
    finally:
        conn.close()

@app.post("/api/admin/users")
def create_user_by_admin(req: CreateUserRequest, admin_user: str = Depends(get_current_admin_user)):
    email = req.email.lower().strip()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Invalid email address.")
    
    password = req.password or "User@123"
    role = req.role.lower().strip()

    conn = get_sqlite_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT role, tenant_id, organization_name, organization_domain FROM users WHERE LOWER(email) = LOWER(?);", (admin_user,))
        caller = cur.fetchone()
        caller_role = caller["role"] if caller else "individual"
        
        if caller_role == "org_admin":
            # Org admin can only create org_user or org_admin within their tenant
            if role not in ["org_admin", "org_user"]:
                role = "org_user"
            tenant_id = caller["tenant_id"]
            org_name = caller["organization_name"]
            org_dom = caller["organization_domain"]
        else:
            valid_roles = ["super_admin", "org_admin", "org_user", "individual"]
            if role not in valid_roles:
                raise HTTPException(status_code=400, detail=f"Invalid role. Options are: {', '.join(valid_roles)}")
            tenant_id = req.tenant_id
            org_name = req.organization_name
            org_dom = req.organization_domain
            if role == "individual":
                tenant_id = None
                org_name = None
                org_dom = None
            else:
                if not tenant_id and org_dom:
                    clean_dom = org_dom.replace(".", "-").lower()
                    tenant_id = f"ORG-{clean_dom}"
                elif not tenant_id and org_name:
                    clean_name = "".join(c for c in org_name if c.isalnum()).lower()
                    tenant_id = f"ORG-{clean_name}"

        try:
            from api.auth import hash_password
        except ImportError:
            from auth import hash_password

        cur.execute("SELECT id FROM users WHERE LOWER(email) = ?;", (email,))
        if cur.fetchone():
            raise HTTPException(status_code=400, detail="User with this email already exists.")
            
        hashed_pwd = hash_password(password)

        cur.execute("""
            INSERT INTO users (email, password_hash, full_name, role, tenant_id, organization_name, organization_domain, is_approved)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1);
        """, (email, hashed_pwd, req.full_name or email.split("@")[0], role, tenant_id, org_name, org_dom))
        conn.commit()
        new_id = cur.lastrowid
        return {
            "success": True,
            "message": f"User '{email}' provisioned successfully.",
            "user_id": new_id,
            "role": role,
            "tenant_id": tenant_id
        }
    finally:
        conn.close()

@app.post("/api/admin/organizations")
def create_organization_by_admin(req: CreateOrgRequest, admin_user: str = Depends(get_current_admin_user)):
    org_name = req.organization_name.strip()
    if not org_name:
        raise HTTPException(status_code=400, detail="Organization name is required.")
        
    domain = (req.organization_domain or "").strip().lower()
    clean_key = domain.replace(".", "-") if domain else "".join(c for c in org_name if c.isalnum()).lower()
    tenant_id = f"ORG-{clean_key}"
    
    conn = get_sqlite_conn()
    try:
        cur = conn.cursor()
        
        # If admin_email provided, assign or create the org_admin
        admin_email = (req.admin_email or "").strip().lower()
        if admin_email:
            try:
                from api.auth import hash_password
            except ImportError:
                from auth import hash_password
                
            cur.execute("SELECT id FROM users WHERE LOWER(email) = ?;", (admin_email,))
            existing_user = cur.fetchone()
            if existing_user:
                cur.execute("""
                    UPDATE users 
                    SET role = 'org_admin', organization_name = ?, organization_domain = ?, tenant_id = ?, is_approved = 1
                    WHERE id = ?;
                """, (org_name, domain or None, tenant_id, existing_user["id"]))
            else:
                pwd = req.admin_password or "OrgAdmin@123"
                hashed_pwd = hash_password(pwd)
                cur.execute("""
                    INSERT INTO users (email, password_hash, full_name, role, tenant_id, organization_name, organization_domain, is_approved)
                    VALUES (?, ?, ?, 'org_admin', ?, ?, ?, 1);
                """, (admin_email, hashed_pwd, req.admin_name or admin_email.split("@")[0], tenant_id, org_name, domain or None))
                
        conn.commit()
        return {
            "success": True,
            "message": f"Organization '{org_name}' created successfully.",
            "tenant_id": tenant_id,
            "organization_name": org_name,
            "organization_domain": domain
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create organization: {e}")
    finally:
        conn.close()

@app.put("/api/admin/users/{user_id}/role")
def update_user_role(user_id: int, req: UpdateRoleRequest, admin_user: str = Depends(get_current_admin_user)):
    valid_roles = ["super_admin", "org_admin", "org_user", "individual"]
    role = req.role.lower().strip()
    if role not in valid_roles:
        raise HTTPException(status_code=400, detail=f"Invalid role. Options are: {', '.join(valid_roles)}")
        
    conn = get_sqlite_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT role, tenant_id, organization_name, organization_domain FROM users WHERE LOWER(email) = LOWER(?);", (admin_user,))
        caller = cur.fetchone()
        caller_role = caller["role"] if caller else "individual"

        cur.execute("SELECT email, role, tenant_id, organization_name FROM users WHERE id = ?;", (user_id,))
        target = cur.fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="User not found.")

        target_role = target["role"]
        
        if caller_role == "org_admin":
            # Org admin can only manage users in their own tenant
            if target["tenant_id"] != caller["tenant_id"] and target["organization_name"] != caller["organization_name"]:
                raise HTTPException(status_code=403, detail="Permission denied to modify users outside your organization.")
            if target_role == "super_admin" or role == "super_admin":
                raise HTTPException(status_code=403, detail="Org admins cannot modify Super Admin permissions.")
            if role not in ["org_admin", "org_user"]:
                role = "org_user"
            tenant_id = caller["tenant_id"]
            org_name = caller["organization_name"]
            org_dom = caller["organization_domain"]
        else:
            tenant_id = req.tenant_id
            org_name = (req.organization_name or "").strip() or None
            org_dom = (req.organization_domain or "").strip() or None
            if org_name or org_dom:
                if not tenant_id and org_dom:
                    clean_dom = org_dom.replace(".", "-").lower()
                    tenant_id = f"ORG-{clean_dom}"
                elif not tenant_id and org_name:
                    clean_name = "".join(c for c in org_name if c.isalnum()).lower()
                    tenant_id = f"ORG-{clean_name}"
                if role == "individual":
                    role = "org_user"
            else:
                if role == "individual":
                    tenant_id = None
                    org_name = None
                    org_dom = None

        sub_status = (req.subscription_status or "").strip().lower() if req.subscription_status else None
        if sub_status:
            cur.execute("""
                UPDATE users 
                SET role = ?, organization_name = ?, organization_domain = ?, tenant_id = ?, subscription_status = ? 
                WHERE id = ?;
            """, (role, org_name, org_dom, tenant_id, sub_status, user_id))
        else:
            cur.execute("""
                UPDATE users 
                SET role = ?, organization_name = ?, organization_domain = ?, tenant_id = ? 
                WHERE id = ?;
            """, (role, org_name, org_dom, tenant_id, user_id))
        conn.commit()
        return {"success": True, "message": "User role, organization & subscription updated successfully."}
    finally:
        conn.close()

@app.put("/api/admin/users/{user_id}/status")
def update_user_status(user_id: int, req: UpdateStatusRequest, admin_user: str = Depends(get_current_admin_user)):
    conn = get_sqlite_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT role, tenant_id, organization_name FROM users WHERE LOWER(email) = LOWER(?);", (admin_user,))
        caller = cur.fetchone()
        caller_role = caller["role"] if caller else "individual"

        cur.execute("SELECT email, role, tenant_id, organization_name FROM users WHERE id = ?;", (user_id,))
        target = cur.fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="User not found.")
            
        if caller_role == "org_admin":
            if target["tenant_id"] != caller["tenant_id"] and target["organization_name"] != caller["organization_name"]:
                raise HTTPException(status_code=403, detail="Permission denied to modify users outside your organization.")
            if target["role"] == "super_admin":
                raise HTTPException(status_code=403, detail="Cannot alter Super Admin status.")

        new_status = 1 if req.is_approved else 0
        cur.execute("UPDATE users SET is_approved = ? WHERE id = ?;", (new_status, user_id))
        conn.commit()
        return {"success": True, "message": "User status updated successfully."}
    finally:
        conn.close()

@app.put("/api/admin/users/{user_id}/subscription")
def update_user_subscription(user_id: int, req: UpdateSubscriptionRequest, admin_user: str = Depends(get_current_admin_user)):
    conn = get_sqlite_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT role, tenant_id, organization_name FROM users WHERE LOWER(email) = LOWER(?);", (admin_user,))
        caller = cur.fetchone()
        caller_role = caller["role"] if caller else "individual"

        cur.execute("SELECT id, email, role, tenant_id, organization_name FROM users WHERE id = ?;", (user_id,))
        target = cur.fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="User not found.")
            
        if caller_role == "org_admin":
            if target["tenant_id"] != caller["tenant_id"] and target["organization_name"] != caller["organization_name"]:
                raise HTTPException(status_code=403, detail="Permission denied to modify users outside your organization.")
            if target["role"] == "super_admin":
                raise HTTPException(status_code=403, detail="Cannot modify Super Admin subscription.")

        valid_statuses = ["purchased", "unpurchased", "subscribed", "unsubscribed"]
        clean_status = req.subscription_status.strip().lower()
        if clean_status not in valid_statuses:
            raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of {valid_statuses}")
            
        cur.execute("UPDATE users SET subscription_status = ? WHERE id = ?;", (clean_status, user_id))
        conn.commit()
        return {"success": True, "message": f"User subscription status updated to {clean_status}.", "subscription_status": clean_status}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update subscription: {e}")
    finally:
        conn.close()

@app.delete("/api/admin/users/{user_id}")
def delete_user(user_id: int, admin_user: str = Depends(get_current_admin_user)):
    conn = get_sqlite_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT role, tenant_id, organization_name FROM users WHERE LOWER(email) = LOWER(?);", (admin_user,))
        caller = cur.fetchone()
        caller_role = caller["role"] if caller else "individual"

        cur.execute("SELECT email, role, tenant_id, organization_name FROM users WHERE id = ?;", (user_id,))
        target = cur.fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="User not found.")
            
        target_email = target["email"].lower()
        if target_email == admin_user.lower():
            raise HTTPException(status_code=400, detail="Cannot delete your own account.")
            
        if caller_role == "org_admin":
            if target["tenant_id"] != caller["tenant_id"] and target["organization_name"] != caller["organization_name"]:
                raise HTTPException(status_code=403, detail="Permission denied to delete users outside your organization.")
            if target["role"] == "super_admin":
                raise HTTPException(status_code=403, detail="Cannot delete Super Admin account.")

        cur.execute("DELETE FROM users WHERE id = ?;", (user_id,))
        cur.execute("DELETE FROM query_logs WHERE user_email = ?;", (target_email,))
        conn.commit()
        return {"success": True, "message": f"User '{target_email}' removed successfully."}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete user: {e}")
    finally:
        conn.close()




@app.get("/db-check")
def db_check(current_user: str = Depends(get_current_user)):
    """Diagnostic endpoint to check PostgreSQL database connection and tables."""
    conn_str = PG_URL
    if not conn_str:
        return {"status": "error", "message": "DATABASE_URL environment variable is not set."}
    
    if conn_str.startswith("postgres://"):
        conn_str = conn_str.replace("postgres://", "postgresql://", 1)
        
    try:
        from urllib.parse import urlparse
        url = urlparse(conn_str)
        conn_kwargs = {
            "user": url.username,
            "password": url.password,
            "host": url.hostname,
            "port": url.port or 5432,
            "database": url.path.lstrip('/'),
            "sslmode": "require"
        }
        conn = psycopg2.connect(**conn_kwargs)
        with conn.cursor() as cur:
            cur.execute("SELECT schema_name FROM information_schema.schemata;")
            schemas = [r[0] for r in cur.fetchall()]
            non_system_schemas = [s for s in schemas if not s.startswith('pg_') and s != 'information_schema']
            
            tables_info = {}
            for t_name in ["lexisearch.court_records", "lexisearch.high_court_records", "lexisearch.supreme_court_records"]:
                try:
                    cur.execute(f"SELECT * FROM {t_name} LIMIT 1")
                    row = cur.fetchone()
                    tables_info[t_name] = f"Exists, Has Data: {row is not None}"
                except Exception as table_err:
                    conn.rollback()
                    tables_info[t_name] = f"Error: {table_err}"
                    
        conn.close()
        return {
            "status": "connected",
            "host": url.hostname,
            "database": url.path.lstrip('/'),
            "schemas": non_system_schemas,
            "tables": tables_info
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Connection failed: {e}"
        }

if __name__ == "__main__":
    import uvicorn
    # Bind to 127.0.0.1 (localhost) for local security instead of 0.0.0.0 (which exposes to all network interfaces)
    # Use import string to support auto-reload
    current_dir = os.path.dirname(os.path.abspath(__file__))
    if current_dir not in sys.path:
        sys.path.insert(0, current_dir)
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
