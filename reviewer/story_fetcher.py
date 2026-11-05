"""Component 1b: Story Fetcher.

1. Find the ticket ID in the PR title first, then the branch name.
     Jira : PROJ-123        e.g. "feature/PROJ-123-login-page", "PROJ-123: Login page"
     Azure: AB#123          (Azure Boards' own convention) or a branch like "feature/123-login"
2. Fetch the ticket and its acceptance criteria.
     Jira : REST API v3 /rest/api/3/issue/{key}. AC comes from JIRA_AC_FIELD (if set),
            otherwise from the section headed "Acceptance Criteria" in the description.
     Azure: /_apis/wit/workitems/{id}. AC comes from Microsoft.VSTS.Common.AcceptanceCriteria,
            otherwise the "Acceptance Criteria" section of System.Description.
3. No ID, fetch failure, or empty AC -> ReviewBlocked. The pipeline then never merges.
"""

import html
import re
from dataclasses import dataclass

import requests

from .errors import NoAcceptanceCriteria, NoTicketId, TicketUnavailable

TIMEOUT = 20
JIRA_KEY = re.compile(r"(?<![A-Za-z0-9])([A-Z][A-Z0-9]{1,9}-\d{1,7})(?![0-9])")
AZURE_HASH = re.compile(r"\bAB#(\d{1,9})\b", re.I)
AZURE_BRANCH = re.compile(r"(?:^|/)(\d{1,9})(?:[-_/]|$)")
# Tokens shaped like Jira keys that are not tickets (found while testing "Update to UTF-8")
NOT_TICKETS = {"UTF-8", "UTF-16", "UTF-32", "SHA-1", "SHA-256", "SHA-512", "ISO-8601", "ISO-8859",
               "PEP-8", "COVID-19", "RFC-2616", "ES-2015", "ES-2020", "MD-5", "X-1", "HTTP-2"}
# Branch-naming words: "release-2024" or "hotfix-12" are not Jira projects
NOT_PROJECT_PREFIXES = {"RELEASE", "HOTFIX", "FEATURE", "BUGFIX", "FIX", "CHORE", "BUILD",
                        "VERSION", "DEPENDABOT", "RENOVATE", "PATCH", "SPRINT", "WEEK", "PR"}
AC_HEADING = re.compile(r"^\s*(?:#+\s*|h\d\.\s*|\*\s*)?acceptance\s+criteria\s*:?\s*$", re.I)
NEXT_HEADING = re.compile(r"^\s*(?:#+\s+\S|h\d\.\s|[A-Z][A-Za-z ]{2,40}:\s*$)")


@dataclass
class Ticket:
    id: str
    title: str
    description: str
    acceptance_criteria: str
    url: str = ""


# ------------------------------------------------------------ ticket ID
def extract_ticket_id(title: str, branch: str, system: str, project_keys: str = "") -> tuple[str, str]:
    """Return (ticket_id, where_found). Raises NoTicketId.

    project_keys: optional comma list (JIRA_PROJECT_KEYS, e.g. "PROJ,SHOP"). When set,
    only keys from those projects count. Otherwise common look-alikes such as
    UTF-8 or SHA-256 are skipped.
    """
    title, branch = title or "", branch or ""
    if system == "azure":
        for text, where in ((title, "PR title"), (branch, "branch name")):
            m = AZURE_HASH.search(text)
            if m:
                return m.group(1), where
        m = AZURE_BRANCH.search(branch)
        if m:
            return m.group(1), "branch name"
        hint = "Add 'AB#<work item id>' to the PR title, or name the branch like feature/123-login."
    else:
        allowed = {k.strip().upper() for k in (project_keys or "").split(",") if k.strip()}
        for text, where in ((title, "PR title"), (branch, "branch name")):
            for m in JIRA_KEY.finditer(text.upper() if where == "branch name" else text):
                key = m.group(1)
                prefix = key.rsplit("-", 1)[0]
                if allowed and prefix not in allowed:
                    continue
                if not allowed and (key in NOT_TICKETS or prefix in NOT_PROJECT_PREFIXES):
                    continue
                return key, where
        hint = "Put the Jira key in the PR title (e.g. 'PROJ-123: Login page') or the branch (feature/PROJ-123-login)."
    raise NoTicketId(f"No ticket ID found in the PR title ('{title}') or branch ('{branch}'). {hint}")


# ------------------------------------------------------------ text helpers
def adf_to_text(node) -> str:
    """Convert Jira's Atlassian Document Format (JSON) to plain text with line breaks."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(adf_to_text(n) for n in node)
    kind = node.get("type")
    if kind == "text":
        return node.get("text", "")
    if kind == "hardBreak":
        return "\n"
    inner = adf_to_text(node.get("content", []))
    if kind == "heading":
        return "\n# " + inner.strip() + "\n"
    if kind == "listItem":
        return "- " + inner.strip() + "\n"
    if kind in ("paragraph", "bulletList", "orderedList", "codeBlock", "blockquote", "taskList"):
        return inner + ("\n" if not inner.endswith("\n") else "")
    if kind == "taskItem":
        return "- " + inner.strip() + "\n"
    return inner


def html_to_text(text: str) -> str:
    text = re.sub(r"<\s*(br|/p|/div|/li|/h\d)\s*/?>", "\n", text or "", flags=re.I)
    text = re.sub(r"<\s*li[^>]*>", "- ", text, flags=re.I)
    text = re.sub(r"<\s*h\d[^>]*>", "# ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", html.unescape(text)).strip()


def ac_section(text: str) -> str:
    """Return the lines under an 'Acceptance Criteria' heading, up to the next heading."""
    lines = (text or "").splitlines()
    for i, line in enumerate(lines):
        if AC_HEADING.match(line):
            out = []
            for nxt in lines[i + 1:]:
                if NEXT_HEADING.match(nxt) and not nxt.strip().startswith("-"):
                    break
                out.append(nxt)
            return "\n".join(out).strip()
    return ""


def _has_content(ac: str) -> bool:
    return bool(re.sub(r"[\s\-*•#:]+", "", ac or ""))


# ------------------------------------------------------------ fetchers
def fetch_ticket(ticket_id: str, cfg, session=None) -> Ticket:
    session = session or requests.Session()
    if cfg.ticket_system == "azure":
        return _fetch_azure(ticket_id, cfg, session)
    return _fetch_jira(ticket_id, cfg, session)


def _get(session, url, **kw):
    try:
        r = session.get(url, timeout=TIMEOUT, **kw)
    except requests.exceptions.RequestException as e:
        raise TicketUnavailable("Could not reach the ticket system.", detail=e.__class__.__name__)
    return r


def _fetch_jira(key, cfg, session):
    url = f"{cfg.jira_base_url}/rest/api/3/issue/{key}"
    r = _get(session, url, auth=(cfg.jira_email, cfg.jira_api_token),
             headers={"Accept": "application/json"})
    if r.status_code == 404:
        raise TicketUnavailable(f"Ticket {key} was not found in Jira (or the token cannot see it).")
    if r.status_code in (401, 403):
        raise TicketUnavailable(f"Jira refused access to {key} (HTTP {r.status_code}). Check JIRA_EMAIL / JIRA_API_TOKEN.")
    if r.status_code >= 400:
        raise TicketUnavailable(f"Jira returned HTTP {r.status_code} for {key}.")
    try:
        fields = r.json()["fields"]
    except (ValueError, KeyError, TypeError):
        raise TicketUnavailable(f"Jira returned an unreadable response for {key}.")

    description = adf_to_text(fields.get("description")).strip()
    ac = ""
    if cfg.jira_ac_field:
        raw = fields.get(cfg.jira_ac_field)
        ac = adf_to_text(raw).strip() if isinstance(raw, (dict, list)) else (raw or "").strip()
    if not _has_content(ac):
        ac = ac_section(description)
    if not _has_content(ac):
        raise NoAcceptanceCriteria(
            f"Ticket {key} has no acceptance criteria. Add an 'Acceptance Criteria' section to its "
            f"description{' or fill ' + cfg.jira_ac_field if cfg.jira_ac_field else ''}.")
    return Ticket(key, fields.get("summary", ""), description, ac, f"{cfg.jira_base_url}/browse/{key}")


def _fetch_azure(wid, cfg, session):
    url = f"{cfg.azure_org_url}/{cfg.azure_project}/_apis/wit/workitems/{wid}?api-version=7.1"
    r = _get(session, url, auth=("", cfg.azure_pat))
    if r.status_code == 404:
        raise TicketUnavailable(f"Work item {wid} was not found in Azure DevOps.")
    if r.status_code in (203, 401, 403):  # 203 = Azure's sign-in page when the PAT is wrong
        raise TicketUnavailable(f"Azure DevOps refused access to work item {wid}. Check AZURE_PAT.")
    if r.status_code >= 400:
        raise TicketUnavailable(f"Azure DevOps returned HTTP {r.status_code} for work item {wid}.")
    try:
        fields = r.json()["fields"]
    except (ValueError, KeyError, TypeError):
        raise TicketUnavailable(f"Azure DevOps returned an unreadable response for work item {wid}.")
    description = html_to_text(fields.get("System.Description", ""))
    ac = html_to_text(fields.get("Microsoft.VSTS.Common.AcceptanceCriteria", ""))
    if not _has_content(ac):
        ac = ac_section(description)
    if not _has_content(ac):
        raise NoAcceptanceCriteria(f"Work item {wid} has no acceptance criteria.")
    return Ticket(str(wid), fields.get("System.Title", ""), description, ac,
                  f"{cfg.azure_org_url}/{cfg.azure_project}/_workitems/edit/{wid}")
