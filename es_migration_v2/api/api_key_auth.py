import os
import time
import secrets
import hashlib
import sqlite3
import psycopg2
from collections import defaultdict
from typing import Optional, Dict, Any, List
from urllib.parse import urlparse
from fastapi import Request, Response, Header, HTTPException, status
from dotenv import load_dotenv

# Ensure environment variables are loaded
_env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".env")
load_dotenv(dotenv_path=_env_path, override=True)
load_dotenv(override=True)

PG_URL = os.getenv("DATABASE_URL")
SQLITE_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.db")

# In-memory sliding window rate-limiter: key_id -> list of request timestamps
_RATE_LIMIT_BUCKET = defaultdict(list)

# Short-lived in-memory cache for validated API keys (TTL: 60s)
_KEY_CACHE = {}
_KEY_CACHE_TTL = 60.0

def _get_db():
    """Returns a PostgreSQL connection if available, otherwise returns SQLite connection."""
    if PG_URL:
        try:
            url = urlparse(PG_URL)
            conn = psycopg2.connect(
                user=url.username,
                password=url.password,
                host=url.hostname,
                port=url.port or 5432,
                database=url.path.lstrip('/'),
                sslmode="require",
                connect_timeout=3
            )
            return conn, "postgres"
        except Exception as e:
            pass
    conn = sqlite3.connect(SQLITE_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn, "sqlite"

def hash_key(raw_key: str) -> str:
    """Computes SHA-256 hash of the secret key."""
    return hashlib.sha256(raw_key.strip().encode("utf-8")).hexdigest()

def generate_api_key(
    user_email: str,
    key_name: str,
    organization_name: str = "Default Org",
    rate_limit_per_min: int = 60,
    monthly_quota: int = 10000
) -> Dict[str, Any]:
    """
    Generates a secure cryptographically random API Key.
    Stores the SHA-256 hash in the database.
    Returns the raw key (shown only ONCE upon creation).
    """
    random_bytes = secrets.token_hex(24)
    raw_key = f"bls_live_{random_bytes}"
    key_prefix = f"bls_live_{random_bytes[:6]}..."
    hashed = hash_key(raw_key)

    conn, db_type = _get_db()
    try:
        cur = conn.cursor()
        if db_type == "postgres":
            cur.execute("""
                INSERT INTO lexisearch.api_keys (
                    user_email, organization_name, key_name, key_prefix,
                    hashed_key, rate_limit_per_min, monthly_quota, used_count, is_active
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, 0, TRUE)
                RETURNING id, created_at;
            """, (user_email, organization_name, key_name, key_prefix, hashed, rate_limit_per_min, monthly_quota))
            row = cur.fetchone()
            key_id, created_at = row[0], row[1]
            conn.commit()
        else:
            cur.execute("""
                INSERT INTO api_keys (
                    user_email, organization_name, key_name, key_prefix,
                    hashed_key, rate_limit_per_min, monthly_quota, used_count, is_active
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 1);
            """, (user_email, organization_name, key_name, key_prefix, hashed, rate_limit_per_min, monthly_quota))
            key_id = cur.lastrowid
            created_at = time.strftime("%Y-%m-%d %H:%M:%S")
            conn.commit()

        return {
            "id": key_id,
            "raw_key": raw_key,
            "key_prefix": key_prefix,
            "key_name": key_name,
            "organization_name": organization_name,
            "rate_limit_per_min": rate_limit_per_min,
            "monthly_quota": monthly_quota,
            "created_at": str(created_at)
        }
    finally:
        conn.close()

def list_api_keys(user_email: str) -> List[Dict[str, Any]]:
    """Lists all API keys belonging to a user (never returns secret hashes)."""
    conn, db_type = _get_db()
    try:
        cur = conn.cursor()
        if db_type == "postgres":
            cur.execute("""
                SELECT id, key_name, key_prefix, organization_name,
                       rate_limit_per_min, monthly_quota, used_count, is_active,
                       created_at, last_used_at
                FROM lexisearch.api_keys
                WHERE user_email = %s
                ORDER BY created_at DESC;
            """, (user_email,))
            rows = cur.fetchall()
            return [{
                "id": r[0],
                "key_name": r[1],
                "key_prefix": r[2],
                "organization_name": r[3],
                "rate_limit_per_min": r[4],
                "monthly_quota": r[5],
                "used_count": r[6],
                "is_active": r[7],
                "created_at": str(r[8] or ""),
                "last_used_at": str(r[9] or "")
            } for r in rows]
        else:
            cur.execute("""
                SELECT id, key_name, key_prefix, organization_name,
                       rate_limit_per_min, monthly_quota, used_count, is_active,
                       created_at, last_used_at
                FROM api_keys
                WHERE user_email = ?
                ORDER BY created_at DESC;
            """, (user_email,))
            rows = cur.fetchall()
            return [{
                "id": r["id"],
                "key_name": r["key_name"],
                "key_prefix": r["key_prefix"],
                "organization_name": r["organization_name"],
                "rate_limit_per_min": r["rate_limit_per_min"],
                "monthly_quota": r["monthly_quota"],
                "used_count": r["used_count"],
                "is_active": bool(r["is_active"]),
                "created_at": str(r["created_at"] or ""),
                "last_used_at": str(r["last_used_at"] or "")
            } for r in rows]
    finally:
        conn.close()

def revoke_api_key(user_email: str, key_id: int) -> bool:
    """Revokes (deactivates) an API key."""
    conn, db_type = _get_db()
    try:
        cur = conn.cursor()
        if db_type == "postgres":
            cur.execute("""
                UPDATE lexisearch.api_keys
                SET is_active = FALSE
                WHERE id = %s AND user_email = %s;
            """, (key_id, user_email))
            conn.commit()
            updated = cur.rowcount > 0
        else:
            cur.execute("""
                UPDATE api_keys
                SET is_active = 0
                WHERE id = ? AND user_email = ?;
            """, (key_id, user_email))
            conn.commit()
            updated = cur.rowcount > 0

        # Flush in-memory validation cache
        _KEY_CACHE.clear()
        return updated
    finally:
        conn.close()

def validate_and_record_usage(raw_key: str) -> Dict[str, Any]:
    """
    Validates the raw API key, enforces rate limiting, and records usage.
    Returns key metadata. Raises HTTPException on failure.
    """
    if not raw_key or not raw_key.startswith("bls_"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API Key format. Expected 'bls_live_...'"
        )

    hashed = hash_key(raw_key)
    now = time.time()

    # 1. Fast Cache Check
    key_info = None
    if hashed in _KEY_CACHE:
        cached_time, cached_info = _KEY_CACHE[hashed]
        if now - cached_time < _KEY_CACHE_TTL:
            key_info = cached_info

    # 2. Query Database if not cached
    if not key_info:
        conn, db_type = _get_db()
        try:
            cur = conn.cursor()
            if db_type == "postgres":
                cur.execute("""
                    SELECT id, user_email, organization_name, key_name,
                           rate_limit_per_min, monthly_quota, used_count, is_active
                    FROM lexisearch.api_keys
                    WHERE hashed_key = %s;
                """, (hashed,))
                row = cur.fetchone()
                if row:
                    key_info = {
                        "id": row[0],
                        "user_email": row[1],
                        "organization_name": row[2],
                        "key_name": row[3],
                        "rate_limit_per_min": row[4] or 60,
                        "monthly_quota": row[5] or 10000,
                        "used_count": row[6] or 0,
                        "is_active": bool(row[7])
                    }
            else:
                cur.execute("""
                    SELECT id, user_email, organization_name, key_name,
                           rate_limit_per_min, monthly_quota, used_count, is_active
                    FROM api_keys
                    WHERE hashed_key = ?;
                """, (hashed,))
                row = cur.fetchone()
                if row:
                    key_info = {
                        "id": row["id"],
                        "user_email": row["user_email"],
                        "organization_name": row["organization_name"],
                        "key_name": row["key_name"],
                        "rate_limit_per_min": row["rate_limit_per_min"] or 60,
                        "monthly_quota": row["monthly_quota"] or 10000,
                        "used_count": row["used_count"] or 0,
                        "is_active": bool(row["is_active"])
                    }
        finally:
            conn.close()

        if not key_info or not key_info["is_active"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="API Key is invalid, inactive, or has been revoked."
            )

        _KEY_CACHE[hashed] = (now, key_info)

    # 3. Rate Limiting Check (Sliding 60-second window)
    key_id = key_info["id"]
    window = _RATE_LIMIT_BUCKET[key_id]
    cutoff = now - 60.0
    _RATE_LIMIT_BUCKET[key_id] = [t for t in window if t > cutoff]

    limit = key_info["rate_limit_per_min"]
    if len(_RATE_LIMIT_BUCKET[key_id]) >= limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit exceeded. Maximum {limit} requests per minute allowed."
        )

    # Record this request
    _RATE_LIMIT_BUCKET[key_id].append(now)

    # 4. Asynchronously update usage count in DB
    try:
        conn, db_type = _get_db()
        cur = conn.cursor()
        if db_type == "postgres":
            cur.execute("""
                UPDATE lexisearch.api_keys
                SET used_count = used_count + 1, last_used_at = CURRENT_TIMESTAMP
                WHERE id = %s;
            """, (key_id,))
            conn.commit()
        else:
            cur.execute("""
                UPDATE api_keys
                SET used_count = used_count + 1, last_used_at = CURRENT_TIMESTAMP
                WHERE id = ?;
            """, (key_id,))
            conn.commit()
        conn.close()
        key_info["used_count"] += 1
    except Exception:
        pass

    return key_info

async def get_api_key_client(
    request: Request,
    response: Response,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    authorization: Optional[str] = Header(None)
) -> Dict[str, Any]:
    """
    FastAPI dependency for Partner REST API authentication.
    Supports either:
      - Header: `X-API-Key: bls_live_...`
      - Header: `Authorization: Bearer bls_live_...`
    """
    raw_key = None
    if x_api_key:
        raw_key = x_api_key.strip()
    elif authorization and authorization.lower().startswith("bearer bls_"):
        raw_key = authorization[7:].strip()

    if not raw_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Provide your Partner API Key via 'X-API-Key: bls_live_...' or 'Authorization: Bearer bls_live_...'",
            headers={"WWW-Authenticate": "ApiKey"}
        )

    client_info = validate_and_record_usage(raw_key)

    # Inject standard Rate-Limit & Quota headers into response
    limit = client_info["rate_limit_per_min"]
    used_in_window = len(_RATE_LIMIT_BUCKET[client_info["id"]])
    remaining_in_window = max(0, limit - used_in_window)
    quota_left = max(0, client_info["monthly_quota"] - client_info["used_count"])

    response.headers["X-RateLimit-Limit"] = str(limit)
    response.headers["X-RateLimit-Remaining"] = str(remaining_in_window)
    response.headers["X-Quota-Remaining"] = str(quota_left)

    return client_info
