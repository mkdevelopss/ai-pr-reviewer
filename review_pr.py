"""Manual trigger: run the same review pipeline on an existing PR, without a webhook.

    python review_pr.py owner/repo 12          # uses DRY_RUN from the environment (default: on)
    python review_pr.py owner/repo 12 --live   # actually comment / set status / merge

Useful for the demo and for testing before setting up a public webhook URL.
It builds the same PRInfo the webhook would, then calls the same run_review().
"""

import argparse
import sys

from reviewer.config import Config
from reviewer.errors import ActionFailed
from reviewer import history
from reviewer.github_api import GitHub
from reviewer.pipeline import run_review
from reviewer.webhook import PRInfo


def main(argv=None):
    # Windows consoles default to cp1252; AI feedback can contain characters like a non-breaking hyphen
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser(description="Review one GitHub PR against its ticket.")
    p.add_argument("repo", help="owner/repo")
    p.add_argument("number", type=int, help="PR number")
    p.add_argument("--live", action="store_true", help="perform real actions (overrides DRY_RUN)")
    args = p.parse_args(argv)

    cfg = Config.from_env()
    if args.live:
        cfg.dry_run = False
    if cfg.missing:
        print(f"Missing settings: {', '.join(cfg.missing)}. See .env.example.")
        return 2
    if "/" not in args.repo:
        print("repo must look like owner/repo")
        return 2
    owner, repo = args.repo.split("/", 1)

    gh = GitHub(cfg)
    try:
        data = gh.pull_request(owner, repo, args.number)
    except ActionFailed as e:
        print(f"Error: {e}")
        return 3
    if data.get("state") != "open":
        print(f"PR #{args.number} is {data.get('state')}; only open PRs are reviewed.")
        return 0
    pr = PRInfo(owner, repo, args.number, data.get("title", ""), data["head"]["ref"],
                data["base"]["ref"], data["head"]["sha"], data.get("html_url", ""))
    result = run_review(pr, cfg, gh)
    history.record(pr, result, cfg, source="command line")
    print(f"\nRESULT: {result.outcome} ({result.reason})")
    if result.feedback:
        print(f"\nAI feedback:\n{result.feedback}")
    return 0 if result.outcome in ("merged", "changes_requested", "blocked") else 1


if __name__ == "__main__":
    sys.exit(main())
