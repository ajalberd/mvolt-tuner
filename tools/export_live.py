"""Write state/journal -> data/live.json for the web UI."""
import sys
from pathlib import Path

# Runnable as `python tools\export_live.py` from the project root without installing.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tuner.config import LIVE_JSON  # noqa: E402
from tuner.state import Journal  # noqa: E402

j = Journal()
running = "--running" in sys.argv
j.write_live(LIVE_JSON, running=running)
rows = j.trials()
print(f"wrote {LIVE_JSON} with {len(rows)} trials (completed={j.completed_count()})")
