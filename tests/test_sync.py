"""Unit tests for PDF company extraction, sync, and ATS discovery."""
from __future__ import annotations

from pathlib import Path

import yaml

from jobhunt.sync import (
    detect_ats_from_url,
    extract_companies_from_pdf,
    normalize_name,
    sync_companies,
)

PDF_FILE = Path(__file__).resolve().parent.parent / "Kunal_Bishwal_2026_Master_Company_Target_List.pdf"


def test_pdf_extraction_extracts_353_companies():
    assert PDF_FILE.exists(), f"PDF not found at {PDF_FILE}"
    companies = extract_companies_from_pdf(PDF_FILE)
    assert len(companies) == 353

    # Check required fields
    for c in companies:
        assert c["name"], f"missing name: {c}"
        assert c["category"], f"missing category: {c}"
        assert c["priority"] in ("S", "A", "B"), f"invalid priority: {c}"
        assert c["career_url"].startswith("http"), f"invalid career_url: {c}"


def test_sync_preserves_verified_slugs(tmp_path):
    existing_yaml = tmp_path / "companies.yaml"
    initial_content = {
        "companies": [
            {"name": "Razorpay", "ats": "greenhouse", "slug": "razorpaysoftwareprivatelimited", "active": True},
            {"name": "Postman", "ats": "greenhouse", "slug": "postman", "active": True},
            {"name": "Sarvam AI", "ats": "ashby", "slug": "sarvam", "active": True},
        ]
    }
    existing_yaml.write_text(yaml.dump(initial_content), encoding="utf-8")

    summary = sync_companies(PDF_FILE, existing_yaml)
    assert summary["pdf_companies"] == 353
    assert summary["already_present"] >= 3

    merged_data = yaml.safe_load(existing_yaml.read_text(encoding="utf-8"))
    comps = {normalize_name(c["name"]): c for c in merged_data["companies"]}

    # Verified entries must have their verified ATS and slugs preserved
    assert comps["razorpay"]["slug"] == "razorpaysoftwareprivatelimited"
    assert comps["razorpay"]["ats"] == "greenhouse"

    assert comps["postman"]["slug"] == "postman"
    assert comps["postman"]["ats"] == "greenhouse"

    assert comps["sarvam ai"]["slug"] == "sarvam"
    assert comps["sarvam ai"]["ats"] == "ashby"


def test_detect_ats_from_url():
    ats, slug = detect_ats_from_url("https://boards.greenhouse.io/postman")
    assert ats == "greenhouse"
    assert slug == "postman"

    ats, slug = detect_ats_from_url("https://jobs.lever.co/aidash")
    assert ats == "lever"
    assert slug == "aidash"

    ats, slug = detect_ats_from_url("https://jobs.ashbyhq.com/sarvam")
    assert ats == "ashby"
    assert slug == "sarvam"

    ats, slug = detect_ats_from_url("https://jobs.smartrecruiters.com/Freshworks")
    assert ats == "smartrecruiters"
    assert slug == "Freshworks"

    ats, slug = detect_ats_from_url("https://example.com/careers")
    assert ats == "unknown"
    assert slug is None
