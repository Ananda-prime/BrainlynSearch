import os
import sys
import psycopg2
from urllib.parse import urlparse
from elasticsearch import Elasticsearch
from dotenv import load_dotenv

# Load environment variables explicitly
env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
load_dotenv(dotenv_path=env_path, override=True)

PG_URL = os.getenv("DATABASE_URL")
ES_URL = os.getenv("ES_URL", "http://localhost:9201")

def get_pg_connection():
    if not PG_URL:
        print("[ERROR] DATABASE_URL not found in .env", flush=True)
        return None
    try:
        url = urlparse(PG_URL)
        conn_kwargs = {
            "user": url.username,
            "password": url.password,
            "host": url.hostname,
            "port": url.port or 5432,
            "database": url.path.lstrip('/'),
            "sslmode": "require",
            "connect_timeout": 10
        }
        print(f"Connecting to Postgres at '{url.hostname}' (db: '{url.path.lstrip('/')}')...", flush=True)
        return psycopg2.connect(**conn_kwargs)
    except Exception as e:
        print(f"[ERROR] PostgreSQL connection failed: {e}", flush=True)
        return None

def create_pg_table():
    print(">>> Setting up PostgreSQL table 'lexisearch.ecourts_live_records'...", flush=True)
    conn = get_pg_connection()
    if not conn:
        print("[-] Skipping PostgreSQL table creation due to connection failure.", flush=True)
        return False
    
    create_sql = """
    CREATE SCHEMA IF NOT EXISTS lexisearch;

    CREATE TABLE IF NOT EXISTS lexisearch.ecourts_live_records (
        id SERIAL PRIMARY KEY,
        cnr VARCHAR(64) UNIQUE NOT NULL,
        party_name TEXT NOT NULL,
        petitioner TEXT,
        respondent TEXT,
        case_type VARCHAR(64),
        case_number VARCHAR(128),
        filing_number VARCHAR(128),
        case_status VARCHAR(64),
        state_code VARCHAR(10),
        state_name VARCHAR(64),
        district_code INT,
        district_name VARCHAR(128),
        court_code VARCHAR(32),
        court_name TEXT,
        court_type VARCHAR(32) DEFAULT 'district',
        registration_date DATE,
        filing_date DATE,
        next_hearing_date DATE,
        decision_date DATE,
        judges TEXT[],
        petitioner_advocates TEXT[],
        respondent_advocates TEXT[],
        case_category TEXT,
        order_count INT DEFAULT 0,
        interim_order_count INT DEFAULT 0,
        judgment_count INT DEFAULT 0,
        hearing_count INT DEFAULT 0,
        has_orders BOOLEAN DEFAULT FALSE,
        has_judgments BOOLEAN DEFAULT FALSE,
        raw_payload JSONB,
        source_table VARCHAR(64) DEFAULT 'ecourts_live_api',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE INDEX IF NOT EXISTS idx_ecourts_live_cnr ON lexisearch.ecourts_live_records(cnr);
    CREATE INDEX IF NOT EXISTS idx_ecourts_live_state ON lexisearch.ecourts_live_records(state_name);
    CREATE INDEX IF NOT EXISTS idx_ecourts_live_case_type ON lexisearch.ecourts_live_records(case_type);
    """
    try:
        with conn.cursor() as cur:
            cur.execute(create_sql)
            conn.commit()
        print("[+] PostgreSQL table 'lexisearch.ecourts_live_records' ready!", flush=True)
        return True
    except Exception as e:
        print(f"[-] Error creating PostgreSQL table: {e}", flush=True)
        conn.rollback()
        return False
    finally:
        conn.close()

def create_es_index():
    print(f">>> Setting up Elasticsearch index 'ecourts_live_records' at {ES_URL}...", flush=True)
    try:
        es = Elasticsearch(ES_URL, request_timeout=10)
        index_name = "ecourts_live_records"

        settings = {
            "analysis": {
                "analyzer": {
                    "ngram_analyzer": {
                        "tokenizer": "ngram_tokenizer",
                        "filter": ["lowercase", "asciifolding"]
                    },
                    "phonetic_analyzer": {
                        "tokenizer": "standard",
                        "filter": ["lowercase", "asciifolding", "phonetic_filter"]
                    },
                    "standard_folding": {
                        "tokenizer": "standard",
                        "filter": ["lowercase", "asciifolding"]
                    }
                },
                "tokenizer": {
                    "ngram_tokenizer": {
                        "type": "ngram",
                        "min_gram": 2,
                        "max_gram": 3,
                        "token_chars": ["letter", "digit"]
                    }
                },
                "filter": {
                    "phonetic_filter": {
                        "type": "phonetic",
                        "encoder": "beider_morse",
                        "replace": False
                    }
                }
            }
        }

        mappings = {
            "properties": {
                "cnr": {"type": "keyword"},
                "case_number": {"type": "keyword"},
                "case_type": {"type": "keyword"},
                "state_name": {
                    "type": "text",
                    "analyzer": "standard",
                    "fields": {"keyword": {"type": "keyword"}}
                },
                "district_name": {
                    "type": "text",
                    "analyzer": "standard",
                    "fields": {"keyword": {"type": "keyword"}}
                },
                "party_name": {
                    "type": "text",
                    "analyzer": "standard_folding",
                    "fields": {
                        "ngram": {"type": "text", "analyzer": "ngram_analyzer"},
                        "phonetic": {"type": "text", "analyzer": "phonetic_analyzer"}
                    }
                },
                "petitioner": {"type": "text"},
                "respondent": {"type": "text"},
                "registration_date": {"type": "date", "format": "yyyy-MM-dd||epoch_millis"},
                "next_hearing_date": {"type": "date", "format": "yyyy-MM-dd||epoch_millis"},
                "source_index": {"type": "keyword"},
                "source_table": {"type": "keyword"}
            }
        }

        if not es.indices.exists(index=index_name):
            es.indices.create(index=index_name, settings=settings, mappings=mappings)
            print(f"[+] Elasticsearch index '{index_name}' created successfully!", flush=True)
        else:
            print(f"[+] Elasticsearch index '{index_name}' already exists.", flush=True)
        return True
    except Exception as e:
        print(f"[-] Note: Elasticsearch index setup: {e}", flush=True)
        return False

if __name__ == "__main__":
    print("==================================================", flush=True)
    print("Initializing Dedicated eCourts Live Storage Layers", flush=True)
    print("==================================================", flush=True)
    pg_ok = create_pg_table()
    es_ok = create_es_index()
    print(f"\nStatus: PG Table: {'SUCCESS' if pg_ok else 'FAILED'}, ES Index: {'SUCCESS' if es_ok else 'FAILED/OFFLINE'}", flush=True)
