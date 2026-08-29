import os
import re
import csv
import time
import argparse
import psycopg2
from collections import Counter
from urllib.parse import urlparse
from dotenv import load_dotenv

# Load environment
load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL not found in .env")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Regex pattern for splitters like vs, v/s, versus, v., etc.
VS_PATTERN = re.compile(
    r'\s+(?:vs\.?|v/s|versus|v\.\s*s\.?|v\s*/\s*s|v\.)\s+',
    re.IGNORECASE
)

def clean_entity_name(name_str: str) -> str:
    """Normalize whitespace and uppercase, but preserve exact letters and wording."""
    if not name_str:
        return ""
    # Collapse multiple whitespaces/tabs/newlines into a single space
    cleaned = " ".join(name_str.strip().split())
    # Uppercase for standard case-insensitive unique matching
    return cleaned.upper()

def split_parties(party_raw: str):
    """
    Splits a party string into (petitioner, respondent).
    Preserves exact character text without artificial combining.
    """
    if not party_raw:
        return "", ""
    
    parts = VS_PATTERN.split(party_raw.strip(), maxsplit=1)
    if len(parts) == 2:
        petitioner = clean_entity_name(parts[0])
        respondent = clean_entity_name(parts[1])
        return petitioner, respondent
    else:
        # If no delimiter found, record as petitioner
        return clean_entity_name(party_raw), ""

def get_db_connection():
    url = urlparse(DATABASE_URL)
    return psycopg2.connect(
        user=url.username,
        password=url.password,
        host=url.hostname,
        port=url.port or 5432,
        database=url.path.lstrip('/'),
        sslmode="require"
    )

def process_table(schema_name: str, table_name: str, out_dir: str, batch_size: int = 50000):
    full_table = f"{schema_name}.{table_name}"
    print(f"\n==========================================", flush=True)
    print(f"[*] Generating Golden Ledger for: {full_table}", flush=True)
    print(f"==========================================", flush=True)
    
    os.makedirs(out_dir, exist_ok=True)
    out_file = os.path.join(out_dir, f"{schema_name}_{table_name}_golden_ledger.csv")
    
    conn = get_db_connection()
    # Use server-side named cursor to stream large datasets without high memory usage
    cursor_name = f"cur_{schema_name}_{table_name}_{int(time.time())}"
    cur = conn.cursor(name=cursor_name)
    
    query = f"SELECT party_name, state_name FROM {full_table} WHERE party_name IS NOT NULL"
    cur.execute(query)
    
    petitioner_counts = Counter()
    respondent_counts = Counter()
    state_map = {}
    
    total_processed = 0
    start_time = time.time()
    
    while True:
        rows = cur.fetchmany(batch_size)
        if not rows:
            break
        
        for party_raw, state_name in rows:
            pet, resp = split_parties(party_raw)
            clean_state = (state_name or schema_name).strip().upper()
            
            if pet:
                petitioner_counts[pet] += 1
                state_map[pet] = clean_state
            if resp:
                respondent_counts[resp] += 1
                state_map[resp] = clean_state
                
        total_processed += len(rows)
        print(f"  Processed {total_processed:,} records... (Elapsed: {time.time() - start_time:.1f}s)", flush=True)
        
    cur.close()
    conn.close()
    
    # Write aggregated Golden Ledger to CSV
    print(f"Writing Golden CSV to: {out_file}", flush=True)
    with open(out_file, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["clean_name", "role", "state_name", "case_count_db"])
        
        # Write Petitioners
        for name, count in petitioner_counts.most_common():
            writer.writerow([name, "PETITIONER", state_map.get(name, "UNKNOWN"), count])
            
        # Write Respondents
        for name, count in respondent_counts.most_common():
            writer.writerow([name, "RESPONDENT", state_map.get(name, "UNKNOWN"), count])
            
    print(f"[OK] Finished {full_table}:", flush=True)
    print(f"   - Total Rows Processed: {total_processed:,}", flush=True)
    print(f"   - Unique Petitioners: {len(petitioner_counts):,}", flush=True)
    print(f"   - Unique Respondents: {len(respondent_counts):,}", flush=True)
    print(f"   - CSV saved: {out_file}\n", flush=True)
    return out_file

def get_all_tables():
    conn = get_db_connection()
    cur = conn.cursor()
    
    # 1. Supreme court
    cur.execute("""
        SELECT table_schema, table_name 
        FROM information_schema.tables 
        WHERE table_schema = 'supreme_court' AND table_name = 'all_cases';
    """)
    supreme_tables = cur.fetchall()

    # 2. High courts
    cur.execute("""
        SELECT table_schema, table_name 
        FROM information_schema.tables 
        WHERE table_schema = 'high_court' AND table_name NOT LIKE '%_backup'
        ORDER BY table_name;
    """)
    high_court_tables = cur.fetchall()

    # 3. District courts
    cur.execute("""
        SELECT table_schema, table_name 
        FROM information_schema.tables 
        WHERE table_schema = 'district_courts' AND table_name NOT LIKE '%_backup'
        ORDER BY table_name;
    """)
    district_tables = cur.fetchall()
    
    cur.close()
    conn.close()
    
    return supreme_tables + high_court_tables + district_tables

def main():
    parser = argparse.ArgumentParser(description="Generate Golden Entity Ledger CSVs from PostgreSQL")
    parser.add_argument("--schema", default=None, help="Schema name (e.g. supreme_court, district_courts, high_court)")
    parser.add_argument("--table", default=None, help="Table name (e.g. all_cases, delhi, sikkim)")
    parser.add_argument("--all", action="store_true", help="Generate golden ledger for all tables in DB")
    parser.add_argument("--out-dir", default="golden_ledger_data", help="Output directory for CSVs")
    args = parser.parse_args()
    
    if args.all:
        tables = get_all_tables()
        print(f"[*] Found {len(tables)} tables to process across Supreme Court, High Courts, and District Courts.", flush=True)
        for idx, (schema, table) in enumerate(tables, 1):
            print(f"\n[{idx}/{len(tables)}] Starting {schema}.{table}...", flush=True)
            try:
                process_table(schema, table, args.out_dir)
            except Exception as e:
                print(f"[ERROR] Failed processing {schema}.{table}: {e}", flush=True)
    elif args.schema and args.table:
        process_table(args.schema, args.table, args.out_dir)
    else:
        # Default test run
        process_table("supreme_court", "all_cases", args.out_dir)

if __name__ == "__main__":
    main()
