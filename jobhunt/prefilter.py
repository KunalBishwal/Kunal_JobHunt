"""Deterministic filter that runs BEFORE any LLM call.

This is the whole cost story: ~2000 raw jobs -> ~40 candidates for ~0 rupees,
so Claude only ever reads jobs that already passed title + location + freshness.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from .fetch import Job

REMOTE_HINTS = ("remote", "anywhere", "work from home", "wfh", "distributed")

# If a location string contains "remote" but ALSO contains one of these
# country/region tokens and does NOT contain "india", the role is for a specific
# non-India geography and should be dropped.
NON_INDIA_GEO = re.compile(
    r"\b(?:united states|usa?\b|u\.s\.?|canada|uk\b|united kingdom|europe|eu\b|"
    r"emea|latam|apac|australia|germany|france|spain|ireland|israel|japan|"
    r"singapore|estonia|netherlands|switzerland|brazil|mexico|poland|"
    r"czech|romania|portugal|south korea|turkey|argentina|colombia|"
    r"chile|peru|belgium|austria|sweden|denmark|norway|finland|italy|"
    r"new zealand|south africa|philippines|taiwan|hong kong|china\b|"
    r"vietnam|thailand|indonesia|malaysia)\b",
    re.IGNORECASE,
)


def _any_match(patterns: list[str], text: str) -> bool:
    return any(re.search(p, text, re.I) for p in patterns)


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    v = value.replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.fromisoformat(v) if fmt is None else datetime.strptime(v, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _is_india_accessible_remote(location: str) -> bool:
    """Return True if a 'remote' location is India-accessible.

    Rules:
    - "Remote" alone (no country qualifier) → True
    - "Remote - India", "Remote, Bengaluru" → True
    - "Remote - US", "Remote - Estonia" → False
    """
    loc_lower = location.lower()
    if "india" in loc_lower:
        return True
    # If no specific non-India geography is mentioned, assume global remote
    if NON_INDIA_GEO.search(loc_lower):
        return False
    return True


def prefilter(jobs: list[Job], cfg: dict) -> list[Job]:
    inc = cfg.get("include_titles") or [r"."]
    exc = cfg.get("exclude_titles") or []
    locs = [l.lower() for l in (cfg.get("locations") or [])]
    allow_remote = bool(cfg.get("allow_remote", True))
    max_age = cfg.get("max_age_days")
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age) if max_age else None

    kept, stats = [], {"title": 0, "location": 0, "age": 0}
    for j in jobs:
        if not _any_match(inc, j.title) or (exc and _any_match(exc, j.title)):
            stats["title"] += 1
            continue

        if locs:
            hay = f"{j.location} {j.title}".lower()
            is_remote = allow_remote and any(h in hay for h in REMOTE_HINTS)
            if is_remote:
                # Further check: is the remote role India-accessible?
                if not _is_india_accessible_remote(j.location):
                    stats["location"] += 1
                    continue
            elif not any(l in hay for l in locs):
                stats["location"] += 1
                continue

        if cutoff:
            posted = _parse_date(j.posted_at)
            if posted and posted < cutoff:
                stats["age"] += 1
                continue

        kept.append(j)

    print(f"  prefilter: {len(jobs)} -> {len(kept)} "
          f"(dropped title={stats['title']} location={stats['location']} stale={stats['age']})")
    return kept

