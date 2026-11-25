"""Reset the study pointer and clear the one-shot chaos marker.

Resume is keyed on finished grid points, not on an index: points that already
have a result are not re-run, and points at least as aggressive as a crash are
skipped. Use `python -m tuner --retry-failed` to try those again on purpose.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tuner.state import Journal  # noqa: E402

j = Journal()
j.set_meta("next_index", "0")
j.delete_meta("chaos_done")

print("next_index =", j.get_meta("next_index"))
print("chaos_done =", j.get_meta("chaos_done"))
print("completed  =", j.completed_count())
print("unstable   =", len(j.unstable_params()), "config(s) will be skipped")
