"""Send a GitHub-style signed pull_request webhook to the listener (for testing).

    python tools/send_webhook.py <pr_number> [--url http://127.0.0.1:5000/webhook]
           [--action opened] [--bad-signature] [--no-signature] [--delivery ID]

The payload has the same shape GitHub sends (repository.owner.login, repository.name,
pull_request.number/title/head.ref/head.sha/base.ref/state/draft). Uses WEBHOOK_SECRET.
Repository is demo/shop from tools/mock_services.py.
"""

import argparse
import hashlib
import hmac
import json
import os
import sys
import uuid

import requests

sys.path.insert(0, os.path.dirname(__file__))
import mock_services  # noqa: E402


def main():
    p = argparse.ArgumentParser()
    p.add_argument("pr", type=int)
    p.add_argument("--url", default="http://127.0.0.1:5000/webhook")
    p.add_argument("--action", default="opened")
    p.add_argument("--bad-signature", action="store_true")
    p.add_argument("--no-signature", action="store_true")
    p.add_argument("--delivery", default=None)
    a = p.parse_args()

    title, branch, _ = mock_services.PRS[a.pr]
    payload = {"action": a.action,
               "repository": {"name": "shop", "owner": {"login": "demo"}, "full_name": "demo/shop"},
               "pull_request": {"number": a.pr, "title": title, "state": "open", "draft": False,
                                "head": {"ref": branch, "sha": mock_services.sha_for(a.pr)},
                                "base": {"ref": "main"},
                                "html_url": f"https://github.com/demo/shop/pull/{a.pr}"}}
    body = json.dumps(payload).encode()
    secret = os.environ.get("WEBHOOK_SECRET", "")
    headers = {"Content-Type": "application/json", "X-GitHub-Event": "pull_request",
               "X-GitHub-Delivery": a.delivery or str(uuid.uuid4())}
    if not a.no_signature:
        key = (secret + "-tampered") if a.bad_signature else secret
        headers["X-Hub-Signature-256"] = "sha256=" + hmac.new(key.encode(), body, hashlib.sha256).hexdigest()
    r = requests.post(a.url, data=body, headers=headers, timeout=10)
    print(f"PR #{a.pr} -> HTTP {r.status_code} {r.text.strip()}")


if __name__ == "__main__":
    main()
