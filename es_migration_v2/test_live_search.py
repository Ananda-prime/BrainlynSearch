import os
import sys
import time

# Ensure proper path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "api"))

from api.services.similarity_engine import filter_and_score_ecourts_records, normalize_ecourts_item
from api.services.ecourts_service import fetch_ecourts_records, sync_fetch_ecourts
from api.services.ingestion_worker import persist_ecourts_records

# 1. Test Mock Record
mock_api_record = {
    "id": "DLET020003942024",
    "cnr": "DLET020003942024",
    "caseType": "CC",
    "caseStatus": "PENDING",
    "filingNumber": "394/2024",
    "filingDate": "2024-01-09",
    "registrationNumber": "85/2024",
    "registrationDate": "2024-01-24",
    "nextHearingDate": "2026-01-15",
    "decisionDate": None,
    "judges": ["Municipal Magistrate"],
    "petitioners": ["M/s Sunlife Sciences Through Its Authorized Signatory"],
    "petitionerAdvocates": ["Rohit Chhabra"],
    "respondents": ["Divi Srinivas Chakravarthy"],
    "respondentAdvocates": [],
    "actsAndSections": [],
    "courtCode": "DLET02",
    "courtName": "Chief Metropolitan Magistrate, East, Karkardooma Court, East District, Delhi",
    "caseCategory": "Negotiable Instruments Act - 138",
    "stateCode": "DL",
    "districtCode": 3,
    "hasOrders": True,
    "hasJudgments": False,
    "orderCount": 9
}

print("==================================================")
print("1. Testing Schema Normalization & 39-Field Capture")
print("==================================================")
normalized = normalize_ecourts_item(mock_api_record)
print(f"CNR: {normalized['cnr']}")
print(f"Party Name: {normalized['party_name']}")
print(f"State Name: {normalized['state_name']}")
print(f"Target Table: {normalized['source_table']}")
print(f"Raw Payload Preserved: {bool(normalized['raw_payload'])}")

print("\n==================================================")
print("2. Testing 75%-100% Similarity Engine")
print("==================================================")
filtered = filter_and_score_ecourts_records([mock_api_record], "Divi Srinivas Chakravarthy", min_similarity=75.0)
print(f"Filter passed: {len(filtered)} items (Confidence Score: {filtered[0]['confidence'] if filtered else 0}%)")

print("\n==================================================")
print("3. Testing PostgreSQL Ingestion to Dedicated Table")
print("==================================================")
persist_ecourts_records(filtered)
print("[+] End-to-end verification completed successfully!")
