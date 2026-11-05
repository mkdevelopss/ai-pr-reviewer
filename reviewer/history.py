"""Review history for the dashboard: one JSON line per review in data/reviews.jsonl.

Stored: what was reviewed, the outcome, the AI feedback and which GitHub actions
were (or, in dry-run, would have been) taken. Never stored: tokens, keys, the diff.
"""

import json
import os
import threading
import time
import uuid

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
PATH = os.path.join(DATA_DIR, "reviews.jsonl")
_lock = threading.Lock()


def record(pr, result, cfg, source):
    entry = {
        "id": uuid.uuid4().hex[:10], "time": time.strftime("%Y-%m-%d %H:%M:%S"), "source": source,
        "repo": pr.full_name, "number": pr.number, "title": pr.title, "branch": pr.branch,
        "url": pr.url or f"https://github.com/{pr.full_name}/pull/{pr.number}", "head_sha": pr.head_sha,
        "ticket": result.ticket_id, "ticket_system": cfg.ticket_system,
        "ticket_url": _ticket_url(cfg, result.ticket_id),
        "outcome": result.outcome, "reason": result.reason, "message": getattr(result, "message", ""),
        "feedback": result.feedback, "mode": "dry-run" if cfg.dry_run else "live", "model": cfg.llm_model,
        "actions": [a.get("action", "") for a in result.actions],
    }
    os.makedirs(DATA_DIR, exist_ok=True)
    with _lock, open(PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
    return entry


def load(limit=500):
    if not os.path.exists(PATH):
        return []
    with _lock, open(PATH, encoding="utf-8") as fh:
        lines = fh.readlines()[-limit:]
    out = []
    for line in reversed(lines):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue  # a half-written line is skipped, never fatal
    return out


def _ticket_url(cfg, ticket):
    if not ticket:
        return ""
    if cfg.ticket_system == "azure" and cfg.azure_org_url:
        return f"{cfg.azure_org_url}/{cfg.azure_project}/_workitems/edit/{ticket}"
    if cfg.jira_base_url:
        return f"{cfg.jira_base_url}/browse/{ticket}"
    return ""
