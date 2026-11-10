"""Write state/journal -> data/live.json for the web UI."""
import sys

from tuner.config import DATA
from tuner.state import Journal

j = Journal()
running = "--running" in sys.argv
j.write_live(DATA / "live.json", running=running)
rows = j.trials()
print(f"wrote {DATA / 'live.json'} with {len(rows)} trials (completed={j.completed_count()})")
