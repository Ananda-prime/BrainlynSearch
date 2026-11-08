import os
import json
import psycopg2
from urllib.parse import urlparse
from typing import List, Dict, Any, Optional
from elasticsearch import Elasticsearch
from dotenv import load_dotenv

# Load environment variables explicitly
env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))), ".env")
load_dotenv(dotenv_path=env_path, override=True)
load_dotenv(override=True)

def get_pg_conn():
    pg_url = os.getenv("DATABASE_URL")
    if not pg_url:
        return None
    try:
        url = urlparse(pg_url)
        conn_kwargs = {
            "user": url.username,
            "password": url.password,
            "host": url.hostname,
            "port": url.port or 5432,
            "database": url.path.lstrip('/'),
            "sslmode": "require",
            "connect_timeout": 10
        }
        return psycopg2.connect(**conn_kwargs)
    except Exception as e:
        print(f"[Ingest Worker] PostgreSQL connection error: {e}")
        return None

import re

def _clean_str_list(val) -> List[str]:
    """Ensures a list of clean strings safe for PostgreSQL TEXT[] arrays."""
    if not val:
        return []
    if isinstance(val, (list, tuple, set)):
        cleaned = []
        for item in val:
            if isinstance(item, dict):
                cleaned.append(str(item.get("name") or str(item)).strip())
            elif item is not None:
                cleaned.append(str(item).strip())
        return cleaned
    return [str(val).strip()]

def _clean_date(val: Any) -> Optional[str]:
    """Safely normalizes date values to YYYY-MM-DD or None for PostgreSQL DATE columns."""
    if not val:
        return None
    val_str = str(val).strip()
    if not val_str or val_str.lower() in ("null", "none", "n/a", "-", ""):
        return None
    # YYYY-MM-DD
    if re.match(r"^\d{4}-\d{2}-\d{2}$", val_str):
        return val_str
    # DD-MM-YYYY or DD/MM/YYYY
    match_dmy = re.match(r"^(\d{2})[-/](\d{2})[-/](\d{4})$", val_str)
    if match_dmy:
        d, m, y = match_dmy.groups()
        return f"{y}-{m}-{d}"
    # YYYY/MM/DD
    match_ymd = re.match(r"^(\d{4})/(\d{2})/(\d{2})$", val_str)
    if match_ymd:
        y, m, d = match_ymd.groups()
        return f"{y}-{m}-{d}"
    return None

def _safe_int(val: Any, default: int = 0) -> int:
    try:
        return int(val) if val is not None else default
    except (ValueError, TypeError):
        return default

def persist_ecourts_records(records: List[Dict[str, Any]]) -> None:
    """
    Background worker task to persist new eCourts records into PostgreSQL and Elasticsearch.
    Executes non-blockingly without impacting search response latency.
    """
    if not records:
        return

    # 1. PostgreSQL Ingestion
    conn = get_pg_conn()
    if conn:
        try:
            insert_sql = """
            INSERT INTO lexisearch.ecourts_live_records (
                cnr, party_name, petitioner, respondent, case_type, case_number, filing_number,
                case_status, state_code, state_name, district_code, district_name, court_code,
                court_name, court_type, registration_date, filing_date, next_hearing_date,
                decision_date, judges, petitioner_advocates, respondent_advocates, case_category,
                order_count, interim_order_count, judgment_count, hearing_count, has_orders,
                has_judgments, raw_payload, source_table
            ) VALUES (
                %(cnr)s, %(party_name)s, %(petitioner)s, %(respondent)s, %(case_type)s, %(case_number)s, %(filing_number)s,
                %(case_status)s, %(state_code)s, %(state_name)s, %(district_code)s, %(district_name)s, %(court_code)s,
                %(court_name)s, %(court_type)s, %(registration_date)s, %(filing_date)s, %(next_hearing_date)s,
                %(decision_date)s, %(judges)s, %(petitioner_advocates)s, %(respondent_advocates)s, %(case_category)s,
                %(order_count)s, %(interim_order_count)s, %(judgment_count)s, %(hearing_count)s, %(has_orders)s,
                %(has_judgments)s, %(raw_payload)s, %(source_table)s
            )
            ON CONFLICT (cnr) DO UPDATE SET
                party_name = EXCLUDED.party_name,
                next_hearing_date = EXCLUDED.next_hearing_date,
                case_status = EXCLUDED.case_status,
                raw_payload = EXCLUDED.raw_payload,
                updated_at = CURRENT_TIMESTAMP;
            """
            with conn.cursor() as cur:
                for rec in records:
                    params = dict(rec)
                    # Clean and sanitize types for Postgres schema compatibility
                    params["cnr"] = str(params.get("cnr") or "")[:64].strip()
                    if not params["cnr"]:
                        continue
                    params["party_name"] = str(params.get("party_name") or "Unknown")
                    params["case_type"] = str(params.get("case_type") or "UNKNOWN")[:64]
                    params["case_status"] = str(params.get("case_status") or "UNKNOWN")[:64]
                    params["state_code"] = str(params.get("state_code") or "")[:10]
                    params["state_name"] = str(params.get("state_name") or "")[:64]
                    params["district_name"] = str(params.get("district_name") or "")[:128]
                    params["court_code"] = str(params.get("court_code") or "")[:32]
                    params["court_type"] = str(params.get("court_type") or "district")[:32]

                    # Safe numeric/boolean conversions
                    d_code = params.get("district_code")
                    params["district_code"] = int(d_code) if d_code and str(d_code).isdigit() else None
                    params["order_count"] = _safe_int(params.get("order_count"), 0)
                    params["interim_order_count"] = _safe_int(params.get("interim_order_count"), 0)
                    params["judgment_count"] = _safe_int(params.get("judgment_count"), 0)
                    params["hearing_count"] = _safe_int(params.get("hearing_count"), 0)
                    params["has_orders"] = bool(params.get("has_orders", False))
                    params["has_judgments"] = bool(params.get("has_judgments", False))

                    # Safe array sanitization for TEXT[] columns
                    params["judges"] = _clean_str_list(params.get("judges"))
                    params["petitioner_advocates"] = _clean_str_list(params.get("petitioner_advocates"))
                    params["respondent_advocates"] = _clean_str_list(params.get("respondent_advocates"))

                    # Safe date normalization
                    params["registration_date"] = _clean_date(params.get("registration_date"))
                    params["filing_date"] = _clean_date(params.get("filing_date"))
                    params["next_hearing_date"] = _clean_date(params.get("next_hearing_date"))
                    params["decision_date"] = _clean_date(params.get("decision_date"))

                    # Convert raw_payload dict to JSON string for Postgres JSONB
                    if isinstance(params.get("raw_payload"), (dict, list)):
                        params["raw_payload"] = json.dumps(params["raw_payload"])
                    cur.execute(insert_sql, params)
                conn.commit()
            print(f"[Ingest Worker] Successfully persisted {len(records)} records to PostgreSQL 'lexisearch.ecourts_live_records'.")
        except Exception as pg_err:
            print(f"[Ingest Worker Error] PostgreSQL insert failed: {pg_err}")
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # 2. Elasticsearch Indexing
    try:
        es_url = os.getenv("ES_URL", "http://localhost:9201")
        es = Elasticsearch(es_url, request_timeout=10)
        if es.ping():
            for rec in records:
                es_doc = {
                    "cnr": rec["cnr"],
                    "case_number": rec.get("case_number", ""),
                    "case_type": rec.get("case_type", ""),
                    "case_status": rec.get("case_status", ""),
                    "court_type": rec.get("court_type", "district"),
                    "state_code": rec.get("state_code", ""),
                    "state_name": rec.get("state_name", ""),
                    "district_name": rec.get("district_name", ""),
                    "party_name": rec.get("party_name", ""),
                    "petitioner": rec.get("petitioner", ""),
                    "respondent": rec.get("respondent", ""),
                    "registration_date": _clean_date(rec.get("registration_date")),
                    "next_hearing_date": _clean_date(rec.get("next_hearing_date")),
                    "source_index": "ecourts_live_records",
                    "source_table": "ecourts_live_records"
                }
                # Index by CNR
                es.index(index="ecourts_live_records", id=rec["cnr"], document=es_doc)
            print(f"[Ingest Worker] Successfully indexed {len(records)} documents to Elasticsearch 'ecourts_live_records'.")
    except Exception as es_err:
        print(f"[Ingest Worker Note] Elasticsearch background indexing note: {es_err}")


def get_case_report_from_db(cnr: str) -> Optional[Dict[str, Any]]:
    """Retrieves cached full case report from lexisearch.case_reports by CNR."""
    clean_cnr = (cnr or "").strip().upper()
    if not clean_cnr:
        return None
    conn = get_pg_conn()
    if not conn:
        return None
    try:
        from psycopg2.extras import RealDictCursor
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT cnr, case_number, party_name, petitioner, respondent, case_type, case_status,
                       court_name, court_code, state_name, district_name, registration_date, filing_date,
                       next_hearing_date, decision_date, first_hearing_date, judges, petitioner_advocates,
                       respondent_advocates, acts_sections, hearing_history, orders, files, ai_analysis,
                       raw_report, created_at, updated_at
                FROM lexisearch.case_reports
                WHERE cnr = %s
                LIMIT 1;
            """, (clean_cnr,))
            row = cur.fetchone()
            if row:
                return dict(row)
            return None
    except Exception as e:
        print(f"[Ingest Worker] Error querying case_reports for {clean_cnr}: {e}")
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def save_case_report(cnr: str, api_response: Dict[str, Any]) -> bool:
    """
    Upserts a full case report from eCourts Partner API into lexisearch.case_reports,
    and synchronizes updated status/dates into lexisearch.ecourts_live_records.
    """
    clean_cnr = (cnr or "").strip().upper()
    if not clean_cnr or not api_response:
        return False

    data = api_response.get("data") or {}
    case_data = data.get("courtCaseData") or data.get("tribunalCaseData") or {}

    petitioners = _clean_str_list(case_data.get("petitioners") or [])
    respondents = _clean_str_list(case_data.get("respondents") or [])
    p_str = ", ".join(petitioners)
    r_str = ", ".join(respondents)
    party_name = f"{p_str} vs {r_str}" if (p_str and r_str) else (p_str or r_str or case_data.get("party_name") or "Unknown")

    judges = _clean_str_list(case_data.get("judges") or [])
    petitioner_advocates = _clean_str_list(case_data.get("petitionerAdvocates") or [])
    respondent_advocates = _clean_str_list(case_data.get("respondentAdvocates") or [])

    hearing_history = case_data.get("historyOfCaseHearings") or []
    orders = case_data.get("judgmentOrders") or case_data.get("interimOrders") or []
    files = data.get("files") or []
    acts_sections = case_data.get("actsAndSections") or []
    ai_analysis = data.get("caseAiAnalysis") or {}

    conn = get_pg_conn()
    if not conn:
        return False

    try:
        upsert_report_sql = """
        INSERT INTO lexisearch.case_reports (
            cnr, case_number, party_name, petitioner, respondent, case_type, case_status,
            court_name, court_code, state_name, district_name, registration_date, filing_date,
            next_hearing_date, decision_date, first_hearing_date, judges, petitioner_advocates,
            respondent_advocates, acts_sections, hearing_history, orders, files, ai_analysis,
            raw_report, updated_at
        ) VALUES (
            %(cnr)s, %(case_number)s, %(party_name)s, %(petitioner)s, %(respondent)s, %(case_type)s, %(case_status)s,
            %(court_name)s, %(court_code)s, %(state_name)s, %(district_name)s, %(registration_date)s, %(filing_date)s,
            %(next_hearing_date)s, %(decision_date)s, %(first_hearing_date)s, %(judges)s, %(petitioner_advocates)s,
            %(respondent_advocates)s, %(acts_sections)s, %(hearing_history)s, %(orders)s, %(files)s, %(ai_analysis)s,
            %(raw_report)s, CURRENT_TIMESTAMP
        )
        ON CONFLICT (cnr) DO UPDATE SET
            case_number = EXCLUDED.case_number,
            party_name = EXCLUDED.party_name,
            petitioner = EXCLUDED.petitioner,
            respondent = EXCLUDED.respondent,
            case_type = EXCLUDED.case_type,
            case_status = EXCLUDED.case_status,
            court_name = EXCLUDED.court_name,
            court_code = EXCLUDED.court_code,
            state_name = EXCLUDED.state_name,
            district_name = EXCLUDED.district_name,
            registration_date = EXCLUDED.registration_date,
            filing_date = EXCLUDED.filing_date,
            next_hearing_date = EXCLUDED.next_hearing_date,
            decision_date = EXCLUDED.decision_date,
            first_hearing_date = EXCLUDED.first_hearing_date,
            judges = EXCLUDED.judges,
            petitioner_advocates = EXCLUDED.petitioner_advocates,
            respondent_advocates = EXCLUDED.respondent_advocates,
            acts_sections = EXCLUDED.acts_sections,
            hearing_history = EXCLUDED.hearing_history,
            orders = EXCLUDED.orders,
            files = EXCLUDED.files,
            ai_analysis = EXCLUDED.ai_analysis,
            raw_report = EXCLUDED.raw_report,
            updated_at = CURRENT_TIMESTAMP;
        """

        params = {
            "cnr": clean_cnr,
            "case_number": str(case_data.get("caseNumber") or "")[:128] or None,
            "party_name": party_name,
            "petitioner": p_str or None,
            "respondent": r_str or None,
            "case_type": str(case_data.get("caseType") or "UNKNOWN")[:64],
            "case_status": str(case_data.get("caseStatus") or "UNKNOWN")[:64],
            "court_name": case_data.get("courtName") or None,
            "court_code": str(case_data.get("courtCode") or "")[:32] or None,
            "state_name": str(case_data.get("state") or "")[:64] or None,
            "district_name": str(case_data.get("district") or "")[:128] or None,
            "registration_date": _clean_date(case_data.get("registrationDate")),
            "filing_date": _clean_date(case_data.get("filingDate")),
            "next_hearing_date": _clean_date(case_data.get("nextHearingDate")),
            "decision_date": _clean_date(case_data.get("decisionDate")),
            "first_hearing_date": _clean_date(case_data.get("firstHearingDate")),
            "judges": judges,
            "petitioner_advocates": petitioner_advocates,
            "respondent_advocates": respondent_advocates,
            "acts_sections": json.dumps(acts_sections),
            "hearing_history": json.dumps(hearing_history),
            "orders": json.dumps(orders),
            "files": json.dumps(files),
            "ai_analysis": json.dumps(ai_analysis),
            "raw_report": json.dumps(api_response)
        }

        with conn.cursor() as cur:
            cur.execute(upsert_report_sql, params)

            # Layer 2: Sync updated status & dates into lexisearch.ecourts_live_records
            sync_live_sql = """
            UPDATE lexisearch.ecourts_live_records
            SET case_status = %s,
                next_hearing_date = %s,
                decision_date = %s,
                judges = %s,
                petitioner_advocates = %s,
                respondent_advocates = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE cnr = %s;
            """
            cur.execute(sync_live_sql, (
                params["case_status"],
                params["next_hearing_date"],
                params["decision_date"],
                judges,
                petitioner_advocates,
                respondent_advocates,
                clean_cnr
            ))

            conn.commit()
            print(f"[Ingest Worker] Successfully saved full case report & synced live record for CNR: {clean_cnr}")
            return True
    except Exception as e:
        print(f"[Ingest Worker] Error saving case report for {clean_cnr}: {e}")
        conn.rollback()
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass

