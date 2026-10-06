"""Plot your focus log. Run it yourself:

    python plot_focus.py                       # reads the vault path from assistant config
    python plot_focus.py path/to/focus.jsonl

Top: every block, planned vs actual minutes, with the trainer level.
Bottom: total focused minutes per day.
"""
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import matplotlib.pyplot as plt

if len(sys.argv) > 1:
    path = Path(sys.argv[1])
else:
    from assistant.config import data_dir, load_config
    path = data_dir(load_config()) / "logs" / "focus.jsonl"

blocks = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
if not blocks:
    sys.exit(f"No blocks in {path} yet.")

t = [datetime.fromisoformat(b["start"]) for b in blocks]
x = range(len(blocks))
fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 7))

ax1.bar(x, [b["planned_min"] for b in blocks], color="lightgray", label="planned")
ax1.bar(x, [b["actual_min"] for b in blocks],
        color=["tab:green" if b["completed"] else "tab:red" for b in blocks], alpha=0.8,
        label="actual (green = finished, red = bailed)")
ax1.step(x, [b["level_after"] for b in blocks], where="mid", color="black", label="trainer level")
for i, b in enumerate(blocks):
    if b["distractions"]:
        ax1.annotate(str(b["distractions"]), (i, b["actual_min"]), ha="center", va="bottom", fontsize=8)
ax1.set_ylabel("minutes")
ax1.set_xlabel("block # (numbers = distractions)")
ax1.legend(loc="upper left", fontsize=8)

per_day = defaultdict(float)
for d, b in zip(t, blocks):
    per_day[d.date()] += b["actual_min"]
days = sorted(per_day)
ax2.bar(days, [per_day[d] for d in days])
ax2.set_ylabel("focused min / day")
fig.autofmt_xdate()
fig.tight_layout()
plt.show()
