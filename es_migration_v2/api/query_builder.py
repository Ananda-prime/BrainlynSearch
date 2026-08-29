import re
from typing import Dict, Any, List

def parse_date_range(s: str):
    s = s.strip()
    # 1. Check YYYY-MM-DD
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s, s
    # 2. Check YYYY-YYYY
    match_range = re.match(r"^(\d{4})\s*-\s*(\d{4})$", s)
    if match_range:
        y1, y2 = sorted([int(match_range.group(1)), int(match_range.group(2))])
        return f"{y1}-01-01", f"{y2}-12-31"
    # 3. Check YYYY
    if re.match(r"^\d{4}$", s):
        return f"{s}-01-01", f"{s}-12-31"
    return None

def build_es_query(params: Dict[str, Any]) -> Dict[str, Any]:
    name = (params.get('name') or '').strip()
    state = (params.get('state') or '').strip()
    district = (params.get('district') or '').strip()
    registration_date = (params.get('registration_date') or '').strip()
    cnr = (params.get('cnr') or '').strip()
    
    query = {
        "bool": {
            "must": [],
            "should": [], # Used for boosting scores
            "filter": []
        }
    }
    
    # 1. Mandatory Name Search (High Importance)
    if name:
        query["bool"]["must"].append({
            "multi_match": {
                "query": name,
                "fields": ["party_name^3", "party_name.ngram^2", "party_name.phonetic"],
                "type": "best_fields",
                "fuzziness": "AUTO",
                "operator": "and"
            }
        })
        
        # ADDED: Massive boost for exact phrase sequence
        query["bool"]["should"].append({
            "match_phrase": {
                "party_name": {
                    "query": name,
                    "boost": 10.0
                }
            }
        })
    
    # 2. Optional Filters (Strict)
    if registration_date:
        parsed = parse_date_range(registration_date)
        if parsed:
            start_date, end_date = parsed
            query["bool"]["filter"].append({
                "range": {
                    "registration_date": {
                        "gte": start_date,
                        "lte": end_date
                    }
                }
            })

    if cnr:
        query["bool"]["filter"].append({
            "wildcard": { "cnr": {"value": f"*{cnr.upper()}*"} }
        })
        
    # 3. Location Filters (Optimized for Sub-Second Performance)
    # Bypass state and district filters for Supreme Court records (which have state_name='Unknown')
    court_type = (params.get('court_type') or 'all').strip().lower()
    if court_type != "supreme":
        if state:
            query["bool"]["filter"].append({
                "match": {
                    "state_name": {
                        "query": state,
                        "operator": "and"
                    }
                }
            })
            
        if district:
            query["bool"]["filter"].append({
                "match": {
                    "district_name": {
                        "query": district,
                        "operator": "and"
                    }
                }
            })
        
    return query
