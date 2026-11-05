"""Webhook listener (the 'Receiver').

    python app.py                      -> listens on http://0.0.0.0:5000/webhook
                                          dashboard at http://127.0.0.1:5000/dashboard

Expose it to GitHub with a tunnel (e.g. `ngrok http 5000` or smee.io), then in the
test repository: Settings -> Webhooks -> Add webhook
    Payload URL : https://<tunnel>/webhook
    Content type: application/json
    Secret      : the same value as WEBHOOK_SECRET
    Events      : Pull requests

The handler verifies the signature, answers GitHub within milliseconds (202),
and runs the review in a background thread, because an LLM call can take longer
than GitHub's 10-second webhook timeout. Duplicate deliveries are ignored.
"""

import hmac
import json
import logging
import os
import sys
import threading
import time
import uuid
from collections import OrderedDict

from flask import Flask, jsonify, redirect, request, send_from_directory

from reviewer import history
from reviewer.config import Config
from reviewer.errors import ActionFailed
from reviewer.github_api import GitHub
from reviewer.pipeline import run_review
from reviewer.webhook import PRInfo, parse_pr, should_review, verify_signature

app = Flask(__name__)
# Flask's per-request access log interleaves with review logs from worker threads;
# every request decision is logged below instead.
logging.getLogger("werkzeug").setLevel(logging.ERROR)
CFG = Config.from_env()
_print_lock = threading.Lock()
_seen = OrderedDict()  # recent delivery IDs, to ignore GitHub re-deliveries
_lock = threading.Lock()
REVIEWS = []  # results, exposed at /status for the demo


def log(msg):
    with _print_lock:
        print(msg, flush=True)


def _review_in_background(pr, cfg=None, source="webhook"):
    cfg = cfg or CFG
    gh = GitHub(cfg, log=log)
    result = run_review(pr, cfg, gh, log=log)
    REVIEWS.append({"pr": f"{pr.full_name}#{pr.number}", "outcome": result.outcome,
                    "reason": result.reason, "ticket": result.ticket_id})
    log(f"=== RESULT {pr.full_name}#{pr.number}: {result.outcome} ({result.reason})")
    try:
        return history.record(pr, result, cfg, source)
    except OSError as e:
        log(f"(could not save review history: {e})")


@app.post("/webhook")
def webhook():
    body = request.get_data()
    if not verify_signature(CFG.webhook_secret, body, request.headers.get("X-Hub-Signature-256")):
        log("rejected webhook: missing or invalid signature")
        return jsonify(error="invalid signature"), 401

    event = request.headers.get("X-GitHub-Event", "")
    delivery = request.headers.get("X-GitHub-Delivery", "")
    try:
        payload = json.loads(body)
    except ValueError:
        return jsonify(error="body is not JSON"), 400

    review, reason = should_review(event, payload)
    if not review:
        log(f"ignored: {reason}")
        return jsonify(status="ignored", reason=reason), 200

    with _lock:
        if delivery and delivery in _seen:
            return jsonify(status="duplicate delivery ignored"), 200
        _seen[delivery] = True
        while len(_seen) > 500:
            _seen.popitem(last=False)

    try:
        pr = parse_pr(payload)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    if CFG.missing:
        log(f"cannot review: missing settings {CFG.missing}")
        return jsonify(error="reviewer not configured", missing=CFG.missing), 503

    log(f"accepted webhook {delivery or '(no id)'}: {reason} for {pr.full_name}#{pr.number}")
    threading.Thread(target=_review_in_background, args=(pr,), daemon=True).start()
    return jsonify(status="accepted", pr=f"{pr.full_name}#{pr.number}"), 202


# ---------------------------------------------------------------- dashboard
# The dashboard can start reviews (and, in live mode, merges), and app.py is often
# exposed through a tunnel, so every /api/ route needs DASHBOARD_TOKEN.
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard_static")
JOBS = {}


def _auth():
    expected = os.environ.get("DASHBOARD_TOKEN", "")
    if not expected:
        return jsonify(error="Set DASHBOARD_TOKEN in .env to use the dashboard.", kind="no_token"), 503
    given = request.headers.get("X-Dashboard-Token", "")
    if not hmac.compare_digest(given.encode(), expected.encode()):
        return jsonify(error="Wrong dashboard token.", kind="bad_token"), 401
    return None


@app.get("/dashboard")
def dashboard_redirect():
    return redirect("/dashboard/")


@app.get("/dashboard/")
def dashboard():
    return send_from_directory(STATIC, "index.html")


@app.get("/dashboard/<path:name>")
def dashboard_files(name):
    return send_from_directory(STATIC, name)


@app.get("/api/config")
def api_config():
    if (bad := _auth()):
        return bad
    c = CFG
    ticket_keys = (["JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"] if c.ticket_system == "jira"
                   else ["AZURE_ORG_URL", "AZURE_PROJECT", "AZURE_PAT"])
    names = ["GITHUB_TOKEN", "WEBHOOK_SECRET", "LLM_API_KEY"] + ticket_keys
    settings = [{"name": n, "set": bool(os.environ.get(n))} for n in names]
    return jsonify(mode="dry-run" if c.dry_run else "live", ticket_system=c.ticket_system,
                   llm=f"{c.llm_provider}:{c.llm_model}", merge_method=c.merge_method,
                   max_diff_chars=c.max_diff_chars, project_keys=c.jira_project_keys,
                   settings=settings, missing=c.missing, github_api=c.github_api)


@app.get("/api/reviews")
def api_reviews():
    if (bad := _auth()):
        return bad
    return jsonify(reviews=history.load())


@app.post("/api/review")
def api_review():
    """Review one PR now, same pipeline as the webhook. Dry-run unless live is asked AND allowed."""
    if (bad := _auth()):
        return bad
    if CFG.missing:
        return jsonify(error="Missing settings: " + ", ".join(CFG.missing)), 503
    data = request.get_json(silent=True) or {}
    repo = str(data.get("repo", "")).strip().strip("/")
    if repo.startswith("https://github.com/"):
        repo = repo[len("https://github.com/"):]
    try:
        number = int(data.get("number"))
    except (TypeError, ValueError):
        return jsonify(error="PR number must be a number."), 400
    if repo.count("/") != 1:
        return jsonify(error="Repository must look like owner/repo."), 400
    live = bool(data.get("live"))
    cfg = Config.from_env(dotenv=False)
    cfg.dry_run = not live
    owner, name = repo.split("/")
    gh = GitHub(cfg, log=log)
    try:
        pr_data = gh.pull_request(owner, name, number)
    except ActionFailed as e:
        return jsonify(error=str(e)), 404
    if pr_data.get("state") != "open":
        return jsonify(error=f"PR #{number} is {pr_data.get('state')}; only open PRs are reviewed."), 409
    pr = PRInfo(owner, name, number, pr_data.get("title", ""), pr_data["head"]["ref"],
                pr_data["base"]["ref"], pr_data["head"]["sha"], pr_data.get("html_url", ""))
    job = uuid.uuid4().hex[:10]
    JOBS[job] = {"state": "running", "started": time.time(), "pr": f"{repo}#{number}", "mode": cfg.dry_run}

    def work():
        try:
            JOBS[job]["entry"] = _review_in_background(pr, cfg, "dashboard")
            JOBS[job]["state"] = "done"
        except Exception as e:  # pipeline already fails closed; this is only for the UI
            JOBS[job].update(state="error", error=str(e))
    threading.Thread(target=work, daemon=True).start()
    log(f"dashboard review started for {repo}#{number} ({'LIVE' if live else 'dry-run'})")
    return jsonify(job=job, pr=f"{repo}#{number}", title=pr.title), 202


@app.get("/api/review-jobs/<job>")
def api_job(job):
    if (bad := _auth()):
        return bad
    if job not in JOBS:
        return jsonify(error="unknown job"), 404
    j = dict(JOBS[job])
    j["elapsed"] = round(time.time() - j.pop("started"), 1)
    j.pop("mode", None)
    return jsonify(j)


@app.get("/")
def health():
    return jsonify(service="ai-pr-reviewer", mode="dry-run" if CFG.dry_run else "live",
                   missing_settings=CFG.missing, reviews=REVIEWS[-20:])


if __name__ == "__main__":
    log(f"ai-pr-reviewer starting | {CFG.describe()}")
    if not CFG.webhook_secret:
        log("WARNING: WEBHOOK_SECRET is not set - every webhook will be rejected (401).")
    if CFG.missing:
        log(f"WARNING: missing settings: {', '.join(CFG.missing)}")
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5000
    app.run(host="0.0.0.0", port=port)
