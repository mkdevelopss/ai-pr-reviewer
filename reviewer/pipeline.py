"""The review pipeline: Story -> Code -> AI -> Action, with one fail-safe rule:

    The PR is merged ONLY IF every step succeeded AND the AI returned fulfilled == true.

Any problem (no ticket ID, ticket missing, no acceptance criteria, empty or huge
diff, AI unreachable, invalid AI JSON, any unexpected error) ends in:
    - a PR comment explaining exactly what went wrong and how to fix it
    - commit status 'failure' (blocks merging when branch protection requires it)
    - NO merge
"""

import traceback
from dataclasses import dataclass, field

from . import ai_engine, story_fetcher
from .errors import ActionFailed, ReviewBlocked

FOOTER = "\n\n---\n<sub>Automated review by ai-pr-reviewer. Re-runs on every new push to this PR.</sub>"


@dataclass
class Result:
    outcome: str                # merged | changes_requested | blocked | merge_failed | error
    reason: str
    ticket_id: str = ""
    feedback: str = ""
    actions: list = field(default_factory=list)
    message: str = ""           # human-readable reason when the review was blocked or failed


def _blocked_comment(err, pr):
    return (f"### ⛔ Automated review stopped: not merged\n\n**Reason:** {err.message}\n\n"
            f"The PR was not evaluated or merged, and its `ai-pr-reviewer` status is set to failed. "
            f"Fix the issue above and push a new commit to run the review again.{FOOTER}")


def _fail_comment(ticket, feedback):
    return (f"### ❌ Acceptance criteria not met: [{ticket.id}]({ticket.url}) {ticket.title}\n\n"
            f"{feedback}\n\n<details><summary>Acceptance criteria checked</summary>\n\n"
            f"{ticket.acceptance_criteria}\n\n</details>{FOOTER}")


def run_review(pr, cfg, gh, log=print, ticket_session=None, llm_session=None):
    log(f"--- Reviewing {pr.full_name}#{pr.number} '{pr.title}' (branch {pr.branch}) | {cfg.describe()}")
    ticket_id = ""
    try:
        gh.set_status(pr, "pending", "AI review in progress")

        # Component 1: Story Fetcher
        ticket_id, where = story_fetcher.extract_ticket_id(pr.title, pr.branch, cfg.ticket_system,
                                                           cfg.jira_project_keys)
        log(f"ticket {ticket_id} found in {where}")
        ticket = story_fetcher.fetch_ticket(ticket_id, cfg, ticket_session)
        log(f"ticket fetched: '{ticket.title}' ({len(ticket.acceptance_criteria.splitlines())} AC lines)")

        # Component 1: Code Fetcher
        diff = gh.diff(pr)
        log(f"diff fetched: {len(diff):,} characters")

        # Component 2: AI Evaluation Engine
        fulfilled, feedback, _raw = ai_engine.evaluate(cfg, ticket, diff, llm_session)
        log(f"AI verdict: fulfilled={fulfilled}")

    except ReviewBlocked as err:
        log(f"BLOCKED ({err.code}): {err.message}" + (f" [{err.detail}]" if err.detail else ""))
        _safe(lambda: gh.comment(pr, _blocked_comment(err, pr), request_changes=False), log)
        _safe(lambda: gh.set_status(pr, "failure", f"Review stopped: {err.message}"), log)
        return Result("blocked", err.code, ticket_id, actions=gh.actions, message=err.message)
    except Exception as err:  # unexpected bug: fail closed, never merge
        log("UNEXPECTED ERROR, failing safe:\n" + traceback.format_exc())
        _safe(lambda: gh.set_status(pr, "error", "Reviewer crashed; PR not merged"), log)
        return Result("error", err.__class__.__name__, ticket_id, actions=gh.actions,
                      message="The reviewer hit an unexpected error; nothing was merged. See the server log.")

    # Component 3: Action Executor
    if fulfilled is True:
        try:
            gh.set_status(pr, "success", f"All acceptance criteria of {ticket.id} met")
            gh.merge(pr, pr.title)
            log("MERGED" if not cfg.dry_run else "would MERGE (dry-run)")
            return Result("merged", "fulfilled", ticket.id, feedback, gh.actions)
        except ActionFailed as err:
            log(f"merge failed: {err}")
            _safe(lambda: gh.comment(pr, (f"### ⚠️ Criteria met, but the automatic merge failed\n\n{err}\n\n"
                                          f"**AI summary:** {feedback}{FOOTER}"), request_changes=False), log)
            return Result("merge_failed", str(err), ticket.id, feedback, gh.actions, message=str(err))

    try:
        gh.comment(pr, _fail_comment(ticket, feedback), request_changes=True)
        gh.set_status(pr, "failure", f"Acceptance criteria of {ticket.id} not met")
    except ActionFailed as err:
        log(f"could not post the review: {err}")
    return Result("changes_requested", "not fulfilled", ticket.id, feedback, gh.actions)


def _safe(fn, log):
    """Run a best-effort reporting action; a failure here must not hide the original problem."""
    try:
        fn()
    except Exception as e:
        log(f"(could not report to GitHub: {e})")
