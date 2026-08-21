"""Build the daily HTML digest. Inline CSS only — Gmail strips <style> blocks."""
from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path

from .fetch import Job

BG = "#0f1115"
CARD = "#171a21"
LINE = "#262b36"
TEXT = "#e6e8ec"
MUTED = "#8b93a3"
ACCENT = "#7c9cff"
ACCENT_BG = "rgba(124, 156, 255, 0.15)"


def _badge(score: float | None) -> str:
    s = score or 0.0
    color = "#3fb950" if s >= 8.5 else "#d29922" if s >= 7.0 else "#58a6ff" if s >= 6.0 else "#8b949e"
    return (f'<span style="background:{color};color:#0f1115;font-weight:700;'
            f'padding:3px 9px;border-radius:999px;font-size:13px;">{s:.1f}</span>')


def _priority_tag(priority: str | None) -> str:
    if not priority:
        return ""
    p = priority.upper().strip()
    color = "#f778ba" if p == "S" else "#79c0ff" if p == "A" else "#8b949e"
    return (f'<span style="border:1px solid {color};color:{color};font-weight:600;'
            f'padding:2px 6px;border-radius:4px;font-size:11px;margin-left:6px;">Tier {p}</span>')


def _bullets(items: list[str]) -> str:
    if not items:
        return ""
    lis = "".join(
        f'<li style="margin:0 0 6px 0;color:{TEXT};font-size:14px;line-height:1.5;">'
        f'{html.escape(str(i))}</li>' for i in items if str(i).strip())
    return f'<ul style="margin:8px 0 0 0;padding-left:18px;">{lis}</ul>' if lis else ""


def _section(label: str, body: str) -> str:
    if not body:
        return ""
    return (f'<div style="margin-top:14px;">'
            f'<div style="color:{MUTED};font-size:11px;letter-spacing:.09em;'
            f'text-transform:uppercase;font-weight:700;">{label}</div>{body}</div>')


def _card(j: Job) -> str:
    d = j.draft or {}
    meta_parts = [j.company]
    if j.location:
        meta_parts.append(j.location)
    if j.source or j.ats:
        meta_parts.append(j.source or j.ats)
    if j.posted_at:
        meta_parts.append(f"Posted: {j.posted_at[:10]}")

    meta = " · ".join(meta_parts)
    para = lambda t: (f'<p style="margin:8px 0 0 0;color:{TEXT};font-size:14px;'
                      f'line-height:1.6;">{html.escape(t)}</p>') if t else ""

    cover = d.get("cover_note")
    cover_html = ""
    if cover:
        cover_html = _section("Cover note (edit before sending)",
            f'<div style="margin-top:8px;padding:12px;background:#0d1017;'
            f'border:1px solid {LINE};border-radius:8px;color:{TEXT};font-size:14px;'
            f'line-height:1.6;white-space:pre-wrap;">{html.escape(cover)}</div>')

    fit_summary = d.get("fit_summary") or j.reason or ""

    return f"""
<div style="background:{CARD};border:1px solid {LINE};border-radius:12px;padding:18px;margin-bottom:14px;">
  <div style="display:flex;justify-content:space-between;align-items:flex-start;">
    <div>
      <div style="font-size:17px;font-weight:700;color:{TEXT};">
        {html.escape(j.title)}
        {_priority_tag(j.company_priority)}
      </div>
      <div style="color:{MUTED};font-size:13px;margin-top:5px;">{html.escape(meta)}</div>
    </div>
    <div style="padding-left:12px;white-space:nowrap;">{_badge(j.score)}</div>
  </div>
  {_section("Why it matches", para(fit_summary))}
  {_section("Resume bullets for this role", _bullets(d.get("tailored_bullets", [])))}
  {_section("Honest gaps", _bullets(d.get("gaps", [])))}
  {cover_html}
  {_section("Questions to ask in interview", _bullets(d.get("questions_to_ask", [])))}
  <div style="margin-top:16px;">
    <a href="{html.escape(j.url)}" style="display:inline-block;background:{ACCENT};
       color:#0f1115;font-weight:700;font-size:14px;text-decoration:none;
       padding:10px 18px;border-radius:8px;">Open &amp; apply →</a>
    <span style="color:{MUTED};font-size:11px;margin-left:10px;">{html.escape(j.job_id)}</span>
  </div>
</div>"""


def build(jobs: list[Job], scanned: int, candidates: int, stats: dict) -> tuple[str, str]:
    today = datetime.now().strftime("%d %b %Y")
    subject = (f"[JobHunt] {len(jobs)} New Match{'es' if len(jobs) != 1 else ''} — {today}"
               if jobs else f"[JobHunt] No new matches today — {today}")

    if jobs:
        body = "".join(_card(j) for j in jobs)
    else:
        body = (f'<div style="background:{CARD};border:1px solid {LINE};border-radius:12px;'
                f'padding:24px;color:{MUTED};font-size:14px;">Scanned {scanned} postings across '
                f'monitored company boards; no genuine matching jobs cleared the threshold today. '
                f'Boards are often quieter on weekends.</div>')

    html_doc = f"""<!doctype html><html><body style="margin:0;padding:20px;background:{BG};
font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<div style="max-width:680px;margin:0 auto;">
  <div style="color:{TEXT};font-size:24px;font-weight:800;letter-spacing:-0.02em;">Kunal's Daily Job Hunt</div>
  <div style="color:{MUTED};font-size:13px;margin:6px 0 20px 0;">
    {today} · {scanned} postings scanned · {candidates} unnotified candidates · {len(jobs)} selected matches<br>
    Tracker summary: {stats.get('tracked', 0)} tracked · {stats.get('notified', stats.get('emailed', 0))} notified · {stats.get('applied', 0)} applied
  </div>
  {body}
  <div style="color:{MUTED};font-size:11px;line-height:1.6;margin-top:18px;
       border-top:1px solid {LINE};padding-top:14px;">
    Drafts and notes are candidate starting points, not auto-sent. Verify the JD, tailor your note, and submit directly.
  </div>
</div></body></html>"""
    return subject, html_doc


def write(html_doc: str, path: str | Path = "out/digest.html") -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html_doc, encoding="utf-8")
    return path
