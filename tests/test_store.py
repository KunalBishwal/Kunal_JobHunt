"""Unit tests for the Store state machine, legacy migration, deduplication, and notification tracking."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobhunt.fetch import Job
from jobhunt.store import Store


@pytest.fixture
def temp_store(tmp_path):
    store_file = tmp_path / "test_seen.json"
    return Store(store_file)


def _make_job(job_id: str, company: str = "Acme", title: str = "SDE Intern", score: float | None = None) -> Job:
    return Job(
        job_id=job_id,
        ats="greenhouse",
        company=company,
        title=title,
        location="Bengaluru",
        url="https://example.com",
        description="Python, React",
        score=score,
        reason="Good fit",
    )


def test_legacy_seen_json_migration(tmp_path):
    legacy_data = {
        "greenhouse:acme:1": {
            "first_seen": "2026-08-20T10:00:00+00:00",
            "company": "Acme",
            "title": "SDE Intern",
            "location": "Bengaluru",
            "url": "https://example.com/1",
            "score": 8.5,
            "reason": "Strong match",
            "emailed": True,
            "applied": False,
            "applied_on": None,
        },
        "greenhouse:beta:2": {
            "first_seen": "2026-08-20T10:00:00+00:00",
            "company": "Beta",
            "title": "Backend Engineer",
            "location": "Remote",
            "url": "https://example.com/2",
            "score": 6.2,
            "reason": "Plausible",
            "emailed": False,
            "applied": False,
            "applied_on": None,
        }
    }
    store_path = tmp_path / "legacy_seen.json"
    store_path.write_text(json.dumps(legacy_data), encoding="utf-8")

    store = Store(store_path)
    # Entry 1 should be notified
    assert store.is_notified("greenhouse:acme:1") is True
    assert store.data["greenhouse:acme:1"]["status"] == "notified"

    # Entry 2 should NOT be notified
    assert store.is_notified("greenhouse:beta:2") is False
    assert store.data["greenhouse:beta:2"]["status"] == "scored"

    # Unnotified filter should keep Entry 2
    j2 = _make_job("greenhouse:beta:2")
    assert store.unnotified([j2]) == [j2]


def test_discovered_vs_notified_state_transition(temp_store):
    # 20 jobs discovered
    jobs = [_make_job(f"gh:comp:{i}", score=7.0 + (i * 0.1)) for i in range(20)]
    temp_store.record_discovered(jobs)

    assert temp_store.stats()["tracked"] == 20
    assert temp_store.stats()["discovered"] == 20
    assert temp_store.stats()["notified"] == 0

    # Top 10 selected and notified
    notified_jobs = jobs[10:]
    temp_store.record_notified(notified_jobs, emailed=True)

    assert temp_store.stats()["notified"] == 10

    # On next run, unnotified should return the remaining 10
    remaining = temp_store.unnotified(jobs)
    assert len(remaining) == 10
    assert [j.job_id for j in remaining] == [f"gh:comp:{i}" for i in range(10)]


def test_mark_applied(temp_store):
    j = _make_job("gh:alpha:100", score=9.0)
    temp_store.record_discovered([j])
    temp_store.record_notified([j], emailed=True)

    assert temp_store.data["gh:alpha:100"]["applied"] is False
    assert temp_store.mark_applied("gh:alpha:100") is True

    assert temp_store.data["gh:alpha:100"]["applied"] is True
    assert temp_store.data["gh:alpha:100"]["status"] == "applied"
    assert temp_store.data["gh:alpha:100"]["applied_on"] is not None

    # Applied job is not included in unnotified pool
    assert temp_store.unnotified([j]) == []


def test_export_csv_contains_all_required_columns(temp_store, tmp_path):
    j = _make_job("gh:zeta:500", company="Zeta", score=8.0)
    temp_store.record_discovered([j])
    temp_store.record_notified([j], emailed=True)

    csv_path = tmp_path / "out_tracker.csv"
    exported = temp_store.export_csv(csv_path)
    assert exported.exists()

    content = exported.read_text(encoding="utf-8")
    expected_headers = [
        "job_id", "company", "title", "location", "score",
        "reason", "first_seen", "last_seen", "notified",
        "notified_at", "applied", "applied_on", "url", "source"
    ]
    for h in expected_headers:
        assert h in content
