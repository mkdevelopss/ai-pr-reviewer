"""Create a real Jira ticket + GitHub PR from a ticket text file and a code file (uses .env; prints no keys).

    python tools/setup_eval.py OWNER/REPO eval/ticket_config_apis.txt eval/WUserController.js controllers/WUserController.js

Ticket file format: first line "SUMMARY: ...", lines starting "# " are headings, "- " are bullets, others paragraphs.
"""
import os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import requests
from reviewer.config import Config
from setup_demo import GH, fail


def adf(lines):
    content, bullets = [], []
    def flush():
        if bullets:
            content.append({"type": "bulletList", "content": [
                {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": b}]}]} for b in bullets]})
            bullets.clear()
    for l in lines:
        if l.startswith("# "):
            flush(); content.append({"type": "heading", "attrs": {"level": 3}, "content": [{"type": "text", "text": l[2:]}]})
        elif l.startswith("- "):
            bullets.append(l[2:])
        elif l.strip():
            flush(); content.append({"type": "paragraph", "content": [{"type": "text", "text": l}]})
    flush()
    return {"type": "doc", "version": 1, "content": content}


def main():
    if len(sys.argv) != 5:
        print(__doc__); return 2
    repo, ticket_file, code_file, repo_path = sys.argv[1:]
    cfg = Config.from_env()
    if cfg.missing:
        fail(f"Missing settings in .env: {', '.join(cfg.missing)}")
    lines = open(ticket_file, encoding="utf-8").read().splitlines()
    summary = lines[0].replace("SUMMARY:", "").strip()
    project = (cfg.jira_project_keys.split(",")[0].strip() or "SCRUM").upper()
    s = requests.Session(); s.auth = (cfg.jira_email, cfg.jira_api_token); s.headers["Accept"] = "application/json"
    key = None
    for issue_type in ("Story", "Task"):
        r = s.post(f"{cfg.jira_base_url}/rest/api/3/issue", timeout=30, json={"fields": {
            "project": {"key": project}, "summary": summary, "issuetype": {"name": issue_type}, "description": adf(lines[1:])}})
        if r.status_code == 201:
            key = r.json()["key"]; break
    if not key:
        fail(f"Could not create the Jira ticket (HTTP {r.status_code}): {r.text[:300]}")
    print(f"Jira ticket created: {key} -> {cfg.jira_base_url}/browse/{key}")
    gh = GH(cfg, repo)
    base, sha = gh.check()
    branch = gh.branch(f"feature/{key}-configuration-apis", sha)
    gh.file(branch, repo_path, open(code_file, encoding="utf-8").read(), f"{key}: add controller")
    n, url = gh.pr(branch, base, f"{key}: Implement Configuration Management APIs")
    print(f"PR #{n}: {url}")
    print("\nNext, run one at a time:")
    print(f"  python review_pr.py {repo} {n}          (practice run)")
    print(f"  python review_pr.py {repo} {n} --live   (posts the result on GitHub)")


if __name__ == "__main__":
    sys.exit(main())
