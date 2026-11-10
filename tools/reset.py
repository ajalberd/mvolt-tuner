"""Reset the study pointer and clear the one-shot chaos marker."""
from tuner.state import Journal

j = Journal()
j.set_meta("next_index", "0")
with j._lock:
    j.conn.execute("DELETE FROM meta WHERE key='chaos_done'")
    j.conn.commit()

print("next_index =", j.get_meta("next_index"))
print("chaos_done =", j.get_meta("chaos_done"))
print("completed  =", j.completed_count())
