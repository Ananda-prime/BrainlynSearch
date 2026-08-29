import os
import re
import time
import datetime
import psycopg2
from elasticsearch import Elasticsearch, helpers
from dotenv import load_dotenv

# Load environment variables
load_dotenv(dotenv_path="../../.env")
load_dotenv()

# Clear any libpq environment overrides to prevent them from corrupting the connection string
for key in ['PGUSER', 'PGPASSWORD', 'PGHOST', 'PGPORT', 'PGDATABASE']:
    os.environ.pop(key, None)

# Configuration
ES_URL = os.getenv("ES_URL", "http://localhost:9201")

# Primary Target database URL (resolves to the new Neon cluster lexisearch DB)
PG_URL = os.getenv("DATABASE_URL") or "postgresql://neondb_owner:npg_6swO9ieutHIX@ep-jolly-bonus-aoumwpz5-pooler.c-2.ap-southeast-1.aws.neon.tech/lexisearch?sslmode=require&channel_binding=require"

# Connect to Elasticsearch
es = Elasticsearch(
    ES_URL,
    request_timeout=600,
    retry_on_timeout=True,
    max_retries=10
)

# Batch size for database fetching and Elasticsearch indexing
BATCH_SIZE = 50000

# ─────────────────────────────────────────────────────────────
# 1. Date Normalization & Sanitization
# ─────────────────────────────────────────────────────────────
def normalize_date(date_val):
    if not date_val:
        return None
    if isinstance(date_val, (datetime.date, datetime.datetime)):
        return date_val.strftime("%Y-%m-%d")
    if not isinstance(date_val, str):
        return None
        
    date_str = date_val.strip()
    if not date_str or date_str.lower() in ("none", "nan", "null", ""):
        return None
        
    # Extract only the date part if there's a timestamp component
    date_part = date_str.split()[0]
    normalized = date_part.replace("/", "-")
    
    # Check YYYY-MM-DD
    if re.match(r"^\d{4}-\d{2}-\d{2}$", normalized):
        return normalized
        
    # Check DD-MM-YYYY
    match_dmy = re.match(r"^(\d{1,2})-(\d{1,2})-(\d{4})$", normalized)
    if match_dmy:
        day, month, year = match_dmy.groups()
        return f"{year}-{month.zfill(2)}-{day.zfill(2)}"
        
    # Check YYYY-M-D or similar, and pad
    match_ymd = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", normalized)
    if match_ymd:
        year, month, day = match_ymd.groups()
        return f"{year}-{month.zfill(2)}-{day.zfill(2)}"
        
    return None

# ─────────────────────────────────────────────────────────────
# 2. Setup Index Settings & Mappings in Elasticsearch
# ─────────────────────────────────────────────────────────────
def ensure_indices_exist():
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
                "fields": { "keyword": { "type": "keyword" } }
            },
            "district_name": {
                "type": "text",
                "analyzer": "standard",
                "fields": { "keyword": { "type": "keyword" } }
            },
            "party_name": {
                "type": "text",
                "analyzer": "standard_folding",
                "fields": {
                    "ngram": { "type": "text", "analyzer": "ngram_analyzer" },
                    "phonetic": { "type": "text", "analyzer": "phonetic_analyzer" }
                }
            },
            "registration_date": {"type": "date", "format": "yyyy-MM-dd||epoch_millis"},
            "diary_number": {"type": "keyword"},
            "source_index": {"type": "keyword"},
            "source_table": {"type": "keyword"}
        }
    }

    indices = ["court_records", "high_court_records", "supreme_court_records", "sync_state"]
    for idx in indices:
        if idx == "sync_state":
            if not es.indices.exists(index=idx):
                es.indices.create(index=idx)
            continue
            
        if not es.indices.exists(index=idx):
            print(f"Creating index '{idx}' with proper analyzers & mappings...")
            try:
                es.indices.create(index=idx, settings=settings, mappings=mappings)
                print(f"Index '{idx}' created successfully.")
            except Exception as e:
                print(f"Error creating index {idx}: {e}")

# ─────────────────────────────────────────────────────────────
# 3. Read and Update Sync State in ES
# ─────────────────────────────────────────────────────────────
def get_sync_state(table_name):
    try:
        res = es.get(index="sync_state", id=table_name)
        return res["_source"].get("last_id", "")
    except Exception:
        return ""

def update_sync_state(table_name, last_id):
    es.index(
        index="sync_state",
        id=table_name,
        document={
            "table_name": table_name,
            "last_id": last_id,
            "updated_at": int(time.time())
        }
    )

# ─────────────────────────────────────────────────────────────
# 4. PostgreSQL Helpers
# ─────────────────────────────────────────────────────────────
def get_db_connection():
    try:
        from urllib.parse import urlparse
        url = urlparse(PG_URL)
        
        print(f"Connecting to Postgres at '{url.hostname}' as user '{url.username}'...")
        
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
        print(f"Database connection error: {e}")
        return None

def get_tables_to_sync(conn):
    cursor = conn.cursor()
    
    # District Courts tables (excluding backups)
    cursor.execute("""
        SELECT table_name FROM information_schema.tables 
        WHERE table_schema = 'district_courts' AND table_name NOT LIKE '%_backup'
        ORDER BY table_name;
    """)
    district_tables = [f"district_courts.{t[0]}" for t in cursor.fetchall()]
    
    # High Court tables (excluding backups)
    cursor.execute("""
        SELECT table_name FROM information_schema.tables 
        WHERE table_schema = 'high_court' AND table_name NOT LIKE '%_backup'
        ORDER BY table_name;
    """)
    high_court_tables = [f"high_court.{t[0]}" for t in cursor.fetchall()]
    
    # Supreme Court tables
    cursor.execute("""
        SELECT table_name FROM information_schema.tables 
        WHERE table_schema = 'supreme_court' AND table_name = 'all_cases'
        ORDER BY table_name;
    """)
    supreme_tables = [f"supreme_court.{t[0]}" for t in cursor.fetchall()]
    
    cursor.close()
    return {
        "court_records": district_tables,
        "high_court_records": high_court_tables,
        "supreme_court_records": supreme_tables
    }

def execute_query_with_retry(query_fn, *args, **kwargs):
    retries = 5
    while retries > 0:
        conn = get_db_connection()
        if conn:
            try:
                with conn.cursor() as cursor:
                    res = query_fn(cursor, *args, **kwargs)
                    conn.commit()
                    return res
            except Exception as e:
                print(f"DB Query failed, retrying in 10s... ({retries - 1} left). Error: {e}")
                conn.rollback()
            finally:
                conn.close()
        else:
            print(f"DB Connection failed, retrying in 10s... ({retries - 1} left).")
        retries -= 1
        time.sleep(10)
    raise Exception("Failed to execute database query after maximum retries.")

def fetch_batch_fn(cursor, table_name, last_id, limit):
    query = f"""
        SELECT id, cnr, case_number, case_type, party_name, state_name, district_name, registration_date, diary_number 
        FROM {table_name} 
        WHERE id > %s 
        ORDER BY id 
        LIMIT %s
    """
    cursor.execute(query, (last_id, limit))
    colnames = [desc[0] for desc in cursor.description]
    return [dict(zip(colnames, row)) for row in cursor.fetchall()]

# ─────────────────────────────────────────────────────────────
# 5. Main Sync Loop
# ─────────────────────────────────────────────────────────────
def run_sync():
    from urllib.parse import urlparse
    parsed = urlparse(PG_URL)
    print(f"Starting Robust Resilient Sync on Neon: host='{parsed.hostname}', user='{parsed.username}'")
    ensure_indices_exist()
    
    conn = get_db_connection()
    if not conn:
        print("Fatal: Could not connect to PostgreSQL. Exiting.")
        return
        
    print("Fetching table list from database...")
    try:
        tables_by_index = get_tables_to_sync(conn)
    except Exception as e:
        print(f"Fatal error fetching tables: {e}")
        return
    finally:
        conn.close()
        
    # Enforce strict sync order: Supreme Court, High Court, then District Court
    sync_order = ["supreme_court_records", "high_court_records", "court_records"]
    
    for es_index in sync_order:
        tables = tables_by_index.get(es_index, [])
        print(f"\n=========================================")
        print(f"Syncing target Elasticsearch Index: {es_index}")
        print(f"=========================================")
        
        for table_name in tables:
            print(f"\n>>> Syncing Table: {table_name}")
            
            last_id = get_sync_state(table_name)
            if last_id:
                print(f"  Resuming from last synced ID: {last_id}")
            else:
                print("  Starting sync from the beginning.")
                
            total_synced_table = 0
            
            while True:
                # 1. Fetch batch from database
                try:
                    batch = execute_query_with_retry(fetch_batch_fn, table_name, last_id, BATCH_SIZE)
                except Exception as e:
                    print(f"❌ Failed to fetch batch for {table_name}: {e}. Skipping table.")
                    break
                    
                if not batch:
                    print(f"  ✅ Table {table_name} is fully synchronized!")
                    break
                    
                # 2. Prepare bulk index actions
                actions = []
                for row in batch:
                    reg_date = normalize_date(row.get("registration_date"))
                    
                    actions.append({
                        "_index": es_index,
                        "_id": f"{table_name}_{row['id']}",
                        "_source": {
                            "cnr": row.get("cnr"),
                            "case_number": row.get("case_number"),
                            "case_type": (row.get("case_type") or "").upper() or None,
                            "party_name": row.get("party_name"),
                            "state_name": row.get("state_name", "N/A"),
                            "district_name": row.get("district_name", "N/A"),
                            "registration_date": reg_date,
                            "diary_number": row.get("diary_number"),
                            "source_index": es_index,
                            "source_table": table_name
                        }
                    })
                    
                # 3. Bulk index into Elasticsearch with retry
                es_success = False
                es_retries = 5
                while es_retries > 0 and not es_success:
                    try:
                        success, errors = helpers.bulk(
                            es.options(request_timeout=300, max_retries=5),
                            actions,
                            chunk_size=5000,
                            raise_on_error=False
                        )
                        total_synced_table += success
                        if errors:
                            print(f"  ⚠️ {len(errors)} docs failed to index. Sample: {errors[0]['index']['error']['reason']}")
                        es_success = True
                    except Exception as es_e:
                        es_retries -= 1
                        print(f"  ❌ ES bulk error, retrying in 10s... ({es_retries} left). Error: {es_e}")
                        time.sleep(10)
                        
                if not es_success:
                    print(f"❌ Failed to index batch for {table_name} in ES. Skipping remaining records for this table.")
                    break
                    
                # 4. Save progress
                last_id = batch[-1]["id"]
                try:
                    update_sync_state(table_name, last_id)
                except Exception as tracker_e:
                    print(f"  ⚠️ Warning: Failed to save sync state in ES: {tracker_e}")
                    
                print(f"  Indexed {len(batch)} docs (Total synced for this table: {total_synced_table}). Last ID: {last_id}")
                
                # End of table check
                if len(batch) < BATCH_SIZE:
                    print(f"  ✅ Table {table_name} completed!")
                    break

    print("\n=========================================")
    print("✅ All Source Court Records Synchronized Successfully!")
    print("=========================================")

if __name__ == "__main__":
    run_sync()
