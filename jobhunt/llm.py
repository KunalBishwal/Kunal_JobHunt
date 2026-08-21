"""Two-stage LLM layer: cheap screen over everything, rich draft for the top few.

Cost lives here, so the two stages are deliberately lopsided:

  screen  batch ~8 jobs per call, JD truncated to ~1400 chars, cheapest model
  draft   one call per job, ~6000 chars of JD, best model, only for the top ~5

Both stages take an optional (provider, model) pair so tests can inject a stub
and so you can point screening at Groq while drafting stays on Claude.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from .fetch import Job

from .providers import LLMError, Provider, get_provider, resolve

_FENCE_OPEN = re.compile(r"^\s*```(?:json|JSON)?\s*", re.M)
_FENCE_CLOSE = re.compile(r"\s*```\s*$", re.M)

DRAFT_KEYS = ("fit_summary", "tailored_bullets", "gaps", "cover_note", "questions_to_ask")

# Output ceilings per stage. These are deliberately generous: reasoning models
# (Gemini 2.5+, and anything with thinking on) spend output tokens before the
# answer starts, so a ceiling sized to the visible answer gets consumed and you
# get truncated JSON instead of a result.
SCREEN_MAX_TOKENS = 4000
DRAFT_MAX_TOKENS = 8000
PROFILE_MAX_TOKENS = 4000


def parse_json(raw: str) -> Any:
    """Parse a model reply that is *supposed* to be JSON.

    Models wrap JSON in ```fences```, open with "Here is the JSON:", or return
    an object where you asked for an array. All three show up in practice, so
    this is deliberately forgiving: strip fences, try straight, then fall back
    to the outermost bracketed span.
    """
    if raw is None:
        raise ValueError("empty model reply")
    cleaned = _FENCE_CLOSE.sub("", _FENCE_OPEN.sub("", raw)).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    # Preamble before the payload, or trailing commentary after it.
    candidates = []
    for opener, closer in (("[", "]"), ("{", "}")):
        i, k = cleaned.find(opener), cleaned.rfind(closer)
        if i != -1 and k > i:
            candidates.append((i, cleaned[i:k + 1]))
    for _, blob in sorted(candidates):
        try:
            return json.loads(blob)
        except json.JSONDecodeError:
            continue
    raise ValueError(f"could not parse JSON from model reply: {cleaned[:300]!r}")


def _as_list(payload: Any) -> list[dict]:
    """Accept [ {...} ], { "jobs": [...] }, or a bare { ... }."""
    if isinstance(payload, list):
        return [p for p in payload if isinstance(p, dict)]
    if isinstance(payload, dict):
        for key in ("jobs", "results", "scores", "items"):
            inner = payload.get(key)
            if isinstance(inner, list):
                return [p for p in inner if isinstance(p, dict)]
        return [payload]
    raise ValueError(f"expected a JSON array of results, got {type(payload).__name__}")


# ---------------------------------------------------------------- profile ---

PROFILE_PROMPT = """Extract a structured job-search profile from this resume.

Return ONLY a JSON object, no prose, no markdown fences:
{
  "name": str,
  "current_title": str,
  "years_experience": number,
  "core_skills": [str],        // 10-20, most load-bearing first
  "domains": [str],            // e.g. "distributed systems", "CDN", "frontend"
  "notable_projects": [str],   // one line each, with impact if stated
  "education": str,
  "target_titles": [str],      // roles this person should realistically aim at
  "seniority": str             // intern | new-grad | junior | mid | senior | staff
}"""


def build_profile(resume_bytes: bytes | None = None, resume_text: str | None = None,
                  is_pdf: bool = False, provider: Provider | None = None,
                  model: str | None = None) -> dict:
    """Resume (PDF or text) -> profile.json. Uses the draft-stage model."""
    if provider is None or model is None:
        provider, model = resolve("draft")

    if is_pdf and resume_bytes:
        try:
            raw = provider.complete_document(
                model, PROFILE_PROMPT, resume_bytes, PROFILE_MAX_TOKENS)
        except LLMError as e:
            raise LLMError(
                f"{e}\nTip: export your resume to .txt and re-run, or set "
                f"DRAFT_PROVIDER=anthropic|gemini for PDF support."
            ) from e
    else:
        raw = provider.complete(
            model, "", f"{PROFILE_PROMPT}\n\n--- RESUME ---\n{resume_text or ''}",
            PROFILE_MAX_TOKENS, json_mode=True)

    profile = parse_json(raw)
    if not isinstance(profile, dict):
        raise ValueError("profile extraction did not return a JSON object")
    return profile


# ----------------------------------------------------------------- screen ---

SCREEN_SYSTEM = """You screen job postings for this candidate.
The candidate is a high-performing Computer Science student (9.65 CGPA) targeting:
- Software Engineering & SDE Internships (Primary target)
- Full Stack, AI/ML, Backend, and Frontend Developer Internships
- Entry-Level, Graduate, Junior, Associate, and SDE-1 roles (0-2 YoE) with strong stack overlap (Python, TypeScript, React, Next.js, Node.js, Java, Docker, AWS, PostgreSQL, PyTorch, AI/ML).

Score 0-10 on genuine fit:
  9-10  Direct internship or entry-level match with strong tech stack and domain overlap
  7-8   Good match (SDE-1, junior, early-career engineer, or startup role with high skill overlap)
  5-6   Stretch opportunity (1-2 YoE requested, but candidate has direct project/skill overlap)
  0-4   Severe mismatch: Senior/Staff/Lead/Principal (5+ YoE), non-technical role, or hard blocker

Do not reject roles just because the posting mentions 1-2 years of experience if the candidate's core stack matches. Internships and early-career engineering positions should score 7-10.
Penalise Staff/Senior/Lead roles (5+ YoE) and management roles to 0-3.

Return ONLY a JSON array, one object per job, no prose:
[{"job_id": str, "score": number, "reason": str}]
Echo `job_id` back exactly as given. `reason` is one sentence, max 20 words,
concrete about the deciding factor."""



def _complete_with_fallback(
    provider: Provider,
    model: str,
    system: str,
    user: str,
    max_tokens: int,
    json_mode: bool = True,
) -> str:
    """Execute LLM completion with automatic 429 retry and fallback to Groq or Gemini on failure."""
    try:
        return provider.complete(model, system, user, max_tokens, json_mode=json_mode)
    except Exception as primary_err:
        if getattr(provider, "name", "") in ("stub", "mock", "test"):
            raise primary_err

        # If primary hit a rate limit (429), wait 12 seconds and retry once
        if "429" in str(primary_err) or "rate" in str(primary_err).lower() or "quota" in str(primary_err).lower():
            try:
                print(f"  ! {getattr(provider, 'name', 'provider')} rate limited -> waiting 12s before retry...")
                time.sleep(12)
                return provider.complete(model, system, user, max_tokens, json_mode=json_mode)
            except Exception as retry_err:
                primary_err = retry_err

        # Check if backup provider (Groq or Gemini) is available
        backup_provider_name = None
        backup_model = None
        p_name = getattr(provider, "name", "")
        if p_name != "groq" and os.getenv("GROQ_API_KEY"):
            backup_provider_name = "groq"
            backup_model = "qwen/qwen3.6-27b"
        elif p_name != "gemini" and os.getenv("GEMINI_API_KEY"):
            backup_provider_name = "gemini"
            backup_model = "gemini-3.6-flash"

        if backup_provider_name and backup_model:
            try:
                print(f"  ! {p_name} failed ({primary_err}) -> retrying with backup provider {backup_provider_name}/{backup_model}")
                backup_p = get_provider(backup_provider_name)
                return backup_p.complete(backup_model, system, user, max_tokens, json_mode=json_mode)
            except Exception as backup_err:
                raise LLMError(f"Primary ({p_name}: {primary_err}) and backup ({backup_provider_name}: {backup_err}) both failed") from backup_err
        raise primary_err



def screen(jobs: list[Job], profile: dict, batch_size: int = 8, jd_chars: int = 1400,
           provider: Provider | None = None, model: str | None = None) -> list[Job]:
    """Stage 1: score every surviving job. Mutates and returns `jobs`.

    A batch that fails to parse logs a warning and is skipped — one bad reply
    must not take down the whole run.
    """
    if provider is None or model is None:
        provider, model = resolve("screen")
    batch_size = max(1, int(batch_size))
    profile_blob = json.dumps(profile, ensure_ascii=False)

    total_batches = (len(jobs) + batch_size - 1) // batch_size
    for idx, start in enumerate(range(0, len(jobs), batch_size)):
        batch = jobs[start:start + batch_size]
        payload = [{
            "job_id": j.job_id,
            "company": j.company,
            "title": j.title,
            "location": j.location,
            "description": j.description[:jd_chars],
        } for j in batch]

        n = idx + 1
        try:
            raw = _complete_with_fallback(
                provider, model, SCREEN_SYSTEM,
                f"CANDIDATE PROFILE:\n{profile_blob}\n\n"
                f"JOBS:\n{json.dumps(payload, ensure_ascii=False)}",
                SCREEN_MAX_TOKENS, json_mode=True,
            )
            results = {}
            for r in _as_list(parse_json(raw)):
                jid = r.get("job_id")
                if jid:
                    results[str(jid)] = r
        except (LLMError, ValueError, KeyError, TypeError) as e:
            print(f"  ! screen batch {n} failed ({type(e).__name__}: {e}) — skipping")
            continue

        for j in batch:
            r = results.get(j.job_id)
            if not r:
                continue
            try:
                j.score = max(0.0, min(10.0, float(r.get("score", 0))))
            except (TypeError, ValueError):
                j.score = 0.0
            j.reason = str(r.get("reason", "")).strip()

        print(f"  screened {min(start + batch_size, len(jobs))}/{len(jobs)}")
        if idx < total_batches - 1 and getattr(provider, "name", "") not in ("stub", "mock", "test"):
            time.sleep(2.5)

    return jobs


# ------------------------------------------------------------------ draft ---

DRAFT_SYSTEM = """You prepare an application kit for one job.

Hard rule: never invent experience. Every claim must trace to something in the
candidate profile. If the profile does not support a claim, it goes in `gaps`,
not in a bullet.

Return ONLY a JSON object, no prose:
{
  "fit_summary": str,          // 2 sentences: why this is worth their time
  "tailored_bullets": [str],   // 3-4 resume bullets rewritten for THIS job,
                               // using only real experience from the profile,
                               // each with a concrete artefact or number
  "gaps": [str],               // 1-3 honest gaps, each with how to address it
  "cover_note": str,           // 120-160 words. Plain. No "I am writing to
                               // express my interest", no "I am excited to",
                               // no flattery about the company's mission.
                               // Open with a concrete reason they fit this role.
  "questions_to_ask": [str]    // 2 sharp questions that show they read the JD
}"""


def draft(jobs: list[Job], profile: dict, jd_chars: int = 6000,
          provider: Provider | None = None, model: str | None = None) -> list[Job]:
    """Stage 2: full kit for the shortlist. One call per job, best model."""
    if provider is None or model is None:
        provider, model = resolve("draft")
    profile_blob = json.dumps(profile, ensure_ascii=False)

    for j in jobs:
        try:
            raw = _complete_with_fallback(
                provider, model, DRAFT_SYSTEM,
                f"CANDIDATE PROFILE:\n{profile_blob}\n\n"
                f"JOB: {j.title} at {j.company} ({j.location or 'location not stated'})\n"
                f"URL: {j.url}\n\n{j.description[:jd_chars]}",
                DRAFT_MAX_TOKENS, json_mode=True,
            )
            kit = parse_json(raw)
            if not isinstance(kit, dict):
                raise ValueError("draft did not return a JSON object")
            # Normalise so the digest template never has to guess.
            j.draft = {
                "fit_summary": str(kit.get("fit_summary") or ""),
                "tailored_bullets": [str(b) for b in (kit.get("tailored_bullets") or [])],
                "gaps": [str(g) for g in (kit.get("gaps") or [])],
                "cover_note": str(kit.get("cover_note") or ""),
                "questions_to_ask": [str(q) for q in (kit.get("questions_to_ask") or [])],
            }
            print(f"  drafted {j.title} @ {j.company}")
            if getattr(provider, "name", "") not in ("stub", "mock", "test"):
                time.sleep(2.0)
        except (LLMError, ValueError, KeyError, TypeError) as e:

            print(f"  ! draft failed for {j.job_id} ({type(e).__name__}: {e})")
            j.draft = {k: ("" if k in ("fit_summary", "cover_note") else []) for k in DRAFT_KEYS}

    return jobs


# ------------------------------------------------- offline scorer (no API) ---

def keyword_screen(jobs: list[Job], profile: dict, **_) -> list[Job]:
    """DEV ONLY. Token-overlap stand-in so the pipeline runs with no API key.

    This is not a matcher. It cannot tell a Staff role from a new-grad one and
    it has no idea what the words mean. It exists so `--mock --scorer keyword`
    proves the plumbing end-to-end on a laptop with no secrets configured.
    Never ship a digest built from these scores.
    """
    skills = {s.lower() for s in profile.get("core_skills", []) if s}
    titles = [t.lower() for t in profile.get("target_titles", []) if t]
    role_tokens = {"software", "engineer", "sde", "developer", "backend", "frontend", "full", "stack", "ai", "intern"}

    for j in jobs:
        blob = f"{j.title} {j.description}".lower()
        title_lower = j.title.lower()
        hits = sorted(s for s in skills if s in blob)
        overlap = len(hits) / max(len(skills), 1)

        # Title bonus: exact target title or token match
        if any(t in title_lower for t in titles):
            title_bonus = 3.5
        elif any(token in title_lower for token in role_tokens):
            title_bonus = 2.5
        else:
            title_bonus = 0.0

        j.score = round(min(10.0, max(0.0, 4.0 + (overlap * 10) + title_bonus)), 1)
        j.reason = ("[keyword stub] matched: " + ", ".join(hits[:5])) if hits \
            else "[keyword stub] role title matched target domains"
    return jobs
