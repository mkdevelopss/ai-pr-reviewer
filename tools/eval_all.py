"""Run the evaluator test in one go and save every output to eval/ (prints no keys).

    python tools/eval_all.py

1. Track A: create Jira ticket + PR from eval/ticket_config_apis.txt and eval/WUserController.js
2. Track A: review that PR live (comment + status on GitHub)
3. Track B: analyze https://github.com/psf/requests
"""
import os, re, subprocess, sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = "mkdevelopss/pr-review-demo"
TRACK_B = os.path.join(os.path.dirname(HERE), "track-b-repo-analyzer")


def run(cmd, cwd, log):
    print(f"\n$ {' '.join(cmd)}\n", flush=True)
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=ENV)
    out = p.stdout + p.stderr
    print(out, flush=True)
    with open(os.path.join(HERE, "eval", log), "w", encoding="utf-8") as fh:
        fh.write(f"$ {' '.join(cmd)}\n\n{out}")
    return out


os.makedirs(os.path.join(HERE, "eval"), exist_ok=True)
sys.path.insert(0, HERE)
from reviewer.config import Config
Config.from_env()   # loads .env into the environment, so Track B also gets GITHUB_TOKEN (5,000 requests/hour)
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
ENV = dict(os.environ, PYTHONIOENCODING="utf-8")
ONLY_B = "--trackb" in sys.argv
FEEDBACK_PR = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--feedback=")), None)

if FEEDBACK_PR:
    print("=" * 70 + f"\nTRACK A: show AI feedback for PR #{FEEDBACK_PR} (practice run, changes nothing)\n" + "=" * 70)
    subprocess.run([sys.executable, "review_pr.py", REPO, FEEDBACK_PR], cwd=HERE, env=dict(ENV, DRY_RUN="1"))
    sys.exit(0)
if ONLY_B:
    pass
else:
  print("=" * 70 + "\nTRACK A - step 1: create Jira ticket + PR from the evaluator files\n" + "=" * 70)
  out = run([sys.executable, "tools/setup_eval.py", REPO, "eval/ticket_config_apis.txt",
             "eval/WUserController.js", "controllers/WUserController.js"], HERE, "trackA_1_setup.txt")
  m = re.search(r"PR #(\d+)", out)
  if not m:
      sys.exit("Could not create the PR - see the error above.")
  pr = m.group(1)

  print("=" * 70 + f"\nTRACK A - step 2: AI review of PR #{pr} (live: comment + status)\n" + "=" * 70)
  run([sys.executable, "review_pr.py", REPO, pr, "--live"], HERE, "trackA_2_review.txt")
  print(f"Open on GitHub: https://github.com/{REPO}/pull/{pr}")

print("=" * 70 + "\nTRACK B: analyze https://github.com/psf/requests\n" + "=" * 70)
if os.path.isdir(TRACK_B):
    env_cmd = [sys.executable, "-X", "utf8", "analyze.py", "https://github.com/psf/requests",
               "--save", os.path.join(HERE, "eval", "trackB_requests_report.txt")]
    run(env_cmd, TRACK_B, "trackB_run.txt")
else:
    print(f"Track B folder not found at {TRACK_B} - run analyze.py there manually.")
print("\nDONE. All outputs saved in the eval folder.")
