"""Create the live demo in one command (uses the keys in .env, prints none of them).

    python tools/setup_demo.py YOUR-USER/pr-review-demo
    python tools/setup_demo.py YOUR-USER/pr-review-demo --ticket SCRUM-1   # reuse an existing ticket

It creates:
  1. A Jira ticket in the first project of JIRA_PROJECT_KEYS (e.g. SCRUM) with
     an "Acceptance Criteria" section  (skipped when --ticket is given)
  2. PR A: branch feature/<KEY>-email   -> only is_valid_email          (should FAIL)
  3. PR B: branch feature/<KEY>-full    -> both functions + unit tests  (should MERGE)
and prints the review commands to run next.
"""

import base64
import os
import sys
import time

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from reviewer.config import Config  # noqa: E402

DESCRIPTION = [
    ("p", "Add a utility module for user input checks."),
    ("h", "Acceptance Criteria"),
    ("li", "A function is_valid_email(email) returns True only for addresses with one @ and a dot in the domain"),
    ("li", "A function is_strong_password(pw) requires at least 8 characters, one digit and one uppercase letter"),
    ("li", "Both functions have unit tests"),
]

EMAIL_ONLY = '''import re


def is_valid_email(email):
    return bool(re.fullmatch(r"[^@\\s]+@[^@\\s]+\\.[^@\\s]+", email or ""))
'''

FULL = EMAIL_ONLY + '''

def is_strong_password(pw):
    pw = pw or ""
    return len(pw) >= 8 and any(c.isdigit() for c in pw) and any(c.isupper() for c in pw)
'''

TESTS = '''from validators import is_valid_email, is_strong_password


def test_email():
    assert is_valid_email("a@b.com")
    assert not is_valid_email("ab.com")
    assert not is_valid_email("a@bcom")
    assert not is_valid_email("a@@b.com")


def test_password():
    assert is_strong_password("Abcdefg1")
    assert not is_strong_password("abcdefg1")   # no uppercase
    assert not is_strong_password("Abcdefgh")   # no digit
    assert not is_strong_password("Ab1")        # too short
'''


def fail(msg):
    print(f"\nERROR: {msg}")
    sys.exit(1)


def adf_doc():
    content = []
    bullets = []
    for kind, text in DESCRIPTION:
        node = {"type": "paragraph", "content": [{"type": "text", "text": text}]}
        if kind == "li":
            bullets.append({"type": "listItem", "content": [node]})
            continue
        if kind == "h":
            node = {"type": "heading", "attrs": {"level": 3}, "content": [{"type": "text", "text": text}]}
        content.append(node)
    content.append({"type": "bulletList", "content": bullets})
    return {"type": "doc", "version": 1, "content": content}


def create_ticket(cfg, project):
    s = requests.Session()
    s.auth = (cfg.jira_email, cfg.jira_api_token)
    s.headers["Accept"] = "application/json"
    base = cfg.jira_base_url
    me = s.get(f"{base}/rest/api/3/myself", timeout=20)
    if me.status_code != 200:
        fail(f"Jira login failed (HTTP {me.status_code}). Check JIRA_EMAIL and JIRA_API_TOKEN in .env, "
             f"and that JIRA_BASE_URL is {base}")
    print(f"Jira login OK ({me.json().get('displayName', 'user')})")
    for issue_type in ("Task", "Story"):
        r = s.post(f"{base}/rest/api/3/issue", timeout=20, json={"fields": {
            "project": {"key": project}, "summary": "User input validators",
            "issuetype": {"name": issue_type}, "description": adf_doc()}})
        if r.status_code == 201:
            key = r.json()["key"]
            print(f"Jira ticket created: {key}  ->  {base}/browse/{key}")
            return key
        last = r
    fail(f"Could not create a Jira ticket in project {project} (HTTP {last.status_code}): {last.text[:300]}")


class GH:
    def __init__(self, cfg, repo):
        self.repo = repo
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {cfg.github_token}",
                               "Accept": "application/vnd.github+json",
                               "X-GitHub-Api-Version": "2022-11-28"})
        self.api = f"{cfg.github_api}/repos/{repo}"

    def req(self, method, path, **kw):
        r = self.s.request(method, self.api + path, timeout=30, **kw)
        return r

    def check(self):
        r = self.req("GET", "")
        if r.status_code == 404:
            fail(f"GitHub repo '{self.repo}' not found, or the token cannot see it. "
                 f"Check the name (USERNAME/pr-review-demo) and that the token has access to this repo.")
        if r.status_code == 401:
            fail("GitHub rejected GITHUB_TOKEN (401). Make a new token and update .env.")
        if r.status_code != 200:
            fail(f"GitHub returned HTTP {r.status_code} for the repo.")
        default = r.json().get("default_branch", "main")
        ref = self.req("GET", f"/git/ref/heads/{default}")
        if ref.status_code != 200:
            fail(f"The repo has no commits on '{default}'. Create it with 'Add a README file' ticked.")
        print(f"GitHub repo OK: {self.repo} (default branch {default})")
        return default, ref.json()["object"]["sha"]

    def branch(self, name, sha):
        r = self.req("POST", "/git/refs", json={"ref": f"refs/heads/{name}", "sha": sha})
        if r.status_code == 422 and "already exists" in r.text:
            name = f"{name}-{int(time.time()) % 100000}"
            r = self.req("POST", "/git/refs", json={"ref": f"refs/heads/{name}", "sha": sha})
        if r.status_code not in (200, 201):
            fail(f"Could not create branch {name} (HTTP {r.status_code}): {r.json().get('message', '')}. "
                 f"The token needs 'Contents: Read and write'.")
        return name

    def file(self, branch, path, text, message):
        body = {"message": message, "branch": branch,
                "content": base64.b64encode(text.encode()).decode()}
        existing = self.req("GET", f"/contents/{path}", params={"ref": branch})
        if existing.status_code == 200:
            body["sha"] = existing.json()["sha"]
        r = self.req("PUT", f"/contents/{path}", json=body)
        if r.status_code not in (200, 201):
            fail(f"Could not write {path} (HTTP {r.status_code}): {r.json().get('message', '')}")

    def pr(self, branch, base, title):
        r = self.req("POST", "/pulls", json={"title": title, "head": branch, "base": base,
                                             "body": "Demo PR for the AI PR Reviewer."})
        if r.status_code != 201:
            fail(f"Could not open the PR (HTTP {r.status_code}): {r.json().get('message', '')}. "
                 f"The token needs 'Pull requests: Read and write'.")
        return r.json()["number"], r.json()["html_url"]


def main():
    args = sys.argv[1:]
    if not args or "/" not in args[0]:
        print("Usage: python tools/setup_demo.py YOUR-USER/pr-review-demo [--ticket SCRUM-1]")
        return 2
    repo = args[0].strip().strip("/")
    cfg = Config.from_env()
    if cfg.missing:
        fail(f"Missing settings in .env: {', '.join(cfg.missing)}")

    if "--ticket" in args:
        key = args[args.index("--ticket") + 1].upper()
        print(f"Using existing Jira ticket {key}")
    else:
        project = (cfg.jira_project_keys.split(",")[0].strip() or "SCRUM").upper()
        key = create_ticket(cfg, project)

    gh = GH(cfg, repo)
    base, sha = gh.check()

    b1 = gh.branch(f"feature/{key}-email", sha)
    gh.file(b1, "validators.py", EMAIL_ONLY, f"{key}: add email validator")
    n1, url1 = gh.pr(b1, base, f"{key}: Add email validator")
    print(f"PR #{n1} (should FAIL):  {url1}")

    b2 = gh.branch(f"feature/{key}-full", sha)
    gh.file(b2, "validators.py", FULL, f"{key}: add email and password validators")
    gh.file(b2, "test_validators.py", TESTS, f"{key}: add unit tests")
    n2, url2 = gh.pr(b2, base, f"{key}: Add email and password validators")
    print(f"PR #{n2} (should MERGE): {url2}")

    print("\nNext, run these one at a time:")
    print(f"  python review_pr.py {repo} {n1}            (practice run, should say fulfilled=False)")
    print(f"  python review_pr.py {repo} {n1} --live     (posts the comment + red status)")
    print(f"  python review_pr.py {repo} {n2}            (practice run, should say fulfilled=True)")
    print(f"  python review_pr.py {repo} {n2} --live     (merges the PR)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
