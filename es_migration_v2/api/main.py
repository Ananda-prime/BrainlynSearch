import os
import io
import json
import difflib
import psycopg2
import time
import sqlite3
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Depends
from fastapi.responses import FileResponse, StreamingResponse, HTMLResponse
from pydantic import BaseModel
from typing import Optional, List
from elasticsearch import Elasticsearch
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm

try:
    from api.query_builder import build_es_query
    from api.cache import generate_cache_key, get_cached_result, set_cached_result
except ImportError:
    from query_builder import build_es_query
    from cache import generate_cache_key, get_cached_result, set_cached_result

# Load environment variables explicitly from src/.env
env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".env")
load_dotenv(dotenv_path=env_path, override=True)

app = FastAPI(title="Brainlynsearch Elasticsearch API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

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

class UpdateStatusRequest(BaseModel):
    is_approved: bool


# SQLite database path for local user storage
SQLITE_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.db")

def get_sqlite_conn():
    conn = sqlite3.connect(SQLITE_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def log_query(user_email: str, query_type: str, state_name: Optional[str] = None, risk_level: str = "low"):
    conn = get_sqlite_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT tenant_id FROM users WHERE email = ?;", (user_email,))
        row = cur.fetchone()
        tenant_id = row["tenant_id"] if row else None
        
        cur.execute(
            """
            INSERT INTO query_logs (user_email, tenant_id, query_type, state_name, risk_level)
            VALUES (?, ?, ?, ?, ?);
            """,
            (user_email, tenant_id, query_type, state_name, risk_level)
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

def get_current_user(token: Optional[str] = Depends(oauth2_scheme)):
    if not token:
        raise HTTPException(
            status_code=401,
            detail="Authentication token is missing",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        from api.auth import decode_access_token
    except ImportError:
        from auth import decode_access_token
    payload = decode_access_token(token)
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

# Calculate the path to index.html relative to this file
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) # es_migration
INDEX_PATH = os.path.join(os.path.dirname(BASE_DIR), "index.html") # src/index.html
DASHBOARD_PATH = os.path.join(os.path.dirname(BASE_DIR), "dashboard.html")
USER_DASHBOARD_PATH = os.path.join(os.path.dirname(BASE_DIR), "user_dashboard.html")

INTERCEPTOR_SCRIPT = """
<script>
// Intercept fetch requests to add Authorization header
const originalFetch = window.fetch;
window.fetch = async function(...args) {
    let [resource, config] = args;
    if (!config) config = {};
    if (!config.headers) config.headers = {};
    
    const token = sessionStorage.getItem("access_token");
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
        sessionStorage.removeItem("access_token");
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
        const token = sessionStorage.getItem("access_token");
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
        } catch (e) {
            sessionStorage.removeItem("access_token");
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
        const token = sessionStorage.getItem("access_token");
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
        } catch (e) {
            sessionStorage.removeItem("access_token");
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
        const token = sessionStorage.getItem("access_token");
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
        } catch (e) {
            sessionStorage.removeItem("access_token");
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

@app.get("/{filename}.pdf")
def serve_pdf(filename: str, current_user: str = Depends(get_current_user)):
    pdf_path = os.path.join(os.path.dirname(BASE_DIR), f"{filename}.pdf")
    if os.path.exists(pdf_path):
        return FileResponse(pdf_path, media_type="application/pdf")
    raise HTTPException(status_code=404, detail="PDF not found")

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
INDEX_PATTERN = "court_records,high_court_records,supreme_court_records"

# Map ES indices back to Postgres tables for hydration
TABLE_MAPPING = {
    "court_records": "lexisearch.court_records",
    "high_court_records": "lexisearch.high_court_records",
    "supreme_court_records": "lexisearch.supreme_court_records"
}

es = Elasticsearch(ES_URL)

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

class SuggestRequest(BaseModel):
    name: str
    field: Optional[str] = "party_name"

@app.post("/search")
def search_records(
    req: SearchRequest, 
    current_user: str = Depends(get_current_user),
    log_query_type: Optional[str] = "interactive_search"
):
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
                log_query(current_user, log_query_type, req.state, "low")
            return cached_data

        es_query = build_es_query(cache_params)
        page_size = getattr(req, 'size', 20) or 20
        from_index = (req.page - 1) * page_size
        
        # Determine if we have a filter active (exclusive Petitioner or Respondent)
        is_petitioner_active = bool(req.petitioner) and not bool(req.respondent)
        is_respondent_active = bool(req.respondent) and not bool(req.petitioner)
        is_filtered_search = is_petitioner_active or is_respondent_active

        es_size = 100 if is_filtered_search else 20
        es_from = 0 if is_filtered_search else from_index

        # 0. Select Target Index
        target_index = INDEX_PATTERN
        if req.court_type == "district":
            target_index = "court_records"
        elif req.court_type == "high":
            target_index = "high_court_records"
        elif req.court_type == "supreme":
            target_index = "supreme_court_records"

        # 1. Multi-Index Search
        es_response = es.search(
            index=target_index,
            ignore_unavailable=True,
            query=es_query,
            from_=es_from,
            size=es_size,
            _source=["cnr", "diary_number", "case_type"],
            track_total_hits=True
        )
        
        all_hits = es_response["hits"]["hits"]
        total_hits = es_response["hits"]["total"]["value"]
        
        if not all_hits:
            return {"success": True, "data": [], "total": 0, "search_time_ms": round((time.time() - start_time) * 1000, 2)}

        max_score = es_response["hits"]["max_score"] or 1.0
        
        # Group IDs by their source index to batch Postgres queries
        grouped_hits = {}
        for hit in all_hits:
            idx = hit["_index"]
            if idx not in grouped_hits: grouped_hits[idx] = []
            grouped_hits[idx].append(hit)

        final_records = []
        conn = get_db_connection()
        if not conn:
            raise HTTPException(
                status_code=500,
                detail="Database connection failed. Please verify that your DATABASE_URL in .env is correct and accessible."
            )
            
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

        # Sort final list based on the new Absolute True String Distance score
        final_records.sort(key=lambda x: x.get("confidence", 0), reverse=True)

        total_filtered = len(final_records)
        if is_filtered_search:
            page_records = final_records[from_index : from_index + page_size]
        else:
            page_records = final_records

        import datetime as dt

        def sanitize_record(rec):
            """Convert non-JSON-serializable values to safe types."""
            safe = {}
            for k, v in rec.items():
                if isinstance(v, (dt.datetime, dt.date)):
                    safe[k] = v.isoformat()
                elif v is None or isinstance(v, (str, int, float, bool)):
                    safe[k] = v
                else:
                    safe[k] = str(v)
            return safe

        page_records = [sanitize_record(r) for r in page_records]

        response_data = {
            "success": True,
            "data": page_records,
            "total": total_filtered if is_filtered_search else total_hits,
            "page": req.page,
            "hasMore": (total_filtered > (from_index + page_size)) if is_filtered_search else (total_hits > (from_index + page_size)),
            "search_time_ms": round((time.time() - start_time) * 1000, 2),
            "cached": False
        }
        
        # Log search query
        if log_query_type:
            max_similarity = 0.0
            if page_records:
                max_similarity = max(r.get("confidence", 0) for r in page_records)
            risk_level = "low"
            if max_similarity >= 95.0:
                risk_level = "high"
            elif max_similarity >= 90.0:
                risk_level = "medium"
                
            log_query(current_user, log_query_type, req.state, risk_level)
        
        set_cached_result(cache_key, response_data)
        return response_data
        
    except HTTPException as he:
        raise he
    except Exception as e:
        print(f"Search Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

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
                _source=["party_name"]
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
        print(f"Suggest Error: {e}")
        return {"success": False, "data": []}


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

    NAME_ALIASES    = ["name", "party name", "entity name", "company name",
                       "applicant", "petitioner", "respondent", "litigant"]
    PAN_ALIASES     = ["pan"]
    ADDRESS_ALIASES = ["addr", "address"]
    REGION_ALIASES  = ["region", "zone", "area"]
    SL_ALIASES      = ["sl", "s.no", "sno", "sr", "serial"]

    def find_col(headers_upper: list, aliases: list) -> Optional[int]:
        for alias in aliases:
            for i, h in enumerate(headers_upper):
                if alias in h:
                    return i
        return None

    try:
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        ws = wb.active

        header_row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), None)
        if header_row is None:
            raise HTTPException(status_code=400, detail="Excel file is empty or has no header row")

        raw_headers    = [str(c).strip() if c is not None else "" for c in header_row]
        headers_upper  = [h.upper() for h in raw_headers]

        name_col    = find_col(headers_upper, [a.upper() for a in NAME_ALIASES])
        pan_col     = find_col(headers_upper, [a.upper() for a in PAN_ALIASES])
        addr_col    = find_col(headers_upper, [a.upper() for a in ADDRESS_ALIASES])
        reg_col     = find_col(headers_upper, [a.upper() for a in REGION_ALIASES])
        sl_col      = find_col(headers_upper, [a.upper() for a in SL_ALIASES])

        if name_col is None:
            detected = ", ".join(f'"{h}"' for h in raw_headers if h)
            raise HTTPException(
                status_code=400,
                detail=f"Could not find a Name column. Detected columns: [{detected}]. "
                       f"Please ensure your Excel has a column named 'Name', 'Company Name', or similar."
            )

        entities = []
        for row in ws.iter_rows(min_row=2, values_only=True):
            if all(c is None for c in row):
                continue
            raw_name = row[name_col] if name_col < len(row) else None
            name = str(raw_name).strip() if raw_name is not None else ""
            if not name or name.lower() in ("none", "nan", "n/a", "-", ""):
                continue

            def safe_get(col):
                if col is None or col >= len(row): return ""
                v = row[col]
                return str(v).strip() if v is not None else ""

            entities.append({
                "sl_no":   safe_get(sl_col)   or str(len(entities) + 1),
                "name":    name,
                "pan":     safe_get(pan_col),
                "address": safe_get(addr_col),
                "region":  safe_get(reg_col),
            })

        wb.close()

        detected_columns = {
            "name":    raw_headers[name_col] if name_col is not None else None,
            "pan":     raw_headers[pan_col]  if pan_col  is not None else None,
            "address": raw_headers[addr_col] if addr_col is not None else None,
            "region":  raw_headers[reg_col]  if reg_col  is not None else None,
            "sl_no":   raw_headers[sl_col]   if sl_col   is not None else None,
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to parse Excel file: {e}")

    if not entities:
        raise HTTPException(status_code=400, detail="No valid names found in Excel.")

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
    overall_max_similarity = 0.0
    for entity in entities:
        name = entity["name"]
        
        # Call Interactive Search internally (pass current_user and set log_query_type=None)
        req = SearchRequest(name=name, court_type=court_type, page=1, size=50)
        search_res = search_records(req, current_user=current_user, log_query_type=None)
        
        matches = []
        max_entity_sim = 0.0
        state_name = None
        
        if search_res.get("success") and search_res.get("data"):
            for rec in search_res["data"]:
                confidence = rec.get("confidence", 0)
                if confidence > max_entity_sim:
                    max_entity_sim = confidence
                    state_name = rec.get("state_name")
                if confidence >= (threshold * 100):
                    matches.append({
                        "id":                rec.get("cnr") or rec.get("diary_number") or str(rec.get("id")),
                        "cnr":               rec.get("cnr") or rec.get("diary_number") or "",
                        "party_name":        rec.get("party_name", ""),
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
        
        # Track overall batch max similarity
        if max_entity_sim > overall_max_similarity:
            overall_max_similarity = max_entity_sim
            
        entity_risk = "low"
        if max_entity_sim >= 95.0:
            entity_risk = "high"
        elif max_entity_sim >= 90.0:
            entity_risk = "medium"
            
        # Log this specific query under screener_query
        log_query(current_user, "screener_query", state_name, entity_risk)
        
        results.append({
            "sl_no":        entity["sl_no"],
            "query_name":   name,
            "pan":          entity["pan"],
            "address":      entity["address"],
            "region":       entity["region"],
            "match_count":  len(matches),
            "matches":      matches,
        })
        
    # Log the complete batch upload summary
    overall_risk = "low"
    if overall_max_similarity >= 95.0:
        overall_risk = "high"
    elif overall_max_similarity >= 90.0:
        overall_risk = "medium"
    log_query(current_user, "screener_upload", None, overall_risk)

    return {
        "success":          True,
        "file_name":        file.filename,
        "total_entities":   len(entities),
        "total_matched":    sum(1 for r in results if r["match_count"] > 0),
        "results":          results,
        "court_type":       court_type,
        "threshold_pct":    round(threshold * 100, 1),
        "detected_columns": detected_columns,
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
    info_rows = [
        ("Searched Name", req.query_name),
        ("PAN Number",    req.pan or "N/A"),
        ("Address",       req.address or "N/A"),
        ("Region",        req.region or "N/A"),
        ("Total Matches", str(len(req.matches))),
    ]
    for i, (label, value) in enumerate(info_rows, start=2):
        ws[f"A{i}"] = label
        ws[f"B{i}"] = value
        cell_style(ws[f"A{i}"], bold=True, fg="A78BFA", bg=LIGHT_GREY)
        cell_style(ws[f"B{i}"], fg=WHITE, bg=DARK_BG)
        ws.merge_cells(f"B{i}:H{i}")
    ws.row_dimensions[6].height = 4  # spacer

    # ── Match table header ──
    header_row = 8
    headers = ["#", "Party Name (DB)", "CNR", "Similarity %",
               "Registration Date", "State", "District", "Court Name"]
    col_widths = [4, 38, 22, 14, 18, 16, 18, 22]

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

        values = [
            r_idx,
            match.get("party_name", ""),
            match.get("cnr", ""),
            f"{sim}%",
            match.get("registration_date", ""),
            match.get("state_name", ""),
            match.get("district_name", ""),
            cType,
        ]
        for col_idx, val in enumerate(values, start=1):
            cell = ws.cell(row=row, column=col_idx, value=val)
            fg = sim_color if col_idx == 4 else WHITE
            cell_style(cell, fg=fg, bg=row_bg, align="center" if col_idx in (1, 4, 5) else "left", wrap=(col_idx == 2))
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

    ws.merge_cells("A1:I1")
    ws["A1"] = "BRAINLYNSEARCH AI — Complete Batch Screening Summary"
    cell_style(ws["A1"], bold=True, fg=WHITE, bg=PURPLE, size=14, align="center")
    ws.row_dimensions[1].height = 32

    header_row = 3
    headers = ["#", "Searched Name", "Party Name (DB)", "CNR", "Similarity %",
               "Registration Date", "State", "District", "Court Name"]
    col_widths = [5, 30, 38, 22, 14, 18, 16, 18, 22]

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
                r_idx, searched_name, "No Match Found", "—", "—", "—", "—", "—", "—"
            ]
            for col_idx, val in enumerate(values, start=1):
                cell = ws.cell(row=current_row, column=col_idx, value=val)
                cell_style(cell, fg=WHITE, bg=row_bg, align="center" if col_idx in (1, 4, 5) else "left", wrap=True)
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

                values = [
                    r_idx if m_idx == 0 else "",  # Only show index on first match
                    searched_name if m_idx == 0 else "", # Only show searched name on first match
                    match.get("party_name", ""),
                    match.get("cnr", ""),
                    f"{sim}%",
                    match.get("registration_date", ""),
                    match.get("state_name", ""),
                    match.get("district_name", ""),
                    cType,
                ]
                for col_idx, val in enumerate(values, start=1):
                    cell = ws.cell(row=current_row, column=col_idx, value=val)
                    fg = sim_color if col_idx == 5 else WHITE
                    cell_style(cell, fg=fg, bg=row_bg, align="center" if col_idx in (1, 4, 5) else "left", wrap=(col_idx in (2, 3)))
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

@app.post("/api/stats/log_report")
def log_report(current_user: str = Depends(get_current_user)):
    conn = get_sqlite_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT tenant_id FROM users WHERE email = ?;", (current_user,))
        user_row = cur.fetchone()
        tenant_id = user_row["tenant_id"] if user_row else None
        
        cur.execute(
            "INSERT INTO query_logs (user_email, tenant_id, query_type) VALUES (?, ?, ?);",
            (current_user, tenant_id, "report_generated")
        )
        conn.commit()
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()

@app.post("/api/stats/log_download")
def log_download(current_user: str = Depends(get_current_user)):
    conn = get_sqlite_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT tenant_id FROM users WHERE email = ?;", (current_user,))
        user_row = cur.fetchone()
        tenant_id = user_row["tenant_id"] if user_row else None
        
        cur.execute(
            "INSERT INTO query_logs (user_email, tenant_id, query_type) VALUES (?, ?, ?);",
            (current_user, tenant_id, "report_downloaded")
        )
        conn.commit()
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()

def get_current_super_admin(current_user: str = Depends(get_current_user)):
    conn = get_sqlite_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT role FROM users WHERE email = ?;", (current_user,))
        row = cur.fetchone()
        if not row or row["role"] != "super_admin":
            raise HTTPException(status_code=403, detail="Access denied. Super Admin authorization required.")
        return current_user
    finally:
        conn.close()

@app.get("/api/me")
def get_me(current_user: str = Depends(get_current_user)):
    conn = get_sqlite_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, email, full_name, is_approved, role, tenant_id, organization_name, organization_domain, created_at FROM users WHERE email = ?;",
            (current_user,)
        )
        user = cur.fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="User profile not found.")
        return {
            "success": True,
            "user": {
                "id": user["id"],
                "email": user["email"],
                "full_name": user["full_name"],
                "role": user["role"] or "individual",
                "tenant_id": user["tenant_id"],
                "organization_name": user["organization_name"],
                "organization_domain": user["organization_domain"],
                "is_approved": bool(user["is_approved"]),
                "created_at": user["created_at"]
            }
        }
    finally:
        conn.close()

@app.get("/api/stats/usage")
def get_usage_stats(current_user: str = Depends(get_current_user)):
    conn = get_sqlite_conn()
    try:
        cur = conn.cursor()
        
        # Get user details
        cur.execute("SELECT role, tenant_id, organization_name FROM users WHERE email = ?;", (current_user,))
        user_row = cur.fetchone()
        if not user_row:
            raise HTTPException(status_code=404, detail="User not found.")
            
        role = user_row["role"] or "individual"
        tenant_id = user_row["tenant_id"]
        org_name = user_row["organization_name"]
        
        is_org_admin = (role == "org_admin")
        is_super_admin = (role == "super_admin")
        is_org = bool(tenant_id) and (is_org_admin or is_super_admin)
        
        # Personal Base filters
        user_filter = "user_email = ?"
        user_param = (current_user,)
        
        org_filter = "tenant_id = ?"
        org_param = (tenant_id,)
        
        # Retrieve personal metrics
        cur.execute(
            f"SELECT COUNT(*) FROM query_logs WHERE {user_filter} AND query_type IN ('interactive_search', 'screener_query');",
            user_param
        )
        total_searches = cur.fetchone()[0]
        
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
            f"SELECT COUNT(*) FROM query_logs WHERE {user_filter} AND query_type IN ('interactive_search', 'screener_query') AND risk_level = 'high';",
            user_param
        )
        high_risk_found = cur.fetchone()[0]
        
        # Organization Metrics (only for org_admin or super_admin with a tenant_id)
        org_searches = 0
        org_reports = 0
        org_downloads = 0
        org_high_risk = 0
        member_usage = []
        
        if is_org:
            cur.execute(
                f"SELECT COUNT(*) FROM query_logs WHERE {org_filter} AND query_type IN ('interactive_search', 'screener_query');",
                org_param
            )
            org_searches = cur.fetchone()[0]
            
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
                f"SELECT COUNT(*) FROM query_logs WHERE {org_filter} AND query_type IN ('interactive_search', 'screener_query') AND risk_level = 'high';",
                org_param
            )
            org_high_risk = cur.fetchone()[0]
            
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
                
                member_usage.append({
                    "email": m["email"],
                    "name": m["full_name"],
                    "role": m["role"] or "org_user",
                    "searches": m_searches,
                    "joined_at": m["created_at"]
                })

        # Super Admin Platform-Wide Metrics
        system_metrics = None
        if is_super_admin:
            cur.execute("SELECT COUNT(*) FROM users;")
            total_users_cnt = cur.fetchone()[0]
            
            cur.execute("SELECT COUNT(DISTINCT tenant_id) FROM users WHERE tenant_id IS NOT NULL AND tenant_id != '';")
            active_orgs_cnt = cur.fetchone()[0]
            
            cur.execute("SELECT COUNT(*) FROM users WHERE is_approved = 0;")
            pending_approvals_cnt = cur.fetchone()[0]
            
            cur.execute("SELECT COUNT(*) FROM query_logs WHERE query_type IN ('interactive_search', 'screener_query');")
            total_system_searches = cur.fetchone()[0]
            
            cur.execute("SELECT COUNT(*) FROM query_logs WHERE query_type IN ('interactive_search', 'screener_query') AND risk_level = 'high';")
            total_system_high_risk = cur.fetchone()[0]

            system_metrics = {
                "total_users": total_users_cnt,
                "active_orgs": active_orgs_cnt,
                "pending_approvals": pending_approvals_cnt,
                "total_searches": total_system_searches,
                "total_high_risk": total_system_high_risk
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
            WHERE {user_filter} AND query_type IN ('interactive_search', 'screener_query')
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
            WHERE {user_filter} AND query_type IN ('interactive_search', 'screener_query')
            GROUP BY query_type;
            """,
            user_param
        )
        case_types = {"interactive": 0, "batch": 0}
        for row in cur.fetchall():
            if row["query_type"] == "interactive_search":
                case_types["interactive"] = row["cnt"]
            elif row["query_type"] == "screener_query":
                case_types["batch"] = row["cnt"]
                
        # Risk Trend
        risk_trend = {d.strftime("%Y-%m-%d"): {"high": 0, "medium": 0, "low": 0} for d in dates_list}
        cur.execute(
            f"""
            SELECT date(created_at) as log_date, risk_level, COUNT(*) as cnt 
            FROM query_logs 
            WHERE {user_filter} AND query_type IN ('interactive_search', 'screener_query')
            GROUP BY log_date, risk_level;
            """,
            user_param
        )
        for row in cur.fetchall():
            d_str = row["log_date"]
            r_lvl = row["risk_level"]
            if d_str in risk_trend:
                risk_trend[d_str][r_lvl] = row["cnt"]
                
        # Top States
        cur.execute(
            f"""
            SELECT state_name, COUNT(*) as cnt 
            FROM query_logs 
            WHERE {user_filter} AND state_name IS NOT NULL AND state_name != ''
            GROUP BY state_name 
            ORDER BY cnt DESC 
            LIMIT 5;
            """,
            user_param
        )
        top_states = [{ "state": row["state_name"], "count": row["cnt"] } for row in cur.fetchall()]
        
        return {
            "success": True,
            "role": role,
            "tenant_id": tenant_id,
            "org_name": org_name,
            "metrics": {
                "total_searches": total_searches,
                "reports_generated": reports_generated,
                "reports_downloaded": reports_downloaded,
                "high_risk_found": high_risk_found
            },
            "org_metrics": {
                "total_searches": org_searches,
                "reports_generated": org_reports,
                "reports_downloaded": org_downloads,
                "high_risk_found": org_high_risk
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

# ─────────────────────────────────────────────────────────────
#  SUPER ADMIN USER MANAGEMENT ENDPOINTS
# ─────────────────────────────────────────────────────────────

@app.get("/api/admin/users")
def get_all_users(admin_user: str = Depends(get_current_super_admin)):
    conn = get_sqlite_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, email, full_name, role, tenant_id, organization_name, organization_domain, is_approved, created_at
            FROM users
            ORDER BY created_at DESC;
        """)
        rows = cur.fetchall()
        users = [dict(r) for r in rows]
        
        for u in users:
            email = u["email"]
            cur.execute(
                "SELECT COUNT(*) FROM query_logs WHERE user_email = ? AND query_type IN ('interactive_search', 'screener_query');",
                (email,)
            )
            u["total_searches"] = cur.fetchone()[0]
            
            cur.execute(
                "SELECT COUNT(*) FROM query_logs WHERE user_email = ? AND query_type IN ('report_generated', 'screener_upload');",
                (email,)
            )
            u["total_reports"] = cur.fetchone()[0]
            u["is_approved"] = bool(u["is_approved"])
            u["role"] = u["role"] or "individual"

        return {
            "success": True,
            "total": len(users),
            "users": users
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch users: {e}")
    finally:
        conn.close()

@app.put("/api/admin/users/{user_id}/role")
def update_user_role(user_id: int, req: UpdateRoleRequest, admin_user: str = Depends(get_current_super_admin)):
    valid_roles = ["super_admin", "org_admin", "org_user", "individual"]
    role = req.role.lower().strip()
    if role not in valid_roles:
        raise HTTPException(status_code=400, detail=f"Invalid role. Options are: {', '.join(valid_roles)}")
        
    conn = get_sqlite_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT email FROM users WHERE id = ?;", (user_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="User not found.")
            
        cur.execute("UPDATE users SET role = ? WHERE id = ?;", (role, user_id))
        conn.commit()
        return {"success": True, "message": f"User role updated to '{role}'."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update role: {e}")
    finally:
        conn.close()

@app.put("/api/admin/users/{user_id}/status")
def update_user_status(user_id: int, req: UpdateStatusRequest, admin_user: str = Depends(get_current_super_admin)):
    conn = get_sqlite_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT email FROM users WHERE id = ?;", (user_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="User not found.")
            
        new_status = 1 if req.is_approved else 0
        cur.execute("UPDATE users SET is_approved = ? WHERE id = ?;", (new_status, user_id))
        conn.commit()
        return {"success": True, "message": "User status updated successfully."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update status: {e}")
    finally:
        conn.close()

@app.delete("/api/admin/users/{user_id}")
def delete_user(user_id: int, admin_user: str = Depends(get_current_super_admin)):
    conn = get_sqlite_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT email FROM users WHERE id = ?;", (user_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="User not found.")
            
        target_email = row["email"].lower()
        if target_email == admin_user.lower():
            raise HTTPException(status_code=400, detail="Cannot delete your own super admin account.")
            
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
    uvicorn.run(app, host="0.0.0.0", port=8000)
