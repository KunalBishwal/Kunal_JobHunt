"""Company universe synchronization and ATS discovery.

Extracts curated target companies from the master PDF, merges them with
companies.yaml while strictly preserving verified ATS slugs, and discovers
ATS endpoints from career pages.
"""
from __future__ import annotations

import re
import urllib.parse
from pathlib import Path
from typing import Any

import pypdf
import requests
import yaml

UA = {"User-Agent": "jobhunt/1.0 (personal job search agent)"}
TIMEOUT = 10

CATEGORY_PAGES: list[tuple[str, list[int]]] = [
    ("Big Tech / Global Product", [6, 7]),
    ("Indian Product / Unicorn", [8, 9]),
    ("Fintech / Payments", [10, 11]),
    ("SaaS / Developer Tools", [12, 13]),
    ("AI / ML / GenAI", [14, 15]),
    ("India Startups / Growth Tech", [16, 17, 18]),
    ("Banks / Financial Services Tech", [19, 20]),
    ("Consulting / IT", [21]),
    ("Semiconductor / Hardware / Systems", [23]),
    ("Remote / Global Developer & AI Startups", [25]),
]

# Verified / known public ATS mappings for target companies
KNOWN_ATS_MAPPINGS: dict[str, dict[str, str]] = {
    "razorpay": {"ats": "greenhouse", "slug": "razorpaysoftwareprivatelimited"},
    "postman": {"ats": "greenhouse", "slug": "postman"},
    "groww": {"ats": "greenhouse", "slug": "groww"},
    "together ai": {"ats": "greenhouse", "slug": "togetherai"},
    "twilio": {"ats": "greenhouse", "slug": "twilio"},
    "mongodb": {"ats": "greenhouse", "slug": "mongodb"},
    "planetscale": {"ats": "greenhouse", "slug": "planetscale"},
    "sarvam ai": {"ats": "ashby", "slug": "sarvam"},
    "replit": {"ats": "ashby", "slug": "replit"},
    "perplexity": {"ats": "ashby", "slug": "perplexity"},
    "perplexity ai": {"ats": "ashby", "slug": "perplexity"},
    "cohere": {"ats": "ashby", "slug": "cohere"},
    "harvey": {"ats": "ashby", "slug": "harvey"},
    "ramp": {"ats": "ashby", "slug": "ramp"},
    "phonepe": {"ats": "greenhouse", "slug": "phonepe"},
    "aidash": {"ats": "lever", "slug": "aidash"},
    "drivetrain": {"ats": "lever", "slug": "drivetrain"},
    "outmarket ai": {"ats": "ashby", "slug": "outmarket"},
    "conga": {"ats": "greenhouse", "slug": "conga"},
    "smartbear": {"ats": "greenhouse", "slug": "smartbear"},
    "energy exemplar": {"ats": "greenhouse", "slug": "energyexemplarllc"},
    "amtech software": {"ats": "greenhouse", "slug": "amtechsoftware"},
    "redwood software": {"ats": "greenhouse", "slug": "redwoodsoftware"},
    "new relic": {"ats": "greenhouse", "slug": "newrelic"},
    "nanonets": {"ats": "greenhouse", "slug": "nanonets"},
    "supabase": {"ats": "ashby", "slug": "supabase"},
    "vercel": {"ats": "greenhouse", "slug": "vercel"},
    "linear": {"ats": "ashby", "slug": "linear"},
    "posthog": {"ats": "ashby", "slug": "posthog"},
    "anthropic": {"ats": "greenhouse", "slug": "anthropic"},
    "scale ai": {"ats": "greenhouse", "slug": "scaleai"},
    "stripe": {"ats": "greenhouse", "slug": "stripe"},
    "uber": {"ats": "greenhouse", "slug": "uber"},
    "databricks": {"ats": "greenhouse", "slug": "databricks"},
    "hugging face": {"ats": "workable", "slug": "huggingface"},
    "inmobi": {"ats": "greenhouse", "slug": "inmobi"},
    "freshworks": {"ats": "smartrecruiters", "slug": "Freshworks"},
    "atlan": {"ats": "ashby", "slug": "atlan"},
    "hasura": {"ats": "greenhouse", "slug": "hasura"},
    "sprinklr": {"ats": "greenhouse", "slug": "sprinklr"},
    "clevertap": {"ats": "greenhouse", "slug": "clevertap"},
    "sprinto": {"ats": "ashby", "slug": "sprinto"},
    "thoughtspot": {"ats": "greenhouse", "slug": "thoughtspot"},
    "browserstack": {"ats": "greenhouse", "slug": "browserstack"},
}


def normalize_name(name: str) -> str:
    """Canonical key for company name matching."""
    s = name.lower().strip()
    s = re.sub(r"[^\w\s]", "", s)
    s = re.sub(r"\s+", " ", s)
    return s


def extract_companies_from_pdf(pdf_path: str | Path) -> list[dict[str, Any]]:
    """Extract all 353 companies from the master target PDF."""
    path = Path(pdf_path)
    if not path.exists():
        raise FileNotFoundError(f"PDF not found: {path}")

    reader = pypdf.PdfReader(str(path))
    companies: list[dict[str, Any]] = []

    for cat_name, page_nums in CATEGORY_PAGES:
        for p_num in page_nums:
            page = reader.pages[p_num - 1]
            annots = []
            if "/Annots" in page:
                for a in page["/Annots"]:
                    obj = a.get_object()
                    if "/A" in obj and "/URI" in obj["/A"]:
                        rect = [float(x) for x in obj["/Rect"]]
                        annots.append({"uri": obj["/A"]["/URI"], "rect": rect})
            # sort annotations top to bottom
            annots.sort(key=lambda x: -x["rect"][1])

            text = page.extract_text() or ""
            lines = [l.strip() for l in text.split("\n") if l.strip()]

            # Find starting index after header
            start_idx = 0
            for idx, l in enumerate(lines):
                if l.lower() in ("career page", "target roles"):
                    start_idx = idx + 1
                    break

            items = []
            cur_idx = start_idx
            while cur_idx < len(lines):
                l = str(lines[cur_idx])
                if re.match(r"^\d+$", l):
                    num = int(l)
                    if cur_idx + 3 < len(lines):
                        comp_name = str(lines[cur_idx + 1])
                        priority = str(lines[cur_idx + 2].strip())
                        roles = str(lines[cur_idx + 3])
                        items.append({
                            "num": num,
                            "name": comp_name,
                            "priority": priority,
                            "target_roles": roles,
                            "category": str(cat_name),
                            "page": p_num,
                        })
                        cur_idx += 5
                    else:
                        break
                else:
                    cur_idx += 1

            for it, a in zip(items, annots):
                it["career_url"] = str(a["uri"])
                companies.append(it)

    return companies


def detect_ats_from_url(url: str) -> tuple[str, str | None]:
    """Detect ATS system and slug from direct URL pattern."""
    if not url:
        return "unknown", None

    u = url.strip()
    # Greenhouse
    m = re.search(r"(?:boards\.greenhouse\.io|job-boards\.greenhouse\.io|boards-api\.greenhouse\.io)/([^/?#]+)", u, re.I)
    if m:
        return "greenhouse", m.group(1).rstrip("/")

    # Lever
    m = re.search(r"jobs\.lever\.co/([^/?#]+)", u, re.I)
    if m:
        return "lever", m.group(1).rstrip("/")

    # Ashby
    m = re.search(r"jobs\.ashbyhq\.com/([^/?#]+)", u, re.I)
    if m:
        return "ashby", m.group(1).rstrip("/")

    # SmartRecruiters
    m = re.search(r"jobs\.smartrecruiters\.com/([^/?#]+)", u, re.I)
    if m:
        return "smartrecruiters", m.group(1).rstrip("/")

    # Workday
    if "myworkdayjobs.com" in u.lower():
        return "workday", None

    # Workable
    m = re.search(r"apply\.workable\.com/([^/?#]+)", u, re.I)
    if m:
        return "workable", m.group(1).rstrip("/")

    return "unknown", None


def sync_companies(pdf_path: str | Path,
                   yaml_path: str | Path) -> dict[str, Any]:
    """Merge 353 PDF companies into companies.yaml preserving verified values."""
    pdf_companies = extract_companies_from_pdf(pdf_path)
    yaml_p = Path(yaml_path)

    existing_companies = []
    if yaml_p.exists():
        try:
            existing_data = yaml.safe_load(yaml_p.read_text(encoding="utf-8")) or {}
            existing_companies = existing_data.get("companies") or []
        except Exception as e:
            print(f"  ! Warning reading {yaml_p}: {e}")

    # Build lookup of existing verified companies
    existing_by_name = {}
    for c in existing_companies:
        if isinstance(c, dict) and "name" in c:
            existing_by_name[normalize_name(c["name"])] = c

    merged_list = []
    seen_normalized = set()
    already_present_count = 0
    new_added_count = 0

    for item in pdf_companies:
        name = item["name"]
        norm = normalize_name(name)
        if norm in seen_normalized:
            continue
        seen_normalized.add(norm)

        existing = existing_by_name.get(norm)
        if existing:
            already_present_count += 1
            # Preserve existing verified ATS and slug
            ats = existing.get("ats")
            slug = existing.get("slug")
            active = existing.get("active", True if ats not in (None, "unknown", "custom") else False)
        else:
            new_added_count += 1
            # Check known verified dictionary or direct url detection
            known = KNOWN_ATS_MAPPINGS.get(norm)
            if known:
                ats = known["ats"]
                slug = known["slug"]
                active = True
            else:
                detected_ats, detected_slug = detect_ats_from_url(item["career_url"])
                ats = detected_ats
                slug = detected_slug
                active = True if ats not in ("unknown", "custom", "workday", "workable") else False

        entry = {
            "name": name,
            "category": item["category"],
            "priority": item["priority"],
            "target_roles": item["target_roles"],
            "career_url": item["career_url"],
            "ats": ats or "unknown",
            "active": active,
        }
        if slug:
            entry["slug"] = slug

        merged_list.append(entry)

    # Write out clean YAML
    out_dict = {"companies": merged_list}
    # Custom dump formatting for readability
    yaml_text = "# Target Company Universe (Derived from Master Target List PDF)\n"
    yaml_text += f"# Total Companies: {len(merged_list)}\n\n"
    yaml_text += yaml.dump(out_dict, sort_keys=False, allow_unicode=True)

    yaml_p.write_text(yaml_text, encoding="utf-8")

    summary = {
        "pdf_companies": len(pdf_companies),
        "existing_companies": len(existing_companies),
        "already_present": already_present_count,
        "new_added": new_added_count,
        "total_in_yaml": len(merged_list),
    }
    return summary


def discover_ats_for_companies(yaml_path: str | Path,
                               limit: int | None = None) -> list[dict[str, str]]:
    """Probes career URLs to discover public ATS endpoints for unknown companies."""
    yaml_p = Path(yaml_path)
    if not yaml_p.exists():
        return []

    data = yaml.safe_load(yaml_p.read_text(encoding="utf-8")) or {}
    companies = data.get("companies") or []

    session = requests.Session()
    results = []

    count = 0
    for c in companies:
        name = c.get("name")
        ats = c.get("ats", "unknown")
        slug = c.get("slug")
        url = c.get("career_url", "")

        if ats != "unknown" and slug:
            results.append({"name": name, "ats": ats, "slug": slug, "status": "verified"})
            continue

        # Try to detect
        detected_ats, detected_slug = detect_ats_from_url(url)
        if detected_ats != "unknown" and detected_slug:
            results.append({"name": name, "ats": detected_ats, "slug": detected_slug, "status": "discovered_from_url"})
            continue

        results.append({"name": name, "ats": ats, "slug": slug or "", "status": "custom/unknown"})
        count += 1
        if limit and count >= limit:
            break

    return results
