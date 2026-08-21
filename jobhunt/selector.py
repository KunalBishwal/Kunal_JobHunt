"""Selection engine for the daily job digest.

Implements:
- 10-job daily target
- Smart backfill (preferred threshold -> fallback threshold -> hard min floor)
- Company diversity constraint (e.g. max 2 per company)
- Role tier & 2027 eligibility ranking bonuses
- Strict anti-fabrication guarantee (never invent jobs to fill quotas)
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from .fetch import Job
from .prefilter import _parse_date

TIER1_TITLES = (
    r"\bsoftware engineer intern\b",
    r"\bsde intern\b",
    r"\bsoftware developer intern\b",
    r"\bsoftware engineer\b",
    r"\bsde\b",
    r"\bsde-?1\b",
    r"\bsoftware engineer i\b",
    r"\bfull[- ]stack\b",
    r"\bbackend\b",
)

TIER2_TITLES = (
    r"\bai engineer\b",
    r"\bapplied ai\b",
    r"\bai software engineer\b",
    r"\bmachine learning\b",
    r"\bml engineer\b",
    r"\bgenai\b",
    r"\bgenerative ai\b",
    r"\bllm\b",
)

TIER3_TITLES = (
    r"\bfrontend\b",
    r"\bfront[- ]end\b",
    r"\bassociate software engineer\b",
    r"\bjunior software engineer\b",
    r"\bgraduate software engineer\b",
)


def _freshness_bonus(posted_at: str | None) -> float:
    dt = _parse_date(posted_at)
    if not dt:
        return 0.05
    now = datetime.now(timezone.utc)
    age_days = (now - dt).total_seconds() / 86400.0
    if age_days <= 3:
        return 0.35
    elif age_days <= 7:
        return 0.20
    elif age_days <= 14:
        return 0.10
    elif age_days <= 30:
        return 0.02
    return 0.0


def _priority_bonus(priority: str | None) -> float:
    p = (priority or "").upper().strip()
    if p == "S":
        return 0.30
    elif p == "A":
        return 0.15
    return 0.0


def _eligibility_bonus(job: Job) -> float:
    bonus = 0.0
    text = f"{job.title} {job.description[:1000]}".lower()

    # 2027 / intern eligibility
    if any(k in text for k in ("2027", "penultimate", "intern", "internship", "class of 2027", "graduating in 2027")):
        bonus += 0.25

    # Role tier match
    title = job.title.lower()
    if any(re.search(pat, title, re.I) for pat in TIER1_TITLES):
        bonus += 0.20
    elif any(re.search(pat, title, re.I) for pat in TIER2_TITLES):
        bonus += 0.15
    elif any(re.search(pat, title, re.I) for pat in TIER3_TITLES):
        bonus += 0.10

    return bonus


def compute_rank_score(job: Job) -> float:
    """Composite ranking score balancing technical fit, company tier, freshness, and 2027 eligibility."""
    base_score = float(job.score if job.score is not None else 0.0)
    priority = _priority_bonus(job.company_priority)
    freshness = _freshness_bonus(job.posted_at)
    eligibility = _eligibility_bonus(job)
    return round(base_score + priority + freshness + eligibility, 3)


def select_daily_matches(
    candidates: list[Job],
    cfg: dict[str, Any],
) -> tuple[list[Job], dict[str, Any]]:
    """Select the top N jobs for the daily digest.

    Rules:
    1. Filter out jobs below hard minimum floor (hard_min_score).
    2. Sort by composite rank descending.
    3. Pass 1: Select up to target_jobs_per_digest with score >= preferred_score_threshold
       enforcing max_jobs_per_company.
    4. Pass 2: If under target, backfill with score >= fallback_score_threshold.
    5. Pass 3: If under target, backfill with score >= hard_min_score.
    6. Pass 4: If still under target and supply is limited, relax company cap for remaining
       valid jobs with score >= hard_min_score.
    7. Anti-fabrication: Never invent jobs. If fewer genuine jobs exist, return exactly those.
    """
    target = int(cfg.get("target_jobs_per_digest", 10))
    preferred_threshold = float(cfg.get("preferred_score_threshold", cfg.get("score_threshold", 7.0)))
    fallback_threshold = float(cfg.get("fallback_score_threshold", 6.0))
    hard_min = float(cfg.get("hard_min_score", 5.5))
    max_per_company = int(cfg.get("max_jobs_per_company", 2))

    # Only consider jobs that cleared the hard minimum score
    eligible = [j for j in candidates if (j.score if j.score is not None else 0.0) >= hard_min]
    ranked = sorted(eligible, key=compute_rank_score, reverse=True)

    selected: list[Job] = []
    selected_ids: set[str] = set()
    company_counts: Counter[str] = Counter()

    preferred_matches = 0
    fallback_matches = 0

    # Pass 1: Preferred threshold
    for j in ranked:
        if (j.score or 0.0) >= preferred_threshold:
            if company_counts[j.company] < max_per_company:
                selected.append(j)
                selected_ids.add(j.job_id)
                company_counts[j.company] += 1
                preferred_matches += 1
                if len(selected) >= target:
                    break

    # Pass 2: Fallback threshold
    if len(selected) < target:
        for j in ranked:
            if j.job_id not in selected_ids and (j.score or 0.0) >= fallback_threshold:
                if company_counts[j.company] < max_per_company:
                    selected.append(j)
                    selected_ids.add(j.job_id)
                    company_counts[j.company] += 1
                    fallback_matches += 1
                    if len(selected) >= target:
                        break

    # Pass 3: Hard minimum threshold
    if len(selected) < target:
        for j in ranked:
            if j.job_id not in selected_ids and (j.score or 0.0) >= hard_min:
                if company_counts[j.company] < max_per_company:
                    selected.append(j)
                    selected_ids.add(j.job_id)
                    company_counts[j.company] += 1
                    if len(selected) >= target:
                        break

    # Pass 4: Relax company diversity cap if legitimate candidate supply is tight
    if len(selected) < target:
        for j in ranked:
            if j.job_id not in selected_ids and (j.score or 0.0) >= hard_min:
                selected.append(j)
                selected_ids.add(j.job_id)
                company_counts[j.company] += 1
                if len(selected) >= target:
                    break

    stats = {
        "target": target,
        "total_candidates": len(candidates),
        "eligible_above_floor": len(eligible),
        "preferred_matches": preferred_matches,
        "fallback_matches": fallback_matches,
        "selected_count": len(selected),
        "distinct_companies": len(company_counts),
    }
    return selected, stats
