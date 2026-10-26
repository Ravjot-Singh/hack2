"""Run every feature check in an isolated sandbox and print the results as JSON (last line of stdout).

    python -m app.checks [--rules rules.json]

The sandbox is a fresh SQLite file, the LLM is off, no public URL and no bot token are set, and Telegram is a
fake, so this never touches the real database, Telegram or Groq. The portal starts it as a separate process.
"""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rules", help="JSON file with the business's rules to copy into the sandbox")
    args = ap.parse_args()
    rules = json.loads(Path(args.rules).read_text(encoding="utf-8")) if args.rules else None

    with tempfile.TemporaryDirectory() as tmp:
        # Must happen before anything from app.* is imported: settings and the engine read the environment once.
        os.environ.update(DATABASE_URL=f"sqlite:///{Path(tmp) / 'checks.db'}", PUBLIC_BASE_URL="",
                          LLM_ENABLED="false", GROQ_API_KEY="", SHOPKART_BOT_TOKEN="")
        from app.checks.scenarios import run_all
        from app.db.session import get_engine
        try:
            report = run_all(rules)
        finally:
            get_engine().dispose()
    sys.stdout.write("\n" + json.dumps(report) + "\n")


if __name__ == "__main__":
    main()
