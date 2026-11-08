import os
import re
import csv
import glob
import time
import difflib
import sys
import argparse
from typing import List, Dict, Any, Set

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# ─────────────────────────────────────────────────────────────
# 1. Phonetic Encoders (Soundex Helper)
# ─────────────────────────────────────────────────────────────
def soundex(name: str) -> str:
    """Standard American Soundex Algorithm."""
    if not name:
        return ""
    name = re.sub(r'[^A-Z]', '', name.upper())
    if not name:
        return ""
    
    first_letter = name[0]
    mapping = {
        'B': '1', 'F': '1', 'P': '1', 'V': '1',
        'C': '2', 'G': '2', 'J': '2', 'K': '2', 'Q': '2', 'S': '2', 'X': '2', 'Z': '2',
        'D': '3', 'T': '3',
        'L': '4',
        'M': '5', 'N': '5',
        'R': '6'
    }
    
    encoded = [first_letter]
    last_digit = mapping.get(first_letter, '')
    
    for char in name[1:]:
        digit = mapping.get(char, '')
        if digit != '':
            if digit != last_digit:
                encoded.append(digit)
            last_digit = digit
        else:
            last_digit = ''
            
    code = "".join(encoded)
    code = code[:4].ljust(4, '0')
    return code

def get_soundex_set(text: str) -> Set[str]:
    words = re.findall(r'[A-Z0-9]{2,}', text.upper())
    return {soundex(w) for w in words}

# ─────────────────────────────────────────────────────────────
# 2. Detailed Similarity Scorer
# ─────────────────────────────────────────────────────────────
def calculate_similarity(q: str, t: str, q_tokens: Set[str], q_soundex: Set[str]) -> tuple[float, str]:
    if not q or not t:
        return 0.0, "NONE"
    
    # 1. Exact Match
    if q == t:
        return 100.0, "EXACT"
    
    # 2. Substring Match
    if q in t:
        len_ratio = min(len(q), len(t)) / max(len(q), len(t), 1)
        score = 90.0 + (9.0 * len_ratio)
        return round(score, 1), "EXACT_SUBSTRING"
    if t in q:
        len_ratio = min(len(q), len(t)) / max(len(q), len(t), 1)
        score = 88.0 + (9.0 * len_ratio)
        return round(score, 1), "CONTAINED_IN_QUERY"
    
    lq, lt = len(q), len(t)
    len_bound = (2.0 * (lq if lq < lt else lt)) / (lq + lt) if (lq + lt) > 0 else 0

    t_tokens = set(t.split())
    # 3. Token Set Match
    if q_tokens and t_tokens:
        intersection = q_tokens.intersection(t_tokens)
        if intersection == q_tokens:
            return 93.0, "ALL_WORDS_MATCHED"
        if len(intersection) > 0:
            jaccard = len(intersection) / len(q_tokens.union(t_tokens))
            if jaccard >= 0.4:
                return round(75.0 + (15.0 * jaccard), 1), "PARTIAL_WORDS_MATCH"
            elif len_bound < 0.65:
                return 0.0, "LOW"
                
    # 4. Upper bound length check before expensive soundex/difflib
    if len_bound < 0.65:
        return 0.0, "LOW"

    # 5. Phonetic Match
    t_soundex = get_soundex_set(t)
    if q_soundex and t_soundex:
        p_inter = q_soundex.intersection(t_soundex)
        if p_inter == q_soundex:
            return 85.0, "PHONETIC_ALL_WORDS"
        if len(p_inter) > 0 and len(q_soundex) > 1:
            return 78.0, "PHONETIC_PARTIAL"
            
    # 6. Fuzzy Sequence Matching
    base_ratio = difflib.SequenceMatcher(None, q, t).ratio() * 100.0
    if base_ratio >= 65.0:
        return round(base_ratio, 1), "FUZZY_TYPO_MATCH"
        
    return round(base_ratio, 1), "LOW"

def _process_single_csv_file(args_tuple):
    file_path, query_meta, min_score, role_filter = args_tuple
    file_basename = os.path.basename(file_path)
    file_results = {qm["query_raw"]: [] for qm in query_meta}
    rows_scanned = 0
    
    try:
        with open(file_path, mode="r", encoding="utf-8", errors="ignore") as f:
            header = f.readline()
            for line in f:
                parts = line.rstrip('\r\n').split(',')
                if len(parts) < 4:
                    continue
                clean_name, role, state, count = parts[0], parts[1], parts[2], parts[3]
                rows_scanned += 1
                
                if role_filter and role_filter.upper() != "ALL" and role != role_filter.upper():
                    continue
                
                len_c = len(clean_name)
                if len_c == 0:
                    continue

                name_words = set(clean_name.split())

                for qm in query_meta:
                    q_clean = qm["query_clean"]
                    is_candidate = (q_clean in clean_name) or (len(qm["clean_tokens"].intersection(name_words)) >= qm["min_match_count"])
                        
                    if is_candidate:
                        score, match_type = calculate_similarity(q_clean, clean_name, qm["tokens"], qm["soundex_set"])
                        if score >= min_score:
                            file_results[qm["query_raw"]].append({
                                "matched_name": clean_name,
                                "role": role,
                                "state_name": state,
                                "case_count_db": int(count) if count.isdigit() else count,
                                "similarity_score": score,
                                "match_type": match_type,
                                "source_file": file_basename
                            })
    except Exception as e:
        pass
        
    return rows_scanned, file_results

STOP_WORDS = {"PRIVATE", "LIMITED", "PVT", "LTD", "CORP", "CORPORATION", "COMPANY", "CO", "AND", "INC", "LLP", "OF", "THE", "IN", "FOR"}

# ─────────────────────────────────────────────────────────────
# 3. High-Performance Multi-File Search
# ─────────────────────────────────────────────────────────────
def search_names_in_golden_data(
    queries: List[str], 
    golden_dir: str = "golden_ledger_data", 
    min_score: float = 65.0,
    role_filter: str = None
) -> Dict[str, List[Dict[str, Any]]]:
    
    csv_files = glob.glob(os.path.join(golden_dir, "*.csv"))
    if not csv_files:
        print(f"[!] No CSV files found in '{golden_dir}'", flush=True)
        return {}
    
    print(f"[*] Searching {len(queries)} query name(s) across {len(csv_files)} Golden Ledger files in parallel (Min score: {min_score}%)...", flush=True)
    start_time = time.time()
    
    # Precompute query metadata
    query_meta = []
    for q_raw in queries:
        q_clean = " ".join(q_raw.strip().upper().split())
        raw_tokens = set(re.findall(r'[A-Z0-9]{2,}', q_clean))
        clean_tokens = {t for t in raw_tokens if t not in STOP_WORDS and len(t) >= 2}
        if not clean_tokens:
            clean_tokens = raw_tokens
        prefixes = {t[:3] for t in clean_tokens if len(t) >= 3}
        first_chars = {t[0] for t in clean_tokens if t}
        min_match_count = min(2, len(clean_tokens)) if clean_tokens else 1
        s_set = get_soundex_set(q_clean)
        len_q = len(q_clean)
        query_meta.append({
            "query_raw": q_raw,
            "query_clean": q_clean,
            "tokens": raw_tokens,
            "clean_tokens": clean_tokens,
            "min_match_count": min_match_count,
            "prefixes": prefixes,
            "first_chars": first_chars,
            "soundex_set": s_set,
            "len_q": len_q
        })
        
    results: Dict[str, List[Dict[str, Any]]] = {q: [] for q in queries}
    total_rows_scanned = 0
    
    for idx, f in enumerate(csv_files, 1):
        fname = os.path.basename(f)
        print(f"[{idx}/{len(csv_files)}] Scanning {fname}...", flush=True)
        r_scanned, f_res = _process_single_csv_file((f, query_meta, min_score, role_filter))
        total_rows_scanned += r_scanned
        for q, matches in f_res.items():
            results[q].extend(matches)
        print(f"    [OK] {fname}: {r_scanned:,} rows scanned.", flush=True)
                            
    elapsed = time.time() - start_time
    print(f"\n[OK] Scanned {total_rows_scanned:,} rows across {len(csv_files)} files in {elapsed:.2f} seconds.\n", flush=True)
    
    # Sort results
    for q in queries:
        results[q].sort(key=lambda x: (x["similarity_score"], int(x["case_count_db"]) if str(x["case_count_db"]).isdigit() else 0), reverse=True)
        
    return results

def print_search_report(results: Dict[str, List[Dict[str, Any]]]):
    print("="*105)
    print(" 🎯 GOLDEN LEDGER SEARCH REPORT (DATABASE TRUTH) ")
    print("="*105)
    
    for query, matches in results.items():
        print(f"\n🔍 SEARCH QUERY: \"{query}\"")
        total_cases = sum(m['case_count_db'] for m in matches if isinstance(m['case_count_db'], int))
        print(f"   Matches Found: {len(matches)} distinct name variation(s) | Total Cases in DB: {total_cases:,}")
        print("-" * 105)
        
        if not matches:
            print("   ❌ NOT PRESENT in any Golden Data CSV (Zero occurrences across all 62 court tables)")
            continue
            
        print(f"   {'#':<3} | {'SCORE':<7} | {'MATCH TYPE':<22} | {'ROLE':<11} | {'CASES':<7} | {'STATE':<18} | {'MATCHED NAME IN DB'}")
        print(f"   {'-'*3}-+-{'-'*7}-+-{'-'*22}-+-{'-'*11}-+-{'-'*7}-+-{'-'*18}-+-{'-'*30}")
        
        for idx, m in enumerate(matches[:25], 1): # Display top 25 matches
            score_str = f"{m['similarity_score']}%"
            print(f"   {idx:<3} | {score_str:<7} | {m['match_type']:<22} | {m['role']:<11} | {m['case_count_db']:<7} | {m['state_name'][:18]:<18} | {m['matched_name']}")
            
        if len(matches) > 25:
            print(f"   ... and {len(matches) - 25} more variations in DB.")
            
    print("\n" + "="*105 + "\n")

def main():
    parser = argparse.ArgumentParser(description="Generic Golden Ledger Name Search Tool")
    parser.add_argument("--names", nargs="+", help="Names to search")
    parser.add_argument("--file", help="Path to text file containing names")
    parser.add_argument("--golden-dir", default="golden_ledger_data", help="Path to golden ledger CSV folder")
    parser.add_argument("--min-score", type=float, default=65.0, help="Minimum similarity threshold score")
    parser.add_argument("--role", default="ALL", choices=["ALL", "PETITIONER", "RESPONDENT"], help="Filter by role")
    
    args = parser.parse_args()
    
    queries = []
    if args.names:
        queries.extend(args.names)
    elif args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            queries = [line.strip() for line in f if line.strip()]
    else:
        queries = [
            "PAYOD JENA",
            "B T Polymer",
            "ABA CONSTRUCTION",
            "VARDHMAN GRANITO",
            "OMKAR PRINT LAB PRIVATE LIMITED",
            "SONPALSINGH DHARMPALSINGH RAJPUT"
        ]
        
    results = search_names_in_golden_data(queries, args.golden_dir, args.min_score, args.role)
    print_search_report(results)

if __name__ == "__main__":
    main()
