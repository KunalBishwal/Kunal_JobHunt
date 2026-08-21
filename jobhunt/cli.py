"""jobhunt CLI: profile -> fetch -> prefilter -> dedupe -> screen -> select -> draft -> digest -> mail.

The agent finds, filters, ranks, selects up to 10 best jobs, and drafts application notes.
A human reviews the daily digest, tailors the note, and submits the application.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import yaml

from . import digest as digest_mod
from . import llm, mailer
from .fetch import fetch_all
from .mock import fetch_all_mock
from .prefilter import prefilter
from .providers import LLMError, resolve
from .selector import select_daily_matches
from .store import Store
from .sync import discover_ats_for_companies, sync_companies

ROOT = Path(__file__).resolve().parent.parent


def _load_env(path: str = ".env") -> None:
    """Minimal .env reader without requiring python-dotenv."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _cfg(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"config not found: {p}  (run from the project root)")
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def _load_profile(cfg: dict, allow_sample: bool) -> dict | None:
    path = Path(cfg.get("profile_file", "profile.json"))
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))

    sample = ROOT / "profile.example.json"
    if allow_sample and sample.exists():
        print(f"  ! {path} missing — using {sample.name} for this dry run.")
        print("    Build the real one: python -m jobhunt profile --resume resume.pdf")
        return json.loads(sample.read_text(encoding="utf-8"))

    print(f"missing {path} — run `python -m jobhunt profile --resume <file>` first")
    return None


# ------------------------------------------------------------------ profile --
def cmd_profile(args) -> int:
    src = Path(args.resume)
    if not src.exists():
        print(f"resume not found: {src}")
        return 1
    is_pdf = src.suffix.lower() == ".pdf"

    try:
        provider, model = resolve("draft")
        print(f"reading {src.name} via {provider.name}/{model} ...")
        profile = llm.build_profile(
            resume_bytes=src.read_bytes() if is_pdf else None,
            resume_text=None if is_pdf else src.read_text(encoding="utf-8", errors="replace"),
            is_pdf=is_pdf, provider=provider, model=model,
        )
    except (LLMError, ValueError) as e:
        print(f"profile extraction failed: {e}")
        return 1

    Path(args.out).write_text(json.dumps(profile, indent=2, ensure_ascii=False),
                              encoding="utf-8")
    print(f"wrote {args.out}\n")
    print(json.dumps(profile, indent=2, ensure_ascii=False)[:900])
    return 0


# ----------------------------------------------------------- companies sync --
def cmd_companies_sync(args) -> int:
    pdf_path = Path(args.pdf)
    yaml_path = Path(args.out or "companies.yaml")
    if not pdf_path.exists():
        print(f"PDF not found: {pdf_path}")
        return 1

    print(f"Syncing companies from {pdf_path.name} into {yaml_path.name} ...")
    summary = sync_companies(pdf_path, yaml_path)
    dups_removed = summary["pdf_companies"] - summary["total_in_yaml"]

    print("\nCompany Universe Summary:")
    print(f"  PDF companies:          {summary['pdf_companies']}")
    print(f"  Existing companies:     {summary['existing_companies']}")
    print(f"  Already present:        {summary['already_present']}")
    print(f"  New companies added:    {summary['new_added']}")
    print(f"  Duplicates removed:     {max(0, dups_removed)}")
    print(f"\nTotal companies in {yaml_path.name}: {summary['total_in_yaml']}")
    return 0


def cmd_discover_ats(args) -> int:
    yaml_path = Path(args.config_yaml or "companies.yaml")
    results = discover_ats_for_companies(yaml_path, limit=args.limit)
    print(f"\nDiscovered ATS Status ({len(results)} companies):")
    for r in results[:40]:
        slug_info = f"/ {r['slug']}" if r.get("slug") else ""
        print(f"  {r['name']:<28} -> {r['ats']:<15} {slug_info:<25} ({r['status']})")
    if len(results) > 40:
        print(f"  ... and {len(results) - 40} more companies.")
    return 0


# ---------------------------------------------------------------------- run --
def cmd_run(args) -> int:
    cfg = _cfg(args.config)
    profile = _load_profile(cfg, allow_sample=args.mock)
    if profile is None:
        return 1
    store = Store(cfg.get("seen_file", "seen.json"))
    filters = cfg.get("filters", {}) or {}

    # ---- 1. fetch
    print("\n[1/6] fetching boards")
    if args.mock:
        jobs = fetch_all_mock()
        total_companies_monitored = 4
    else:
        companies_file = cfg.get("companies_file", "companies.yaml")
        companies = _cfg(companies_file).get("companies") or []
        if not companies:
            print(f"{companies_file} has no entries. Run `python -m jobhunt companies sync` first.")
            return 1
        total_companies_monitored = len(companies)
        jobs = fetch_all(companies)

    scanned = len(jobs)
    print(f"  companies in universe: {total_companies_monitored}")
    print(f"  jobs fetched: {scanned}")
    if not scanned:
        print("no postings fetched — check the board entries in companies.yaml")
        return 1

    # Record all raw discovered jobs into store
    store.record_discovered(jobs)

    # ---- 2. deterministic filtering (title, location, max_age)
    print("\n[2/6] deterministic filtering")
    filtered_jobs = prefilter(jobs, filters)
    passed_filters = len(filtered_jobs)

    # ---- 3. deduplication (exclude already notified jobs)
    print("\n[3/6] deduplication")
    unnotified_candidates = store.unnotified(filtered_jobs)
    print(f"  {passed_filters} -> {len(unnotified_candidates)} unnotified candidates")
    candidates_count = len(unnotified_candidates)

    if args.limit and len(unnotified_candidates) > args.limit:
        unnotified_candidates = unnotified_candidates[:args.limit]
        print(f"  --limit {args.limit} applied to candidate pool")

    if not unnotified_candidates:
        subject, doc = digest_mod.build([], scanned, 0, store.stats())
        path = digest_mod.write(doc, cfg.get("digest_file", "out/digest.html"))
        print(f"\nnothing new today. preview: {path}")
        return 0

    # ---- 4. LLM screening
    scorer = "keyword" if args.scorer == "keyword" else "llm"
    if scorer == "keyword":
        print(f"\n[4/6] screening {len(unnotified_candidates)} jobs (keyword stub — DEV ONLY)")
        llm.keyword_screen(unnotified_candidates, profile)
    else:
        try:
            provider, model = resolve("screen")
        except LLMError as e:
            print(f"\n{e}\nNo key? Run with --scorer keyword for an offline dry run.")
            return 1
        print(f"\n[4/6] screening {len(unnotified_candidates)} jobs via {provider.name}/{model}")
        llm.screen(unnotified_candidates, profile,
                   batch_size=int(cfg.get("screen_batch_size", 8)),
                   jd_chars=int(cfg.get("screen_jd_chars", 1800)),
                   provider=provider, model=model)

    # Record scores back to store
    store.record_scores(unnotified_candidates)

    # If every batch failed under LLM mode, bail cleanly
    if scorer == "llm" and not any(j.score is not None for j in unnotified_candidates):
        print("\n! screening scored nothing: every batch failed.\n"
              "  Not marking these as notified, so the next run retries them.\n"
              "  Check your API key and connection.")
        return 1

    # ---- 5. selecting daily matches (10-job engine with backfill & diversity)
    print("\n[5/6] selecting daily matches")
    selected_jobs, selection_stats = select_daily_matches(unnotified_candidates, cfg)
    print(f"  preferred matches (>= {cfg.get('preferred_score_threshold', 7.0)}): {selection_stats['preferred_matches']}")
    print(f"  fallback matches (>= {cfg.get('fallback_score_threshold', 6.0)}):  {selection_stats['fallback_matches']}")
    print(f"  selected: {len(selected_jobs)} (across {selection_stats['distinct_companies']} companies)")

    # ---- 6. drafting & digest
    print("\n[6/6] drafting & digest")
    draft_top_n = int(cfg.get("draft_top_n", 5))
    draft_shortlist = selected_jobs[:draft_top_n]

    if not selected_jobs:
        print("  no jobs met the hard minimum score floor")
    elif scorer == "keyword" or args.no_draft:
        print("  skipped drafting (keyword scorer / --no-draft)")
    else:
        try:
            provider, model = resolve("draft")
            print(f"  drafting application kits for top {len(draft_shortlist)} via {provider.name}/{model}")
            llm.draft(draft_shortlist, profile,
                      jd_chars=int(cfg.get("draft_jd_chars", 6000)),
                      provider=provider, model=model)
        except LLMError as e:
            print(f"  ! drafting unavailable: {e}")

    subject, doc = digest_mod.build(selected_jobs, scanned, candidates_count, store.stats())
    path = digest_mod.write(doc, cfg.get("digest_file", "out/digest.html"))
    print(f"  wrote {path}")

    sent = False
    if args.send:
        try:
            mailer.send(subject, doc)
            sent = True
        except Exception as e:
            print(f"  ! email failed ({type(e).__name__}: {e}) — digest saved to {path}")
    else:
        print("  --send not passed, email skipped")


    # Mark ONLY selected emailed jobs as notified
    store.record_notified(selected_jobs, emailed=(sent or (not args.send and bool(selected_jobs))))
    csv_path = store.export_csv(cfg.get("tracker_csv", "out/tracker.csv"))

    print(f"\nfunnel summary:")
    print(f"  {scanned} scanned -> {passed_filters} passed prefilter -> {candidates_count} unnotified -> {len(selected_jobs)} selected in digest")
    print(f"subject: {subject}")
    print(f"tracker: {store.stats()}  ({csv_path})")
    return 0


# ------------------------------------------------------------------- misc --
def cmd_applied(args) -> int:
    store = Store(_cfg(args.config).get("seen_file", "seen.json"))
    ok = store.mark_applied(args.job_id)
    print(f"marked applied: {args.job_id}" if ok else f"unknown job_id: {args.job_id}")
    return 0 if ok else 1


def cmd_stats(args) -> int:
    cfg = _cfg(args.config)
    store = Store(cfg.get("seen_file", "seen.json"))
    print(json.dumps(store.stats(), indent=2))
    csv_path = store.export_csv(cfg.get("tracker_csv", "out/tracker.csv"))
    print(f"csv: {csv_path}")
    return 0


def main(argv=None) -> int:
    _load_env()
    p = argparse.ArgumentParser(
        prog="jobhunt",
        description="Personal job-search agent for Kunal Bishwal. Finds, filters, ranks and drafts.")
    p.add_argument("--config", default="config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("profile", help="turn a resume into profile.json")
    sp.add_argument("--resume", required=True, help="path to a .pdf, .txt or .md resume")
    sp.add_argument("--out", default="profile.json")
    sp.set_defaults(func=cmd_profile)

    sc = sub.add_parser("companies", help="manage company universe")
    sc_sub = sc.add_subparsers(dest="subcmd", required=True)
    sc_sync = sc_sub.add_parser("sync", help="sync companies from PDF into companies.yaml")
    sc_sync.add_argument("--pdf", default="Kunal_Bishwal_2026_Master_Company_Target_List.pdf",
                         help="path to company target list PDF")
    sc_sync.add_argument("--out", default="companies.yaml", help="output yaml path")
    sc_sync.set_defaults(func=cmd_companies_sync)

    sd = sub.add_parser("discover-ats", help="probe career URLs for ATS endpoints")
    sd.add_argument("--config-yaml", default="companies.yaml")
    sd.add_argument("--limit", type=int, default=50)
    sd.set_defaults(func=cmd_discover_ats)

    sr = sub.add_parser("run", help="run the daily pipeline")
    sr.add_argument("--mock", action="store_true", help="bundled fixtures, no network")
    sr.add_argument("--scorer", choices=["llm", "keyword", "claude"], default="llm",
                    help="keyword = offline stub, needs no API key ('claude' is an alias for 'llm')")
    sr.add_argument("--no-draft", action="store_true", help="skip the expensive drafting stage")
    sr.add_argument("--send", action="store_true", help="actually email the digest")
    sr.add_argument("--limit", type=int, help="cap jobs sent to the LLM")
    sr.set_defaults(func=cmd_run)

    sa = sub.add_parser("applied", help="mark a job_id as applied")
    sa.add_argument("job_id")
    sa.set_defaults(func=cmd_applied)

    ss = sub.add_parser("stats", help="tracker summary + CSV export")
    ss.set_defaults(func=cmd_stats)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
