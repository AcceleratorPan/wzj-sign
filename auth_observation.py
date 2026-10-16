"""Persist credential lifetimes and recovery evidence without credential values."""

import json
import os
import statistics
import time
from pathlib import Path

from auth import fingerprint, private_directory, read_json, write_json


def start_acceptance(runtime, student_id):
    path = Path(runtime) / "acceptance-24h.json"
    now = time.time()
    saved = read_json(path, {})
    if not isinstance(saved, dict):
        saved = {}
    if saved.get("ends_at", 0) > now and str(saved.get("student_id")) == str(student_id):
        return saved
    if saved:
        path.rename(path.with_name(f"acceptance-24h-{int(saved['started_at'])}.json"))
    report = {
        "student_id": student_id, "started_at": now, "ends_at": now + 86400,
        "status": "observing", "poll_count": 0, "last_poll_at": None,
        "max_poll_gap_seconds": 0, "worker_starts": 0, "worker_stops": 0,
        "successful_signs": [], "failed_sign_attempts": 0, "retry_count": 0,
        "automatic_expiries": 0, "automatic_recoveries": 0,
        "pending_recoveries": {}, "diagnostic_passed_at": None,
    }
    write_json(path, report)
    return report


class AuthObservation:
    def __init__(self, runtime, student_id):
        self.path = Path(runtime) / "auth-observation.json"
        self.events = Path(runtime) / "auth-events.jsonl"
        self.student_id = student_id
        self.acceptance_path = Path(runtime) / "acceptance-24h.json"
        self.acceptance = read_json(self.acceptance_path)
        if not isinstance(self.acceptance, dict) or str(self.acceptance.get("student_id")) != str(student_id):
            self.acceptance = None
        data = read_json(self.path, {})
        if not isinstance(data, dict) or str(data.get("student_id")) != str(student_id):
            data = {}
        self.state = data or {
            "student_id": student_id, "started_at": time.time(), "current": None,
            "pending_recovery": None, "samples": [], "automatic_expiries": 0,
            "completed_recoveries": 0,
        }

    def save(self):
        self.state["updated_at"] = time.time()
        write_json(self.path, self.state)

    def event(self, kind, **fields):
        private_directory(self.events.parent)
        record = {"event": kind, "at": time.time(), "student_id": self.student_id, **fields}
        fd = os.open(self.events, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(json.dumps(record, ensure_ascii=False) + "\n")
            output.flush()
            os.fsync(output.fileno())
        self.update_acceptance(record=record)

    def update_acceptance(self, *, record=None, polling=False):
        report = self.acceptance
        if not report or report["status"] == "complete":
            return
        now = time.time()
        if now < report["started_at"]:
            return
        until = min(now, report["ends_at"])
        gap = until - (report["last_poll_at"] or report["started_at"])
        report["max_poll_gap_seconds"] = round(max(report["max_poll_gap_seconds"], gap), 3)
        report["updated_at"] = now
        report["observed_seconds"] = round(until - report["started_at"], 3)
        if now >= report["ends_at"]:
            report.update(status="complete", completed_at=now)
        elif polling:
            report["poll_count"] += 1
            report["last_poll_at"] = now
        elif record:
            kind = record["event"]
            if kind in ("worker_started", "worker_stopped"):
                report["worker_starts" if kind == "worker_started" else "worker_stops"] += 1
            elif kind == "attendance_succeeded":
                report["successful_signs"].append({k: record[k] for k in ("at", "mode", "course_id", "sign_id")})
            elif kind == "attendance_failed":
                report["failed_sign_attempts"] += 1
            elif kind == "retry_scheduled":
                report["retry_count"] += 1
            elif kind == "credential_expired" and record["trigger"] == "automatic":
                report["automatic_expiries"] += 1
                report["pending_recoveries"][record["fingerprint"]] = record["at"]
            elif kind == "recovery_completed" and record["trigger"] == "automatic":
                if record["old_fingerprint"] in report["pending_recoveries"]:
                    report["automatic_recoveries"] += 1
                    report["pending_recoveries"].pop(record["old_fingerprint"])
                    report["last_recovery_seconds"] = record["recovery_seconds"]
            elif kind == "recovery_test_passed":
                report["diagnostic_passed_at"] = record["at"]
        report["sign_counts"] = {mode: sum(s["mode"] == mode for s in report["successful_signs"])
                                 for mode in ("normal", "gps", "qrcode")}
        report["checks"] = {
            "three_sign_modes_observed": all(report["sign_counts"].values()),
            "automatic_recovery_observed": report["automatic_expiries"] > 0
                and report["automatic_recoveries"] == report["automatic_expiries"]
                and not report["pending_recoveries"],
            "diagnostic_recovery_passed": report["diagnostic_passed_at"] is not None,
        }
        write_json(self.acceptance_path, report)

    def applied(self, credential, trigger):
        now = time.time()
        fp = fingerprint(credential.value)
        current = self.state.get("current") or {}
        changed = current.get("fingerprint") != fp
        if changed:
            current = {
                "fingerprint": fp, "captured_at": credential.captured_at,
                "source": credential.source, "first_observed_at": now,
                "issuance_time_known": credential.source in ("http_account_login", "web_account_login"),
            }
        current["last_accepted_at"] = now
        current["last_applied_at"] = now
        self.state["current"] = current
        pending = self.state.get("pending_recovery")
        if pending and pending["fingerprint"] != fp:
            pending.update(new_fingerprint=fp, applied_at=now, identity_verified=True)
        self.event("credential_applied", fingerprint=fp, changed=changed, trigger=trigger,
                   captured_at=credential.captured_at, source=credential.source,
                   identity_verified=True)
        self.save()

    def expired(self, credential, http_status, trigger):
        now = time.time()
        fp = fingerprint(credential.value)
        pending = self.state.get("pending_recovery")
        # A failed refresh can cause the same expired credential to be checked
        # again. Count the lifetime once, keeping the first failure time.
        if pending and pending["fingerprint"] == fp:
            return
        current = self.state.get("current") or {}
        matching = current.get("fingerprint") == fp
        last = current.get("last_accepted_at") if matching else None
        captured = current.get("captured_at") if matching else credential.captured_at
        known = current.get("issuance_time_known") if matching else (
            credential.source in ("http_account_login", "web_account_login")
        )
        valid_times = isinstance(captured, (int, float)) and 0 < captured <= now
        bounded = bool(known and valid_times and last is not None and captured <= last <= now)
        sample = {
            "fingerprint": fp, "detected_at": now, "trigger": trigger,
            "http_status": http_status, "captured_at": captured,
            "last_accepted_at": last,
            "lifetime_lower_seconds": round(last - captured, 3) if bounded else None,
            "lifetime_upper_seconds": round(now - captured, 3) if known and valid_times else None,
            "detection_window_seconds": round(now - last, 3) if last is not None and last <= now else None,
            "eligible_for_period_estimate": trigger == "automatic" and bounded,
            "recovered_at": None,
        }
        self.state["samples"].append(sample)
        self.state["samples"] = self.state["samples"][-200:]
        self.state["pending_recovery"] = dict(sample)
        if trigger == "automatic":
            self.state["automatic_expiries"] += 1
        self.event("credential_expired", **sample)
        self.save()

    def api_success(self, value, *, polling=True):
        now = time.time()
        fp = fingerprint(value)
        current = self.state.get("current") or {}
        if current.get("fingerprint") == fp:
            current["last_accepted_at"] = now
        pending = self.state.get("pending_recovery")
        if polling and pending and pending.get("new_fingerprint") == fp and pending.get("identity_verified"):
            recovery = {
                "new_fingerprint": fp, "applied_at": pending["applied_at"],
                "recovered_at": now, "recovery_seconds": round(now - pending["detected_at"], 3),
                "identity_verified": True, "polling_resumed": True,
            }
            for sample in reversed(self.state["samples"]):
                if sample["fingerprint"] == pending["fingerprint"] and sample["detected_at"] == pending["detected_at"]:
                    sample.update(recovery)
                    break
            self.state["completed_recoveries"] += 1
            self.event("recovery_completed", old_fingerprint=pending["fingerprint"],
                       trigger=pending["trigger"], **recovery)
            self.state["pending_recovery"] = None
        self.save()
        if polling:
            self.update_acceptance(polling=True)

    def retry(self, error, delay):
        self.event("retry_scheduled", error_type=type(error).__name__, retry_in=delay)

    def report(self):
        now = time.time()
        samples = self.state["samples"]
        eligible = [s for s in samples if s["eligible_for_period_estimate"]]
        estimate = None
        if eligible:
            estimate = {
                "sample_count": len(eligible),
                "median_lower_seconds": round(statistics.median(s["lifetime_lower_seconds"] for s in eligible), 3),
                "median_upper_seconds": round(statistics.median(s["lifetime_upper_seconds"] for s in eligible), 3),
            }
        current = self.state.get("current")
        result = {
            **self.state, "reported_at": now, "period_estimate": estimate,
            "note": "按服务端实际失效响应估算；其他登录、换证或撤销也可能使凭证提前失效，不能据此认定固定 TTL。",
        }
        if current:
            current = dict(current)
            captured = current.get("captured_at")
            last = current.get("last_accepted_at")
            if captured and last and current.get("issuance_time_known"):
                current["last_confirmed_valid_age_seconds"] = round(max(0, last - captured), 3)
            result["current"] = current
        return result
