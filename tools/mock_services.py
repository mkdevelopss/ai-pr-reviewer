"""Local stand-ins for GitHub, Jira and an OpenAI-compatible LLM, for testing only.

One HTTP server answers all three APIs (point GITHUB_API_URL, JIRA_BASE_URL and
LLM_BASE_URL at it). Repository: demo/shop. Each PR number is one scenario:

  #1  ticket in title, diff meets every criterion           -> MERGE
  #2  ticket in title, diff misses 2 criteria               -> comments + FAILED
  #3  no ticket ID anywhere                                 -> blocked (no_ticket_id)
  #4  ticket PROJ-999 does not exist                        -> blocked (ticket_unavailable)
  #5  ticket has no acceptance criteria                     -> blocked (no_acceptance_criteria)
  #6  empty diff                                            -> blocked (empty_diff)
  #7  diff larger than MAX_DIFF_CHARS                       -> blocked (diff_too_large)
  #8  LLM answers in prose, not JSON                        -> blocked (invalid_llm_response)
  #9  LLM JSON has no 'fulfilled' key                       -> blocked (invalid_llm_response)
  #10 ticket only in branch name, diff meets criteria       -> MERGE
  #11 LLM service returns HTTP 500                          -> blocked (llm_unavailable)
  #12 criteria met but GitHub refuses the merge (conflict)  -> merge_failed, not merged
  #13 criteria missed, token owns the PR (review 422)       -> plain comment fallback + FAILED

The mock "LLM" is deterministic, not intelligent: every acceptance criterion
names a code identifier in backticks, and the criterion counts as met when that
identifier appears in an added line of the diff. Marker words in the diff force
the broken-answer scenarios.

    python tools/mock_services.py [port]      (default 8800)
"""

import base64
import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

LOGIN_AC = (
    "# Acceptance Criteria\n"
    "- The login form validates the email format with `validate_email`\n"
    "- Passwords are checked with `check_password_hash`, never compared as plain text\n"
    "- After 5 failed attempts the account is locked by `lock_account`\n"
    "- A successful login redirects to the dashboard via `redirect_to_dashboard`\n"
)

GOOD_DIFF = """diff --git a/app/auth.py b/app/auth.py
new file mode 100644
--- /dev/null
+++ b/app/auth.py
@@ -0,0 +1,24 @@
+from werkzeug.security import check_password_hash
+from .accounts import find_user, lock_account, record_failure
+from .validators import validate_email
+from .routes import redirect_to_dashboard
+
+MAX_ATTEMPTS = 5
+
+def login(email, password):
+    if not validate_email(email):
+        return {"error": "Invalid email format"}, 400
+    user = find_user(email)
+    if user is None or not check_password_hash(user.password_hash, password):
+        if user is not None and record_failure(user) >= MAX_ATTEMPTS:
+            lock_account(user)
+        return {"error": "Invalid email or password"}, 401
+    return redirect_to_dashboard(user)
"""

PARTIAL_DIFF = """diff --git a/app/auth.py b/app/auth.py
new file mode 100644
--- /dev/null
+++ b/app/auth.py
@@ -0,0 +1,11 @@
+from .accounts import find_user
+from .validators import validate_email
+from .routes import redirect_to_dashboard
+
+def login(email, password):
+    if not validate_email(email):
+        return {"error": "Invalid email format"}, 400
+    user = find_user(email)
+    if user is None or user.password != password:
+        return {"error": "Invalid email or password"}, 401
+    return redirect_to_dashboard(user)
"""

TICKETS = {
    "PROJ-101": {"summary": "Login page with account lockout",
                 "text": "Users need to sign in with email and password.\n\n" + LOGIN_AC},
    "PROJ-102": {"summary": "Improve login page", "text": "Make the login page better. Details to follow."},
}

PRS = {
    1: ("PROJ-101: Add login endpoint", "feature/PROJ-101-login", GOOD_DIFF),
    2: ("PROJ-101: Add login endpoint", "feature/PROJ-101-login-v1", PARTIAL_DIFF),
    3: ("Fix some stuff", "fix-stuff", GOOD_DIFF),
    4: ("PROJ-999: Something", "feature/PROJ-999", GOOD_DIFF),
    5: ("PROJ-102: Improve login", "feature/PROJ-102", GOOD_DIFF),
    6: ("PROJ-101: Empty change", "feature/PROJ-101-empty", ""),
    7: ("PROJ-101: Huge change", "feature/PROJ-101-huge", GOOD_DIFF + "+# padding\n" * 8000),
    8: ("PROJ-101: Prose answer", "feature/PROJ-101-x", GOOD_DIFF + "+# MOCK_PROSE_ANSWER\n"),
    9: ("PROJ-101: Missing key", "feature/PROJ-101-y", GOOD_DIFF + "+# MOCK_NO_FULFILLED\n"),
    10: ("Login endpoint", "feature/proj-101-login-endpoint", GOOD_DIFF),
    11: ("PROJ-101: LLM down", "feature/PROJ-101-z", GOOD_DIFF + "+# MOCK_LLM_500\n"),
    12: ("PROJ-101: Conflicting", "feature/PROJ-101-conflict", GOOD_DIFF),
    13: ("PROJ-101: Own PR", "feature/PROJ-101-own", PARTIAL_DIFF),
}

STATE = {"actions": [], "merged": set()}
LOCK = threading.Lock()


def sha_for(n):
    return f"{n:02d}" + "a1b2c3d4e5f6" * 3 + "abcdef"[:2]


def adf(text):
    """Very small text -> Atlassian Document Format converter."""
    content = []
    for line in text.splitlines():
        if line.startswith("# "):
            content.append({"type": "heading", "attrs": {"level": 3},
                            "content": [{"type": "text", "text": line[2:]}]})
        elif line.startswith("- "):
            content.append({"type": "bulletList", "content": [{"type": "listItem", "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": line[2:]}]}]}]})
        elif line.strip():
            content.append({"type": "paragraph", "content": [{"type": "text", "text": line}]})
    return {"type": "doc", "version": 1, "content": content}


def mock_llm_answer(user_text):
    diff = user_text.split("<code_diff>", 1)[-1]
    ac = user_text.split("<acceptance_criteria>", 1)[-1].split("</acceptance_criteria>", 1)[0]
    if "MOCK_LLM_500" in diff:
        return None
    if "MOCK_PROSE_ANSWER" in diff:
        return "The change looks mostly fine to me, nice work on the login form!"
    if "MOCK_NO_FULFILLED" in diff:
        return json.dumps({"feedback": "Reviewed."})
    added = "\n".join(l for l in diff.splitlines() if l.startswith("+"))
    missing = []
    for line in ac.splitlines():
        ids = re.findall(r"`([^`]+)`", line)
        if ids and not all(i in added for i in ids):
            missing.append(line.strip("- ").strip())
    if missing:
        fb = "**Missing requirements:**\n" + "\n".join(f"- {m}" for m in missing)
        if "user.password != password" in added:
            fb += "\n\n**Bug:** the password is compared as plain text (`user.password != password`)."
        return json.dumps({"fulfilled": False, "feedback": fb})
    return json.dumps({"fulfilled": True, "feedback": "All 4 acceptance criteria are implemented: "
                       "email validation, hashed password check, lockout after 5 failures, dashboard redirect."})


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, status, body=None, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else json.dumps(body if body is not None else {}).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            return {}

    # ---------------------------------------------------------------- GET
    def do_GET(self):
        path = urlparse(self.path).path
        m = re.fullmatch(r"/repos/demo/shop/pulls/(\d+)", path)
        if m:
            n = int(m.group(1))
            if n not in PRS:
                return self._send(404, {"message": "Not Found"})
            title, branch, diff = PRS[n]
            if "diff" in (self.headers.get("Accept") or ""):
                return self._send(200, diff, "text/plain; charset=utf-8")
            return self._send(200, {"number": n, "title": title, "state": "open",
                                    "head": {"ref": branch, "sha": sha_for(n)}, "base": {"ref": "main"},
                                    "html_url": f"https://github.com/demo/shop/pull/{n}"})
        m = re.fullmatch(r"/rest/api/3/issue/([A-Z0-9-]+)", path)
        if m:
            if not self.headers.get("Authorization", "").startswith("Basic "):
                return self._send(401, {"errorMessages": ["auth required"]})
            t = TICKETS.get(m.group(1))
            if not t:
                return self._send(404, {"errorMessages": ["Issue does not exist"]})
            return self._send(200, {"key": m.group(1), "fields": {"summary": t["summary"],
                                                                  "description": adf(t["text"])}})
        if path == "/_mock/actions":
            return self._send(200, {"actions": STATE["actions"], "merged": sorted(STATE["merged"])})
        return self._send(404, {"message": "Not Found"})

    # --------------------------------------------------------- POST / PUT
    def do_POST(self):
        path = urlparse(self.path).path
        body = self._body()
        if path.endswith("/chat/completions"):
            if not self.headers.get("Authorization", "").removeprefix("Bearer ").strip():
                return self._send(401, {"error": "invalid API key"})
            answer = mock_llm_answer(body["messages"][-1]["content"])
            if answer is None:
                return self._send(500, {"error": "upstream failure"})
            return self._send(200, {"choices": [{"message": {"role": "assistant", "content": answer}}]})
        m = re.fullmatch(r"/repos/demo/shop/(statuses/(\w+)|pulls/(\d+)/reviews|issues/(\d+)/comments)", path)
        if m:
            if m.group(3) and int(m.group(3)) == 13:
                return self._send(422, {"message": "Can not request changes on your own pull request"})
            with LOCK:
                STATE["actions"].append({"path": path, "body": body})
            return self._send(201, {"id": len(STATE["actions"])})
        return self._send(404, {"message": "Not Found"})

    def do_PUT(self):
        path = urlparse(self.path).path
        body = self._body()
        m = re.fullmatch(r"/repos/demo/shop/pulls/(\d+)/merge", path)
        if not m:
            return self._send(404, {"message": "Not Found"})
        n = int(m.group(1))
        if n == 12:
            return self._send(405, {"message": "Pull Request is not mergeable"})
        if body.get("sha") != sha_for(n):
            return self._send(409, {"message": "Head branch was modified. Review and try the merge again."})
        with LOCK:
            STATE["merged"].add(n)
            STATE["actions"].append({"path": path, "body": body})
        return self._send(200, {"merged": True, "message": "Pull Request successfully merged"})


def start(port=8800):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8800
    print(f"Mock GitHub + Jira + LLM on http://127.0.0.1:{port}  (repo demo/shop, PRs #1-#13)")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
