import os
import sys
import psycopg2
import sqlite3
from urllib.parse import urlparse
from dotenv import load_dotenv

# Load environment variables
env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
load_dotenv(dotenv_path=env_path, override=True)
load_dotenv(override=True)

PG_URL = os.getenv("DATABASE_URL")
SQLITE_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "api", "users.db")

def init_postgres_api_keys():
    if not PG_URL:
        print("[!] No DATABASE_URL found in .env, skipping PostgreSQL table creation.")
        return False
    try:
        url = urlparse(PG_URL)
        conn = psycopg2.connect(
            user=url.username,
            password=url.password,
            host=url.hostname,
            port=url.port or 5432,
            database=url.path.lstrip('/'),
            sslmode="require",
            connect_timeout=10
        )
        with conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS lexisearch;")
            cur.execute("""
            CREATE TABLE IF NOT EXISTS lexisearch.api_keys (
                id SERIAL PRIMARY KEY,
                user_email VARCHAR(255) NOT NULL,
                organization_name VARCHAR(255) DEFAULT 'Default Org',
                key_name VARCHAR(128) NOT NULL,
                key_prefix VARCHAR(32) NOT NULL,
                hashed_key VARCHAR(64) UNIQUE NOT NULL,
                rate_limit_per_min INT DEFAULT 60,
                monthly_quota INT DEFAULT 10000,
                used_count INT DEFAULT 0,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_used_at TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON lexisearch.api_keys(hashed_key);
            CREATE INDEX IF NOT EXISTS idx_api_keys_email ON lexisearch.api_keys(user_email);
            """)
            conn.commit()
        conn.close()
        print("[+] PostgreSQL table 'lexisearch.api_keys' is ready!")
        return True
    except Exception as e:
        print(f"[-] Error creating PostgreSQL api_keys table: {e}")
        return False

def init_sqlite_api_keys():
    try:
        conn = sqlite3.connect(SQLITE_DB_PATH)
        cur = conn.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS api_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_email TEXT NOT NULL,
            organization_name TEXT DEFAULT 'Default Org',
            key_name TEXT NOT NULL,
            key_prefix TEXT NOT NULL,
            hashed_key TEXT UNIQUE NOT NULL,
            rate_limit_per_min INTEGER DEFAULT 60,
            monthly_quota INTEGER DEFAULT 10000,
            used_count INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_used_at TIMESTAMP
        );
        """)
        conn.commit()
        conn.close()
        print(f"[+] SQLite table 'api_keys' is ready in {SQLITE_DB_PATH}!")
        return True
    except Exception as e:
        print(f"[-] Error creating SQLite api_keys table: {e}")
        return False

if __name__ == "__main__":
    init_postgres_api_keys()
    init_sqlite_api_keys()
