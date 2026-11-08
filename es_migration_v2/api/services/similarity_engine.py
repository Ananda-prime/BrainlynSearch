import os
import re
import difflib
from typing import List, Dict, Any, Tuple, Set

# State Code Mapping Dictionary
# Complete State/UT Code Mapping (all 36 Indian States & Union Territories)
STATE_CODE_MAP = {
    # States
    "AP": "Andhra Pradesh", "AR": "Arunachal Pradesh", "AS": "Assam",
    "BR": "Bihar", "CT": "Chhattisgarh", "CG": "Chhattisgarh",
    "GA": "Goa", "GJ": "Gujarat", "HR": "Haryana",
    "HP": "Himachal Pradesh", "JH": "Jharkhand", "KA": "Karnataka",
    "KL": "Kerala", "MP": "Madhya Pradesh", "MH": "Maharashtra",
    "MN": "Manipur", "ML": "Meghalaya", "MZ": "Mizoram",
    "NL": "Nagaland", "OD": "Odisha", "OR": "Odisha",
    "PB": "Punjab", "RJ": "Rajasthan", "SK": "Sikkim",
    "TN": "Tamil Nadu", "TS": "Telangana", "TR": "Tripura",
    "UP": "Uttar Pradesh", "UK": "Uttarakhand", "UT": "Uttarakhand",
    "WB": "West Bengal",
    # Union Territories
    "AN": "Andaman and Nicobar Islands", "CH": "Chandigarh",
    "DN": "Dadra and Nagar Haveli and Daman and Diu", "DD": "Daman and Diu",
    "DL": "Delhi", "JK": "Jammu and Kashmir", "LA": "Ladakh",
    "LD": "Lakshadweep", "PY": "Puducherry"
}

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

def _score_single_party(query: str, target: str) -> float:
    """Computes similarity percentage between query and a single individual/entity name."""
    if not query or not target:
        return 0.0

    q = re.sub(r'\s+', ' ', query.lower().strip())
    t = re.sub(r'\s+', ' ', target.lower().strip())

    if q == t:
        return 100.0

    # Substring Match
    if q in t:
        len_ratio = min(len(q), len(t)) / max(len(q), len(t), 1)
        return round(90.0 + (9.0 * len_ratio), 1)
    if t in q:
        len_ratio = min(len(q), len(t)) / max(len(q), len(t), 1)
        return round(88.0 + (9.0 * len_ratio), 1)

    # Token Set Overlap
    q_tokens = [w for w in q.split() if len(w) > 1]
    t_tokens = [w for w in t.split() if len(w) > 1]
    q_set = set(q_tokens)
    t_set = set(t_tokens)
    
    if q_set and t_set:
        intersection = q_set.intersection(t_set)
        if intersection == q_set:
            return 93.0
        if len(intersection) > 0:
            jaccard = len(intersection) / len(q_set.union(t_set))
            if jaccard >= 0.4:
                return round(75.0 + (20.0 * jaccard), 1)

    # Phonetic Soundex Match
    q_soundex = get_soundex_set(q)
    t_soundex = get_soundex_set(t)
    if q_soundex and t_soundex:
        s_intersect = q_soundex.intersection(t_soundex)
        q_last_soundex = soundex(q_tokens[-1]) if q_tokens else None
        t_last_soundex = soundex(t_tokens[-1]) if t_tokens else None
        surname_matches = bool(q_last_soundex and t_last_soundex and q_last_soundex == t_last_soundex)

        if len(s_intersect) == len(q_soundex):
            # All tokens match soundex; higher confidence if surname is confirmed
            return 88.0 if surname_matches else 82.0
        if len(s_intersect) > 0:
            ratio = len(s_intersect) / len(q_soundex.union(t_soundex))
            if ratio >= 0.5:
                # If surname does not match, cap below 68% to prevent cross-name mismatch
                if surname_matches:
                    return round(76.0 + (10.0 * ratio), 1)
                else:
                    return round(60.0 + (8.0 * ratio), 1)

    # Sequence Matcher fuzzy ratio
    base_ratio = difflib.SequenceMatcher(None, q, t).ratio() * 100.0
    return round(base_ratio, 1)


def calculate_name_similarity(query: str, target: str) -> float:
    """
    Computes strict similarity percentage (0-100) between user query and target party name.
    If target is a multi-party list (comma/semicolon separated), evaluates against EACH individual party entity
    to prevent false-positive soundex/token pooling across hundreds of unrelated co-parties.
    """
    if not query or not target:
        return 0.0

    # Split multi-party lists if present
    parties = [p.strip() for p in re.split(r'[,;]+', target) if p.strip()]
    if len(parties) > 1:
        return max(_score_single_party(query, p) for p in parties)
    return _score_single_party(query, target)

def _extract_district_name(court_name: str, state_name: str) -> str:
    """
    Extract district name from eCourts courtName string.
    Handles patterns like:
      - "..., East District, Delhi"            → "East"
      - "District and Sessions Court, Hyderabad" → "Hyderabad"
    Falls back to last meaningful comma-segment if no pattern matches.
    """
    if not court_name:
        return ""

    # Pattern 1: "XYZ District" → extract XYZ
    match = re.search(r'(\b[\w\s]+?)\s+District\b', court_name, re.IGNORECASE)
    if match:
        candidate = match.group(1).strip()
        skip_words = {"and sessions", "sessions", "the", "district"}
        if candidate.lower() not in skip_words:
            return candidate

    # Pattern 2: "District ... Court, CITY" → extract CITY
    match = re.search(r'District\b.*?Court[,\s]+([\w][\w\s]*)', court_name, re.IGNORECASE)
    if match:
        return match.group(1).strip().rstrip(',')

    # Fallback: last comma-segment that isn't the state name or a court identifier
    parts = [p.strip() for p in court_name.split(',') if p.strip()]
    for part in reversed(parts):
        lower = part.lower()
        if lower != state_name.lower() and "court" not in lower and len(part) > 2:
            return part

    return ""


def normalize_ecourts_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """Transforms raw eCourts JSON record into standard internal schema preserving 100% of fields."""
    petitioners_list = item.get("petitioners", []) or []
    respondents_list = item.get("respondents", []) or []
    
    petitioner_str = ", ".join(petitioners_list) if petitioners_list else "Unknown"
    respondent_str = ", ".join(respondents_list) if respondents_list else "Unknown"
    party_name = f"{petitioner_str} vs {respondent_str}"

    state_code = (item.get("stateCode") or "").upper()
    state_name = STATE_CODE_MAP.get(state_code, state_code)

    court_code = (item.get("courtCode") or "").upper()
    court_type = "high" if "HC" in court_code else "district"

    cnr = item.get("cnr") or item.get("id") or ""
    case_type = (item.get("caseType") or "UNKNOWN").upper()
    case_number = item.get("registrationNumber") or item.get("filingNumber") or ""
    filing_number = item.get("filingNumber") or ""
    
    return {
        "cnr": cnr,
        "party_name": party_name,
        "petitioner": petitioner_str,
        "respondent": respondent_str,
        "petitioners": petitioners_list,
        "respondents": respondents_list,
        "case_type": case_type,
        "case_number": case_number,
        "filing_number": filing_number,
        "case_status": item.get("caseStatus") or "UNKNOWN",
        "state_code": state_code,
        "state_name": state_name,
        "district_code": item.get("districtCode"),
        "district_name": _extract_district_name(item.get("courtName") or "", state_name),
        "court_code": court_code,
        "court_name": item.get("courtName") or "",
        "court_type": court_type,
        "registration_date": item.get("registrationDate") or item.get("filingDate"),
        "filing_date": item.get("filingDate"),
        "next_hearing_date": item.get("nextHearingDate"),
        "decision_date": item.get("decisionDate"),
        "judges": item.get("judges", []),
        "petitioner_advocates": item.get("petitionerAdvocates", []),
        "respondent_advocates": item.get("respondentAdvocates", []),
        "case_category": item.get("caseCategory") or "",
        "order_count": item.get("orderCount", 0),
        "interim_order_count": item.get("interimOrderCount", 0),
        "judgment_count": item.get("judgmentCount", 0),
        "hearing_count": item.get("hearingCount", 0),
        "has_orders": item.get("hasOrders", False),
        "has_judgments": item.get("hasJudgments", False),
        "raw_payload": item,
        "source_table": "ecourts_live_records",
        "source_index": "ecourts_live_records"
    }

def filter_and_score_ecourts_records(
    raw_results: List[Dict[str, Any]],
    query_name: str,
    is_petitioner: bool = False,
    is_respondent: bool = False,
    min_similarity: float = 75.0
) -> List[Dict[str, Any]]:
    """
    Normalizes eCourts raw records and filters for records with >= min_similarity (default 75%).
    Attaches a 'confidence' field to each matching record.
    """
    scored_records = []
    
    for item in raw_results:
        rec = normalize_ecourts_item(item)
        if not rec["cnr"]:
            continue

        # Check exact or prefix CNR / case_number match (e.g. CNR search)
        clean_q = (query_name or "").strip().upper()
        if clean_q and (clean_q == rec["cnr"].upper() or clean_q in rec["cnr"].upper() or (rec.get("case_number") and clean_q in rec["case_number"].upper())):
            rec["confidence"] = 100.0
            scored_records.append(rec)
            continue

        # Evaluate target string depending on search mode
        if is_petitioner and not is_respondent:
            score = calculate_name_similarity(query_name, rec["petitioner"])
        elif is_respondent and not is_petitioner:
            score = calculate_name_similarity(query_name, rec["respondent"])
        else:
            score_p = calculate_name_similarity(query_name, rec["petitioner"])
            score_r = calculate_name_similarity(query_name, rec["respondent"])
            score = max(score_p, score_r)

        if score >= min_similarity:
            rec["confidence"] = round(score, 1)
            scored_records.append(rec)

    # Sort descending by confidence
    scored_records.sort(key=lambda x: x.get("confidence", 0), reverse=True)
    return scored_records
