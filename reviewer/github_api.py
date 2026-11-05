"""Component 1c (Code Fetcher) and Component 3 (Action Executor) - GitHub REST API.

Code Fetcher:
    GET /repos/{o}/{r}/pulls/{n}  with Accept: application/vnd.github.diff  -> raw unified diff
Actions:
    POST /repos/{o}/{r}/statuses/{sha}          commit status: failure / success / pending
    POST /repos/{o}/{r}/pulls/{n}/reviews       review comment (REQUEST_CHANGES)
    POST /repos/{o}/{r}/issues/{n}/comments     plain PR comment (fallback)
    PUT  /repos/{o}/{r}/pulls/{n}/merge         merge, pinned to the reviewed head SHA

Why "failure" status blocks merging: with branch protection on the target branch
requiring the 'ai-pr-reviewer' status check, GitHub disables the merge button
until that status is 'success'.

DRY_RUN: every write is printed as "[DRY-RUN] would ..." and NOT sent.
"""

import requests

from .errors import ActionFailed, DiffTooLarge, DiffUnavailable, EmptyDiff

TIMEOUT = 30


class GitHub:
    def __init__(self, cfg, session=None, log=print):
        self.cfg = cfg
        self.log = log
        self.session = session or requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {cfg.github_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ai-pr-reviewer",
        })
        self.actions = []  # record of what was done / would be done (for reports and tests)

    def _url(self, path):
        return f"{self.cfg.github_api}{path}"

    # ----------------------------------------------------------- read
    def pull_request(self, owner, repo, number):
        """PR JSON (used by the manual trigger review_pr.py)."""
        try:
            r = self.session.get(self._url(f"/repos/{owner}/{repo}/pulls/{number}"), timeout=TIMEOUT)
        except requests.exceptions.RequestException as e:
            raise ActionFailed(f"Could not reach GitHub: {e.__class__.__name__}")
        if r.status_code == 404:
            raise ActionFailed(f"PR #{number} not found in {owner}/{repo} (or the token cannot see it).")
        if r.status_code >= 400:
            raise ActionFailed(f"GitHub returned HTTP {r.status_code} for PR #{number}.")
        return r.json()

    def diff(self, pr):
        """Raw diff text. Raises EmptyDiff / DiffTooLarge / DiffUnavailable."""
        try:
            r = self.session.get(self._url(f"/repos/{pr.owner}/{pr.repo}/pulls/{pr.number}"),
                                 headers={"Accept": "application/vnd.github.diff"}, timeout=TIMEOUT)
        except requests.exceptions.RequestException as e:
            raise DiffUnavailable("Could not download the PR diff from GitHub.", detail=e.__class__.__name__)
        if r.status_code == 406 or (r.status_code == 422 and "too large" in r.text.lower()):
            # GitHub refuses diffs over ~20,000 lines / 3,000 files
            raise DiffTooLarge("The diff is too large for GitHub to return, so it cannot be reviewed automatically.")
        if r.status_code >= 400:
            raise DiffUnavailable(f"GitHub returned HTTP {r.status_code} when fetching the diff.")
        text = r.text or ""
        if not text.strip():
            raise EmptyDiff("The PR has no code changes (empty diff), so there is nothing to verify.")
        if len(text) > self.cfg.max_diff_chars:
            raise DiffTooLarge(
                f"The diff is {len(text):,} characters, over the {self.cfg.max_diff_chars:,}-character limit "
                f"for one LLM request. Split the PR or review it manually.")
        return text

    # ----------------------------------------------------------- write
    def _write(self, method, path, body, what):
        self.actions.append({"action": what, "method": method, "path": path, "body": body})
        if self.cfg.dry_run:
            self.log(f"[DRY-RUN] would {what}: {method} {path}")
            return {"dry_run": True}
        try:
            r = self.session.request(method, self._url(path), json=body, timeout=TIMEOUT)
        except requests.exceptions.RequestException as e:
            raise ActionFailed(f"Could not {what}: {e.__class__.__name__}")
        if r.status_code >= 400:
            msg = ""
            try:
                msg = r.json().get("message", "")
            except ValueError:
                pass
            raise ActionFailed(f"Could not {what}: HTTP {r.status_code} {msg}".strip())
        self.log(f"done: {what}")
        try:
            return r.json()
        except ValueError:
            return {}

    def set_status(self, pr, state, description):
        return self._write("POST", f"/repos/{pr.owner}/{pr.repo}/statuses/{pr.head_sha}",
                           {"state": state, "context": self.cfg.status_context,
                            "description": description[:140]},
                           f"set status '{state}' on {pr.head_sha[:7]}")

    def comment(self, pr, body, request_changes=True):
        """Post a review requesting changes; fall back to a plain comment.

        GitHub does not allow REQUEST_CHANGES on your own PR (HTTP 422), which is
        common in a test repo where the bot token belongs to the PR author.
        """
        if request_changes:
            try:
                return self._write("POST", f"/repos/{pr.owner}/{pr.repo}/pulls/{pr.number}/reviews",
                                   {"body": body, "event": "REQUEST_CHANGES", "commit_id": pr.head_sha},
                                   "post a 'request changes' review")
            except ActionFailed as e:
                self.log(f"review not allowed ({e}); posting a normal comment instead")
        return self._write("POST", f"/repos/{pr.owner}/{pr.repo}/issues/{pr.number}/comments",
                           {"body": body}, "post a PR comment")

    def merge(self, pr, title):
        return self._write("PUT", f"/repos/{pr.owner}/{pr.repo}/pulls/{pr.number}/merge",
                           {"sha": pr.head_sha, "merge_method": self.cfg.merge_method,
                            "commit_title": f"{title} (#{pr.number})"},
                           f"merge PR #{pr.number} ({self.cfg.merge_method})")
