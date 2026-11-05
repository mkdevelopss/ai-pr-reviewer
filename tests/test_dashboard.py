"""The dashboard API must be locked: no DASHBOARD_TOKEN -> 503, wrong token -> 401."""
import os
import unittest

import app


class DashboardAuth(unittest.TestCase):
    def setUp(self):
        self.c = app.app.test_client()
        self.old = os.environ.get("DASHBOARD_TOKEN")

    def tearDown(self):
        if self.old is None:
            os.environ.pop("DASHBOARD_TOKEN", None)
        else:
            os.environ["DASHBOARD_TOKEN"] = self.old

    def test_no_token_configured(self):
        os.environ.pop("DASHBOARD_TOKEN", None)
        self.assertEqual(self.c.get("/api/reviews").status_code, 503)

    def test_wrong_and_right_token(self):
        os.environ["DASHBOARD_TOKEN"] = "abc123"
        self.assertEqual(self.c.get("/api/reviews").status_code, 401)
        self.assertEqual(self.c.get("/api/reviews", headers={"X-Dashboard-Token": "nope"}).status_code, 401)
        self.assertEqual(self.c.get("/api/reviews", headers={"X-Dashboard-Token": "abc123"}).status_code, 200)
        self.assertEqual(self.c.post("/api/review", json={"repo": "a/b", "number": 1}).status_code, 401)

    def test_page_is_public_but_empty(self):
        self.assertEqual(self.c.get("/dashboard/").status_code, 200)
