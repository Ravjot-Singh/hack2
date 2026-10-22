"""Run every tenant's bot locally in polling mode (no public URL needed). Ctrl+C to stop.

    python scripts/run_bot.py
"""

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.bot.runner import run_polling_forever  # noqa: E402

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        asyncio.run(run_polling_forever())
    except KeyboardInterrupt:
        pass
