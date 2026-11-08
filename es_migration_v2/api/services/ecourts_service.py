import os
import time
import threading
from typing import List, Dict, Any, Optional
import httpx
from dotenv import load_dotenv

# Ensure root .env is explicitly loaded
_env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))), ".env")
load_dotenv(dotenv_path=_env_path, override=True)
load_dotenv(override=True)

# ──────────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────────
ECOURTS_API_BASE_URL = os.getenv("ECOURTS_API_BASE_URL", "https://webapi.ecourtsindia.com/api/partner/search")
ECOURTS_API_TOKEN = os.getenv("ECOURTS_API_TOKEN", "").strip()
LIVE_SYNC_ENABLED = os.getenv("ECOURTS_LIVE_SYNC_ENABLED", "true").lower() in ("true", "1", "yes")

# ──────────────────────────────────────────────────────────────────────────────
# Thread-Safe In-Memory Cache (TTL-based with bounded eviction)
# Fix for Gap 5: All cache operations are guarded by _cache_lock
# ──────────────────────────────────────────────────────────────────────────────
_cache_lock = threading.Lock()
_ECOURTS_CACHE: Dict[str, tuple] = {}
CACHE_TTL_SECONDS = 3600  # 1 hour
CACHE_MAX_ENTRIES = 5000

# ──────────────────────────────────────────────────────────────────────────────
# Rate Limiter: cap concurrent outbound API calls to prevent eCourts flooding
# ──────────────────────────────────────────────────────────────────────────────
_API_SEMAPHORE = threading.Semaphore(10)

# ──────────────────────────────────────────────────────────────────────────────
# Shared HTTP Client with TCP connection pooling (reused across all requests)
# Fix for Gap 6: Single client with keep-alive connections instead of per-request
# ──────────────────────────────────────────────────────────────────────────────
_http_client: Optional[httpx.Client] = None
_client_lock = threading.Lock()


def _get_http_client() -> httpx.Client:
    """Lazily initialize and return a shared, connection-pooled HTTP client."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        with _client_lock:
            # Double-checked locking: re-verify inside lock to prevent duplicate init
            if _http_client is None or _http_client.is_closed:
                _http_client = httpx.Client(
                    timeout=3.0,
                    follow_redirects=True,
                    limits=httpx.Limits(
                        max_connections=20,
                        max_keepalive_connections=10
                    )
                )
    return _http_client


def _get_from_cache(cache_key: str) -> Optional[List[Dict[str, Any]]]:
    """Thread-safe cache lookup with TTL validation."""
    with _cache_lock:
        entry = _ECOURTS_CACHE.get(cache_key)
        if entry is not None:
            cached_time, data = entry
            if time.time() - cached_time < CACHE_TTL_SECONDS:
                return data
            del _ECOURTS_CACHE[cache_key]
    return None


def _set_in_cache(cache_key: str, data: List[Dict[str, Any]]) -> None:
    """Thread-safe cache insertion with bounded LRU-style eviction."""
    with _cache_lock:
        # Evict oldest entry if at capacity
        if len(_ECOURTS_CACHE) >= CACHE_MAX_ENTRIES:
            oldest_key = min(_ECOURTS_CACHE, key=lambda k: _ECOURTS_CACHE[k][0])
            del _ECOURTS_CACHE[oldest_key]
        _ECOURTS_CACHE[cache_key] = (time.time(), data)


def _fetch_from_db_live_store(clean_name: str) -> List[Dict[str, Any]]:
    """
    Fallback to PostgreSQL lexisearch.ecourts_live_records
    whenever external webapi.ecourtsindia.com is slow, timing out, or unreachable.
    """
    if not clean_name or len(clean_name.strip()) < 2:
        return []
    try:
        from urllib.parse import urlparse
        import psycopg2
        pg_url = os.getenv("DATABASE_URL")
        if not pg_url:
            return []
        url = urlparse(pg_url)
        conn = psycopg2.connect(
            user=url.username,
            password=url.password,
            host=url.hostname,
            port=url.port or 5432,
            database=url.path.lstrip('/'),
            sslmode="require",
            connect_timeout=3
        )
        term = f"%{clean_name.strip()}%"
        with conn.cursor() as cur:
            cur.execute("""
                SELECT raw_payload
                FROM lexisearch.ecourts_live_records
                WHERE party_name ILIKE %s 
                   OR petitioner ILIKE %s 
                   OR respondent ILIKE %s 
                   OR cnr ILIKE %s
                ORDER BY created_at DESC
                LIMIT 50;
            """, (term, term, term, clean_name.strip()))
            rows = cur.fetchall()
            conn.close()
            results = [r[0] for r in rows if r and r[0]]
            if results:
                print(f"[eCourts DB Store] Recovered {len(results)} live records from PostgreSQL lexisearch.ecourts_live_records for '{clean_name}'")
            return results
    except Exception as db_err:
        print(f"[eCourts DB Store Warning] {db_err}")
        return []


def fetch_ecourts_records(
    query_name: str,
    is_petitioner: bool = False,
    is_respondent: bool = False,
    timeout_seconds: float = 2.5
) -> List[Dict[str, Any]]:
    """
    Queries the eCourts partner API with production-grade resilience:
      - Connection-pooled HTTP client (reuses TCP/TLS connections)
      - Thread-safe TTL cache (1 hour, bounded at 5000 entries)
      - Rate limiter (max 10 concurrent API calls via Semaphore)
      - Circuit breaker timeout (configurable, default 2.5s)
      - Automatic fallback to PostgreSQL lexisearch.ecourts_live_records

    Routes to petitioners, respondents, or common query based on search flags.
    Returns matching live records (never crashes).
    """
    if not LIVE_SYNC_ENABLED:
        return []

    clean_name = query_name.strip() if query_name else ""
    if len(clean_name) < 2:
        return []

    # 1. Determine search param based on active search mode
    if is_petitioner and not is_respondent:
        param_key = "petitioners"
    elif is_respondent and not is_petitioner:
        param_key = "respondents"
    else:
        param_key = "query"

    cache_key = f"{param_key}:{clean_name.lower()}"

    # 2. Check thread-safe cache first (avoids unnecessary API calls)
    cached_hits = _get_from_cache(cache_key)
    if cached_hits is not None:
        print(f"[eCourts Cache] Cache HIT for '{clean_name}' ({len(cached_hits)} live records in memory)")
        return cached_hits

    # 3. Build request headers
    api_token = (os.getenv("ECOURTS_API_TOKEN") or ECOURTS_API_TOKEN or "").strip()
    headers = {
        "Accept": "application/json",
        "User-Agent": "BrainlynSearch-LegalEngine/1.0"
    }
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"

    params = {param_key: clean_name}

    # 4. Acquire rate limiter slot (non-blocking wait with 1s patience)
    acquired = _API_SEMAPHORE.acquire(timeout=1.0)
    if not acquired:
        print(f"[eCourts Rate Limit] Too many concurrent requests. Falling back to DB store for: '{clean_name}'")
        return _fetch_from_db_live_store(clean_name)

    try:
        print(f"[eCourts API] Querying live API: {ECOURTS_API_BASE_URL}?{param_key}={clean_name}")
        client = _get_http_client()
        resp = client.get(
            ECOURTS_API_BASE_URL,
            params=params,
            headers=headers,
            timeout=timeout_seconds
        )

        if resp.status_code == 200:
            body = resp.json()
            results = []

            # Handle multiple possible API response envelope structures
            if isinstance(body, dict):
                data_obj = body.get("data", {})
                if isinstance(data_obj, dict):
                    results = data_obj.get("results", [])
                elif isinstance(data_obj, list):
                    results = data_obj
                elif "results" in body and isinstance(body["results"], list):
                    results = body["results"]

            if not results:
                results = _fetch_from_db_live_store(clean_name)

            print(f"[eCourts API] HTTP 200 OK - Received {len(results)} live records for '{clean_name}'")
            for idx, r_item in enumerate(results[:3]):
                p_str = ", ".join(r_item.get("petitioners", []) or []) or "N/A"
                r_str = ", ".join(r_item.get("respondents", []) or []) or "N/A"
                print(f"  -> [Live #{idx+1}] CNR: {r_item.get('cnr') or r_item.get('id')} | {p_str[:30]} vs {r_str[:30]} | Case: {r_item.get('caseType')} | Status: {r_item.get('caseStatus')}")
            if len(results) > 3:
                print(f"  -> ... and {len(results) - 3} more records.")

            if results:
                _set_in_cache(cache_key, results)

            return results
        else:
            print(f"[eCourts API Warning] HTTP {resp.status_code} for '{clean_name}'. Falling back to DB store.")
            db_res = _fetch_from_db_live_store(clean_name)
            if db_res:
                _set_in_cache(cache_key, db_res)
            return db_res

    except httpx.TimeoutException:
        print(f"[eCourts Circuit-Breaker] Timeout (>{timeout_seconds}s) for: '{clean_name}'. Falling back to DB store.")
        db_res = _fetch_from_db_live_store(clean_name)
        if db_res:
            _set_in_cache(cache_key, db_res)
        return db_res
    except Exception as e:
        print(f"[eCourts API Error] {e}. Falling back to DB store.")
        db_res = _fetch_from_db_live_store(clean_name)
        if db_res:
            _set_in_cache(cache_key, db_res)
        return db_res
    finally:
        _API_SEMAPHORE.release()


# Backward-compatible alias (referenced by main.py and test imports)
sync_fetch_ecourts = fetch_ecourts_records

ECOURTS_CASE_API_BASE_URL = os.getenv("ECOURTS_CASE_API_BASE_URL", "https://webapi.ecourtsindia.com/api/partner/case")

def fetch_case_report_by_cnr(cnr: str, timeout_seconds: float = 12.0) -> Optional[Dict[str, Any]]:
    """
    Fetches the comprehensive case report/docket from the eCourts Partner Case API by CNR.
    Endpoint: GET https://webapi.ecourtsindia.com/api/partner/case/{cnr}
    """
    clean_cnr = (cnr or "").strip().upper()
    if not clean_cnr or len(clean_cnr) < 5:
        return None

    api_token = (os.getenv("ECOURTS_API_TOKEN") or ECOURTS_API_TOKEN or "").strip()
    headers = {
        "Accept": "application/json",
        "User-Agent": "BrainlynSearch-LegalEngine/1.0"
    }
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"

    url = f"{ECOURTS_CASE_API_BASE_URL.rstrip('/')}/{clean_cnr}"

    acquired = _API_SEMAPHORE.acquire(timeout=2.0)
    if not acquired:
        print(f"[eCourts Case API] Rate limit hit. Semaphore saturated for CNR: {clean_cnr}")
        return None

    try:
        print(f"[eCourts Case API] Requesting full case docket for CNR: {clean_cnr} -> {url}")
        client = _get_http_client()
        resp = client.get(url, headers=headers, timeout=timeout_seconds)

        if resp.status_code == 200:
            body = resp.json()
            print(f"[eCourts Case API] HTTP 200 OK for CNR: {clean_cnr}")
            return body
        elif resp.status_code == 404:
            print(f"[eCourts Case API] HTTP 404 Not Found for CNR: {clean_cnr}")
            return {"error": "not_found", "status_code": 404, "message": f"Case record not found on live eCourts for CNR: {clean_cnr}"}
        else:
            print(f"[eCourts Case API] HTTP {resp.status_code} for CNR: {clean_cnr}: {resp.text[:200]}")
            return {"error": "api_error", "status_code": resp.status_code, "message": resp.text[:200]}
    except httpx.TimeoutException:
        print(f"[eCourts Case API] Timeout (>{timeout_seconds}s) for CNR: {clean_cnr}")
        return {"error": "timeout", "message": f"eCourts API timed out after {timeout_seconds}s"}
    except Exception as e:
        print(f"[eCourts Case API Error] {e}")
        return {"error": "exception", "message": str(e)}
    finally:
        _API_SEMAPHORE.release()

