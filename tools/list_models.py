"""Print the model names your LLM provider offers (uses LLM_API_KEY from .env, never prints it).

    python tools/list_models.py
"""

import os
import sys

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from reviewer.config import Config  # noqa: E402

cfg = Config.from_env()
print(f"Provider URL: {cfg.llm_base_url}   current LLM_MODEL: {cfg.llm_model}")
try:
    r = requests.get(f"{cfg.llm_base_url}/models", timeout=20,
                     headers={"Authorization": f"Bearer {cfg.llm_api_key}"})
except requests.exceptions.RequestException as e:
    sys.exit(f"Could not reach {cfg.llm_base_url}: {e.__class__.__name__}")
if r.status_code != 200:
    sys.exit(f"HTTP {r.status_code}: {r.text[:300]}")
ids = sorted(m.get("id", "") for m in r.json().get("data", []))
print(f"\n{len(ids)} models available:")
for i in ids:
    print(("  * " if i == cfg.llm_model else "    ") + i)
if cfg.llm_model not in ids:
    print(f"\n'{cfg.llm_model}' is NOT in this list. Put one of the names above in LLM_MODEL in .env.")
else:
    print(f"\n'{cfg.llm_model}' is available.")
