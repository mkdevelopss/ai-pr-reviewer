"""Component 1a: read and verify the GitHub pull_request webhook.

- Signature: GitHub signs the body with HMAC-SHA256 using WEBHOOK_SECRET and
  sends it in X-Hub-Signature-256. We recompute it and compare in constant
  time, so nobody else can trigger reviews (or merges) by posting fake events.
- Only pull_request events with actions opened / reopened / synchronize /
  ready_for_review are reviewed. Draft and closed PRs are ignored.
"""

import hashlib
import hmac
from dataclasses import dataclass

REVIEW_ACTIONS = {"opened", "reopened", "synchronize", "ready_for_review"}


@dataclass
class PRInfo:
    owner: str
    repo: str
    number: int
    title: str
    branch: str          # head branch, e.g. feature/PROJ-123-login-page
    base: str            # target branch
    head_sha: str        # commit the status is attached to
    url: str = ""

    @property
    def full_name(self):
        return f"{self.owner}/{self.repo}"


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    if not secret:
        return False  # refuse to run unauthenticated
    if not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def should_review(event: str, payload: dict) -> tuple[bool, str]:
    """Return (review?, reason). The reason is logged for ignored events."""
    if event == "ping":
        return False, "ping event (webhook connected)"
    if event != "pull_request":
        return False, f"ignored event type '{event}'"
    action = payload.get("action")
    if action not in REVIEW_ACTIONS:
        return False, f"ignored pull_request action '{action}'"
    pr = payload.get("pull_request") or {}
    if pr.get("draft"):
        return False, "PR is a draft"
    if pr.get("state") != "open":
        return False, "PR is not open"
    return True, f"pull_request {action}"


def parse_pr(payload: dict) -> PRInfo:
    """Extract the fields the pipeline needs. Raises ValueError if any is missing."""
    try:
        pr = payload["pull_request"]
        repo = payload["repository"]
        return PRInfo(
            owner=repo["owner"]["login"],
            repo=repo["name"],
            number=int(pr["number"]),
            title=pr.get("title") or "",
            branch=pr["head"]["ref"],
            base=pr["base"]["ref"],
            head_sha=pr["head"]["sha"],
            url=pr.get("html_url", ""),
        )
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"webhook payload is missing a required field: {e}")
