"""Unit tests for the 10-job selection engine, smart backfill, company diversity, and anti-fabrication."""
from __future__ import annotations

import pytest

from jobhunt.fetch import Job
from jobhunt.selector import compute_rank_score, select_daily_matches


def _job(
    jid: str,
    company: str = "Company",
    title: str = "Software Engineer Intern",
    score: float = 8.0,
    priority: str = "A",
    posted_at: str | None = "2026-08-20T00:00:00Z",
    desc: str = "Python, React, Class of 2027",
) -> Job:
    return Job(
        job_id=jid,
        ats="greenhouse",
        company=company,
        title=title,
        location="Bengaluru",
        url=f"https://example.com/{jid}",
        description=desc,
        score=score,
        company_priority=priority,
        posted_at=posted_at,
    )


def test_select_exactly_ten_jobs_when_fifteen_available():
    candidates = [_job(f"j_{i}", company=f"Company_{i}", score=7.5 + (i * 0.1)) for i in range(15)]
    cfg = {
        "target_jobs_per_digest": 10,
        "preferred_score_threshold": 7.0,
        "fallback_score_threshold": 6.0,
        "hard_min_score": 5.5,
        "max_jobs_per_company": 2,
    }
    selected, stats = select_daily_matches(candidates, cfg)
    assert len(selected) == 10
    assert stats["selected_count"] == 10
    assert stats["preferred_matches"] == 10


def test_smart_backfill_uses_fallback_threshold_to_reach_ten():
    # 8 preferred jobs (>= 7.0) and 5 fallback jobs (between 6.0 and 6.9)
    preferred = [_job(f"pref_{i}", company=f"CompanyP_{i}", score=7.5) for i in range(8)]
    fallback = [_job(f"fall_{i}", company=f"CompanyF_{i}", score=6.5) for i in range(5)]
    candidates = preferred + fallback

    cfg = {
        "target_jobs_per_digest": 10,
        "preferred_score_threshold": 7.0,
        "fallback_score_threshold": 6.0,
        "hard_min_score": 5.5,
        "max_jobs_per_company": 2,
    }
    selected, stats = select_daily_matches(candidates, cfg)
    assert len(selected) == 10
    assert stats["preferred_matches"] == 8
    assert stats["fallback_matches"] == 2


def test_insufficient_supply_never_fabricates_jobs():
    # Only 6 legitimate jobs above hard_min_score
    candidates = [_job(f"job_{i}", score=6.2) for i in range(6)]
    cfg = {
        "target_jobs_per_digest": 10,
        "preferred_score_threshold": 7.0,
        "fallback_score_threshold": 6.0,
        "hard_min_score": 5.5,
        "max_jobs_per_company": 2,
    }
    selected, stats = select_daily_matches(candidates, cfg)
    assert len(selected) == 6
    assert all(j.score >= 5.5 for j in selected)


def test_company_diversity_limits_per_company_jobs():
    # 10 Amazon jobs and 5 Microsoft jobs and 3 Google jobs
    amazon = [_job(f"amz_{i}", company="Amazon", score=8.5) for i in range(10)]
    microsoft = [_job(f"msft_{i}", company="Microsoft", score=8.4) for i in range(5)]
    google = [_job(f"goog_{i}", company="Google", score=8.3) for i in range(3)]
    candidates = amazon + microsoft + google

    cfg = {
        "target_jobs_per_digest": 10,
        "preferred_score_threshold": 7.0,
        "fallback_score_threshold": 6.0,
        "hard_min_score": 5.5,
        "max_jobs_per_company": 2,
    }
    selected, stats = select_daily_matches(candidates, cfg)
    counts = {}
    for j in selected:
        counts[j.company] = counts.get(j.company, 0) + 1

    # In pass 1 with 3 companies (2+2+2 = 6) and relaxing for remaining high scorers
    assert counts["Amazon"] <= 6
    assert counts["Microsoft"] <= 5
    assert counts["Google"] <= 3


def test_ranking_prefers_s_tier_and_fresh_jobs():
    j_s_tier = _job("j1", company="Alpha", priority="S", score=8.0, posted_at="2026-08-20T00:00:00Z")
    j_b_tier = _job("j2", company="Beta", priority="B", score=8.0, posted_at="2026-07-20T00:00:00Z")

    rank_s = compute_rank_score(j_s_tier)
    rank_b = compute_rank_score(j_b_tier)
    assert rank_s > rank_b
