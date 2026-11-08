import os
import psycopg2
from dotenv import load_dotenv

# Ensure root .env is loaded
env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
load_dotenv(dotenv_path=env_path, override=True)
load_dotenv(override=True)

PG_URL = os.getenv("DATABASE_URL")

def get_pg_connection():
    if not PG_URL:
        print("[-] DATABASE_URL is not set.")
        return None
    conn_str = PG_URL
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
        return psycopg2.connect(**conn_kwargs)
    except Exception as e:
        print(f"[-] Database connection failed: {e}")
        return None

def create_case_reports_table():
    print(">>> Creating PostgreSQL table 'lexisearch.case_reports'...", flush=True)
    conn = get_pg_connection()
    if not conn:
        print("[-] Could not connect to PostgreSQL.", flush=True)
        return False

    create_sql = """
    CREATE SCHEMA IF NOT EXISTS lexisearch;

    CREATE TABLE IF NOT EXISTS lexisearch.case_reports (
        id SERIAL PRIMARY KEY,
        cnr VARCHAR(64) UNIQUE NOT NULL,
        case_number VARCHAR(128),
        party_name TEXT,
        petitioner TEXT,
        respondent TEXT,
        case_type VARCHAR(64),
        case_status VARCHAR(64),
        court_name TEXT,
        court_code VARCHAR(32),
        state_name VARCHAR(64),
        district_name VARCHAR(128),
        registration_date DATE,
        filing_date DATE,
        next_hearing_date DATE,
        decision_date DATE,
        first_hearing_date DATE,
        judges TEXT[],
        petitioner_advocates TEXT[],
        respondent_advocates TEXT[],
        acts_sections JSONB DEFAULT '[]'::jsonb,
        hearing_history JSONB DEFAULT '[]'::jsonb,
        orders JSONB DEFAULT '[]'::jsonb,
        files JSONB DEFAULT '[]'::jsonb,
        ai_analysis JSONB DEFAULT '{}'::jsonb,
        raw_report JSONB NOT NULL,
        source_api VARCHAR(64) DEFAULT 'ecourts_partner_case_api',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE INDEX IF NOT EXISTS idx_case_reports_cnr ON lexisearch.case_reports(cnr);
    CREATE INDEX IF NOT EXISTS idx_case_reports_updated_at ON lexisearch.case_reports(updated_at);
    """
    try:
        with conn.cursor() as cur:
            cur.execute(create_sql)
            conn.commit()
        print("[+] PostgreSQL table 'lexisearch.case_reports' ready!", flush=True)
        return True
    except Exception as e:
        print(f"[-] Error creating table: {e}", flush=True)
        conn.rollback()
        return False
    finally:
        conn.close()

if __name__ == "__main__":
    create_case_reports_table()
