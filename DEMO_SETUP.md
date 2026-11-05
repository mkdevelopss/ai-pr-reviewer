# Live demo setup (about 20 minutes)

Use a **test repository and a test Jira project only**, never real projects.

## 1. GitHub test repo
1. Create a new repository, e.g. `pr-review-demo`, with a README.
2. Create a **fine-grained token** (GitHub → Settings → Developer settings → Fine-grained tokens), limited to that one repo, with:
   Pull requests: Read and write · Commit statuses: Read and write · Contents: Read and write.
3. Optional, so "Failed" really blocks merging: Settings → Branches → Add rule for `main` → *Require status checks to pass* → add `ai-pr-reviewer` (it appears after the first run).

## 2. Jira ticket (free Jira Cloud site)
1. Create a project with key `PROJ`.
2. Create ticket **PROJ-1** with this description:
   ```
   Add a utility module for user input checks.

   Acceptance Criteria
   - A function is_valid_email(email) returns True only for addresses with one @ and a dot in the domain
   - A function is_strong_password(pw) requires at least 8 characters, one digit and one uppercase letter
   - Both functions have unit tests
   ```
3. Create an API token at id.atlassian.com → Security → API tokens.

## 3. LLM key
Any OpenAI-compatible key works (OpenAI, or Groq's free tier), or an Anthropic key. Put it in `.env` as `LLM_API_KEY`.

## 4. Configure and start
```bash
cp .env.example .env      # fill GITHUB_TOKEN, WEBHOOK_SECRET, JIRA_*, LLM_*; keep DRY_RUN=1 for now
python app.py
```
In a second terminal, expose port 5000 with a tunnel: `ngrok http 5000` (or smee.io).
In the repo: Settings → Webhooks → Add webhook:
Payload URL `https://<tunnel>/webhook` · Content type `application/json` · Secret = `WEBHOOK_SECRET` · Event: *Pull requests*.

No tunnel? Skip the webhook and run `python review_pr.py <user>/pr-review-demo <PR number>` instead. It runs the same pipeline.

## 5. Run 1: ends in "Failed" with comments
Branch `feature/PROJ-1-validators`, add `validators.py` with **only** `is_valid_email` and no tests. Open a PR titled `PROJ-1: Add validators`.
Expected: a review comment listing the missing `is_strong_password` and the missing tests; status **ai-pr-reviewer: failure**.

## 6. Run 2: ends in a merge
On a new branch `feature/PROJ-1-validators-full`, add both functions and `test_validators.py`. Set `DRY_RUN=0`, restart `python app.py`, and open the PR.
Expected: status **success**, and the PR is **merged** automatically.

## 7. Show safe failures (optional, quick)
- PR titled `Update readme` from branch `docs-update` → "No ticket ID found" comment, failed, not merged.
- Ticket PROJ-2 with no "Acceptance Criteria" section → "has no acceptance criteria" comment, not merged.

Take screenshots of each PR page (comment + status + merged badge) and save the terminal output. That is the demo evidence.
