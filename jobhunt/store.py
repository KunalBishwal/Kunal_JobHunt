"""Store and application tracker.

Maintains the state machine for every discovered job:
  discovered -> scored -> notified -> applied (or ignored / expired)

Separates discovery deduplication from notification status so unnotified jobs
from previous runs remain candidates for future digests.
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .fetch import Job


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path = "seen.json"):
        self.path = Path(path)
        self.data: dict[str, dict[str, Any]] = {}
        if self.path.exists():
            try:
                raw_data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw_data, dict):
                    self.data = self._migrate(raw_data)
            except json.JSONDecodeError:
                print(f"  ! {self.path} corrupt, starting fresh")
            except Exception as e:
                print(f"  ! Error loading {self.path}: {e}")

    def _migrate(self, raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Migrate legacy seen.json format without data loss."""
        now = _now_iso()
        migrated = {}
        for jid, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            is_emailed = bool(entry.get("notified", entry.get("emailed", False)))
            is_applied = bool(entry.get("applied", False))
            first_seen = entry.get("first_seen", now)
            last_seen = entry.get("last_seen", first_seen)
            notified_at = entry.get("notified_at")
            if is_emailed and not notified_at:
                notified_at = first_seen
            applied_on = entry.get("applied_on") or entry.get("applied_at")

            source = entry.get("source")
            if not source:
                parts = str(jid).split(":")
                source = parts[0] if len(parts) > 1 else "unknown"

            score = entry.get("score")
            reason = entry.get("reason")

            status = entry.get("status")
            if not status:
                if is_applied:
                    status = "applied"
                elif is_emailed:
                    status = "notified"
                elif score is not None:
                    status = "scored"
                else:
                    status = "discovered"

            migrated[jid] = {
                "job_id": jid,
                "company": entry.get("company", ""),
                "title": entry.get("title", ""),
                "location": entry.get("location", ""),
                "url": entry.get("url", ""),
                "source": source,
                "score": score,
                "reason": reason,
                "status": status,
                "first_seen": first_seen,
                "last_seen": last_seen,
                "notified": is_emailed,
                "notified_at": notified_at,
                "applied": is_applied,
                "applied_on": applied_on,
                "applied_at": applied_on,
            }
        return migrated

    def is_notified(self, job_id: str) -> bool:
        entry = self.data.get(job_id)
        if not entry:
            return False
        return bool(entry.get("notified", False))

    def unnotified(self, jobs: list[Job]) -> list[Job]:
        """Returns jobs that have NOT been notified/emailed yet and are not marked applied/ignored."""
        out = []
        for j in jobs:
            entry = self.data.get(j.job_id)
            if not entry:
                out.append(j)
            elif not entry.get("notified", False) and entry.get("status") not in ("applied", "ignored"):
                # If already scored previously, populate existing score/reason
                if j.score is None and entry.get("score") is not None:
                    j.score = entry["score"]
                    j.reason = entry.get("reason")
                out.append(j)
        return out

    def record_discovered(self, jobs: list[Job]) -> None:
        """Register jobs as discovered; update last_seen timestamp."""
        now = _now_iso()
        for j in jobs:
            if j.job_id not in self.data:
                self.data[j.job_id] = {
                    "job_id": j.job_id,
                    "company": j.company,
                    "title": j.title,
                    "location": j.location,
                    "url": j.url,
                    "source": j.source or j.ats,
                    "score": j.score,
                    "reason": j.reason,
                    "status": "discovered",
                    "first_seen": now,
                    "last_seen": now,
                    "notified": False,
                    "notified_at": None,
                    "applied": False,
                    "applied_on": None,
                    "applied_at": None,
                }
            else:
                self.data[j.job_id]["last_seen"] = now
                if j.score is not None and self.data[j.job_id].get("score") is None:
                    self.data[j.job_id]["score"] = j.score
                    self.data[j.job_id]["reason"] = j.reason
        self.save()

    def record_scores(self, jobs: list[Job]) -> None:
        """Update screening scores in the store."""
        for j in jobs:
            if j.job_id in self.data and j.score is not None:
                self.data[j.job_id]["score"] = j.score
                self.data[j.job_id]["reason"] = j.reason
                if self.data[j.job_id]["status"] == "discovered":
                    self.data[j.job_id]["status"] = "scored"
        self.save()

    def record_notified(self, jobs: list[Job], emailed: bool = True) -> None:
        """Mark ONLY selected jobs as notified/emailed."""
        if not emailed:
            return
        now = _now_iso()
        for j in jobs:
            if j.job_id in self.data:
                self.data[j.job_id]["notified"] = True
                self.data[j.job_id]["notified_at"] = now
                self.data[j.job_id]["status"] = "notified"
            else:
                self.data[j.job_id] = {
                    "job_id": j.job_id,
                    "company": j.company,
                    "title": j.title,
                    "location": j.location,
                    "url": j.url,
                    "source": j.source or j.ats,
                    "score": j.score,
                    "reason": j.reason,
                    "status": "notified",
                    "first_seen": now,
                    "last_seen": now,
                    "notified": True,
                    "notified_at": now,
                    "applied": False,
                    "applied_on": None,
                    "applied_at": None,
                }
        self.save()

    def mark_applied(self, job_id: str) -> bool:
        if job_id not in self.data:
            return False
        now = _now_iso()
        self.data[job_id]["applied"] = True
        self.data[job_id]["applied_on"] = now
        self.data[job_id]["applied_at"] = now
        self.data[job_id]["status"] = "applied"
        self.save()
        return True

    def stats(self) -> dict[str, int]:
        return {
            "tracked": len(self.data),
            "discovered": sum(1 for v in self.data.values() if v.get("status") == "discovered"),
            "scored": sum(1 for v in self.data.values() if v.get("score") is not None),
            "notified": sum(1 for v in self.data.values() if v.get("notified")),
            "applied": sum(1 for v in self.data.values() if v.get("applied")),
        }

    def export_csv(self, path: str | Path = "out/tracker.csv") -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        cols = [
            "job_id", "company", "title", "location", "score",
            "reason", "first_seen", "last_seen", "notified",
            "notified_at", "applied", "applied_on", "url", "source"
        ]
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for jid, row in sorted(
                self.data.items(),
                key=lambda kv: (kv[1].get("notified_at") or kv[1].get("first_seen") or ""),
                reverse=True
            ):
                item = dict(row)
                item["job_id"] = jid
                w.writerow(item)
        return path

    def save(self) -> None:
        self.path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
