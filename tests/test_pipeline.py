"""End-to-end pipeline tests against the local mock GitHub + Jira + LLM.

Runs every scenario in tools/mock_services.py with DRY_RUN off (so the mock
records real API writes) and checks the one rule that matters most:
a PR is merged only when the criteria are met and nothing went wrong.
"""

import os
import sys
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import mock_services
from reviewer.config import Config
from reviewer.github_api import GitHub
from reviewer.pipeline import run_review
from reviewer.webhook import PRInfo

PORT = 8811
BASE = f"http://127.0.0.1:{PORT}"


def cfg(**over):
    c = Config(github_token="test-gh", github_api=BASE, webhook_secret="s", ticket_system="jira",
               jira_base_url=BASE, jira_email="me@test", jira_api_token="test-jira",
               llm_provider="openai", llm_api_key="test-llm", llm_base_url=BASE, llm_model="mock",
               dry_run=False, max_diff_chars=60_000)
    for k, v in over.items():
        setattr(c, k, v)
    return c


def pr(n):
    title, branch, _ = mock_services.PRS[n]
    return PRInfo("demo", "shop", n, title, branch, "main", mock_services.sha_for(n))


class Pipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = mock_services.start(PORT)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def run_pr(self, n, **over):
        logs = []
        gh = GitHub(cfg(**over), log=logs.append)
        result = run_review(pr(n), cfg(**over), gh, log=logs.append)
        return result, gh.actions, logs

    def merged(self, actions):
        return any(a["method"] == "PUT" and a["path"].endswith("/merge") for a in actions)

    def statuses(self, actions):
        return [a["body"]["state"] for a in actions if "/statuses/" in a["path"]]

    # --- the two required demo outcomes
    def test_1_criteria_met_merges(self):
        r, actions, _ = self.run_pr(1)
        self.assertEqual(r.outcome, "merged")
        self.assertTrue(self.merged(actions))
        self.assertEqual(self.statuses(actions), ["pending", "success"])
        merge = [a for a in actions if a["path"].endswith("/merge")][0]
        self.assertEqual(merge["body"]["sha"], mock_services.sha_for(1))  # pinned to reviewed commit

    def test_2_criteria_missed_comments_and_fails(self):
        r, actions, _ = self.run_pr(2)
        self.assertEqual(r.outcome, "changes_requested")
        self.assertFalse(self.merged(actions))
        self.assertEqual(self.statuses(actions), ["pending", "failure"])
        review = [a for a in actions if a["path"].endswith("/reviews")][0]
        self.assertEqual(review["body"]["event"], "REQUEST_CHANGES")
        self.assertIn("lock_account", review["body"]["body"])
        self.assertIn("check_password_hash", review["body"]["body"])

    # --- the four required unsafe situations (and variants): never merge
    def assert_blocked(self, n, code, **over):
        r, actions, _ = self.run_pr(n, **over)
        self.assertEqual((r.outcome, r.reason), ("blocked", code))
        self.assertFalse(self.merged(actions))
        self.assertEqual(self.statuses(actions)[-1], "failure")
        comments = [a for a in actions if a["path"].endswith("/comments")]
        self.assertTrue(comments and "not merged" in comments[0]["body"]["body"])
        return r

    def test_3_no_ticket_id(self):
        self.assert_blocked(3, "no_ticket_id")

    def test_4_ticket_not_found(self):
        self.assert_blocked(4, "ticket_unavailable")

    def test_4b_jira_unreachable(self):
        self.assert_blocked(1, "ticket_unavailable", jira_base_url="http://127.0.0.1:9")

    def test_5_no_acceptance_criteria(self):
        self.assert_blocked(5, "no_acceptance_criteria")

    def test_6_empty_diff(self):
        self.assert_blocked(6, "empty_diff")

    def test_7_diff_too_large(self):
        self.assert_blocked(7, "diff_too_large")

    def test_8_llm_not_json(self):
        self.assert_blocked(8, "invalid_llm_response")

    def test_9_llm_missing_fulfilled(self):
        self.assert_blocked(9, "invalid_llm_response")

    def test_11_llm_down(self):
        self.assert_blocked(11, "llm_unavailable")

    def test_11b_llm_bad_key(self):
        r = self.assert_blocked(1, "llm_unavailable", llm_api_key="")

    # --- other paths
    def test_10_ticket_from_branch_name(self):
        r, actions, logs = self.run_pr(10)
        self.assertEqual((r.outcome, r.ticket_id), ("merged", "PROJ-101"))
        self.assertTrue(any("found in branch name" in l for l in logs))

    def test_12_merge_refused_is_reported(self):
        r, actions, _ = self.run_pr(12)
        self.assertEqual(r.outcome, "merge_failed")
        self.assertIn("not mergeable", r.reason)

    def test_13_own_pr_falls_back_to_comment(self):
        r, actions, logs = self.run_pr(13)
        self.assertEqual(r.outcome, "changes_requested")
        self.assertTrue(any(a["path"].endswith("/issues/13/comments") for a in actions))
        self.assertEqual(self.statuses(actions)[-1], "failure")

    def test_dry_run_sends_nothing(self):
        before = len(mock_services.STATE["actions"])
        r, actions, logs = self.run_pr(1, dry_run=True)
        self.assertEqual(r.outcome, "merged")       # decision is made...
        self.assertEqual(len(mock_services.STATE["actions"]), before)  # ...but nothing was sent
        self.assertTrue(any("[DRY-RUN] would merge" in l for l in logs))

    def test_only_fulfilled_prs_merge(self):
        """The core safety property across ALL scenarios."""
        for n in mock_services.PRS:
            r, actions, _ = self.run_pr(n)
            self.assertEqual(self.merged(actions), n in (1, 10, 12), msg=f"PR #{n}: {r.outcome}")


if __name__ == "__main__":
    unittest.main()
