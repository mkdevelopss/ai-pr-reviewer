"""Every reason the reviewer must STOP without merging is a ReviewBlocked error.

The pipeline catches ReviewBlocked, posts the reason on the PR, marks the PR
status as failed, and never merges. `code` is a short machine-readable label
used in logs and tests.
"""


class ReviewBlocked(Exception):
    code = "blocked"

    def __init__(self, message, detail=""):
        super().__init__(message)
        self.message = message
        self.detail = detail  # extra info for logs only (never secrets)


class NoTicketId(ReviewBlocked):
    code = "no_ticket_id"


class TicketUnavailable(ReviewBlocked):
    code = "ticket_unavailable"


class NoAcceptanceCriteria(ReviewBlocked):
    code = "no_acceptance_criteria"


class DiffUnavailable(ReviewBlocked):
    code = "diff_unavailable"


class EmptyDiff(ReviewBlocked):
    code = "empty_diff"


class DiffTooLarge(ReviewBlocked):
    code = "diff_too_large"


class LLMUnavailable(ReviewBlocked):
    code = "llm_unavailable"


class InvalidLLMResponse(ReviewBlocked):
    code = "invalid_llm_response"


class ActionFailed(Exception):
    """A GitHub action (comment / status / merge) failed after the decision."""
