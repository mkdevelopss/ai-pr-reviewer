"""All settings come from environment variables (or a local .env file).

Secrets (tokens, API keys, webhook secret) are NEVER hard-coded, logged or
printed. `Config.describe()` shows only whether each secret is set.

Safety default: DRY_RUN is ON unless explicitly set to 0/false. In dry-run the
tool prints every comment, status and merge it WOULD make, and changes nothing.
"""

import os
from dataclasses import dataclass, field


def _load_dotenv(path=".env"):
    """Tiny .env loader (KEY=value lines). Real environment variables win."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _flag(name, default):
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value.strip().lower() not in ("0", "false", "no", "off")


@dataclass
class Config:
    # Git platform (GitHub)
    github_token: str = ""
    github_api: str = "https://api.github.com"
    webhook_secret: str = ""
    # Ticket system: "jira" or "azure"
    ticket_system: str = "jira"
    jira_base_url: str = ""          # e.g. https://yourname.atlassian.net
    jira_email: str = ""
    jira_api_token: str = ""
    jira_ac_field: str = ""          # optional custom field id, e.g. customfield_10050
    jira_project_keys: str = ""      # optional, e.g. "PROJ,SHOP": only these keys count as tickets
    azure_org_url: str = ""          # e.g. https://dev.azure.com/yourorg
    azure_project: str = ""
    azure_pat: str = ""
    # LLM
    llm_provider: str = "openai"     # "openai" (any OpenAI-compatible API) or "anthropic"
    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o"
    # Behaviour
    dry_run: bool = True
    max_diff_chars: int = 60_000     # larger diffs are NOT sent to the LLM -> no merge
    merge_method: str = "squash"     # merge | squash | rebase
    status_context: str = "ai-pr-reviewer"
    missing: list = field(default_factory=list)

    @classmethod
    def from_env(cls, dotenv=True):
        if dotenv:
            _load_dotenv()
        e = os.environ.get
        cfg = cls(
            github_token=e("GITHUB_TOKEN", ""),
            github_api=e("GITHUB_API_URL", "https://api.github.com").rstrip("/"),
            webhook_secret=e("WEBHOOK_SECRET", ""),
            ticket_system=e("TICKET_SYSTEM", "jira").strip().lower(),
            jira_base_url=e("JIRA_BASE_URL", "").rstrip("/"),
            jira_email=e("JIRA_EMAIL", ""),
            jira_api_token=e("JIRA_API_TOKEN", ""),
            jira_ac_field=e("JIRA_AC_FIELD", ""),
            jira_project_keys=e("JIRA_PROJECT_KEYS", ""),
            azure_org_url=e("AZURE_ORG_URL", "").rstrip("/"),
            azure_project=e("AZURE_PROJECT", ""),
            azure_pat=e("AZURE_PAT", ""),
            llm_provider=e("LLM_PROVIDER", "openai").strip().lower(),
            llm_api_key=e("LLM_API_KEY", ""),
            llm_base_url=e("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            llm_model=e("LLM_MODEL", "gpt-4o"),
            dry_run=_flag("DRY_RUN", True),
            max_diff_chars=int(e("MAX_DIFF_CHARS", "60000") or 60000),
            merge_method=e("MERGE_METHOD", "squash"),
        )
        cfg.missing = cfg.check()
        return cfg

    def check(self):
        """Names of required settings that are missing (empty list = ready)."""
        need = {"GITHUB_TOKEN": self.github_token, "LLM_API_KEY": self.llm_api_key}
        if self.ticket_system == "jira":
            need.update(JIRA_BASE_URL=self.jira_base_url, JIRA_EMAIL=self.jira_email,
                        JIRA_API_TOKEN=self.jira_api_token)
        elif self.ticket_system == "azure":
            need.update(AZURE_ORG_URL=self.azure_org_url, AZURE_PROJECT=self.azure_project,
                        AZURE_PAT=self.azure_pat)
        else:
            return [f"TICKET_SYSTEM must be 'jira' or 'azure' (got '{self.ticket_system}')"]
        if self.llm_provider not in ("openai", "anthropic"):
            return [f"LLM_PROVIDER must be 'openai' or 'anthropic' (got '{self.llm_provider}')"]
        return [k for k, v in need.items() if not v]

    def describe(self):
        """Safe summary for logs: secrets shown only as set / NOT SET."""
        s = lambda v: "set" if v else "NOT SET"
        return (f"mode={'DRY-RUN (no changes)' if self.dry_run else 'LIVE (will comment/merge)'} | "
                f"tickets={self.ticket_system} | llm={self.llm_provider}:{self.llm_model} | "
                f"GITHUB_TOKEN {s(self.github_token)} | WEBHOOK_SECRET {s(self.webhook_secret)} | "
                f"LLM_API_KEY {s(self.llm_api_key)}")
