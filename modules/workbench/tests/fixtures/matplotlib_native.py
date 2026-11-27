"""Matplotlib fixture for Tavotto text, series, and native styling."""

import matplotlib.pyplot as plt

fig, ax = plt.subplots(figsize=(5, 3), layout="constrained")
fig.suptitle("Whole figure")
ax.set(title="Original title", xlabel="Time (s)", ylabel="Signal")
ax.plot([0, 1, 2], [1, 3, 2], label="Control")
ax.scatter([0, 1, 2], [2, 1, 3], label="Sample")
ax.bar([0, 1, 2], [0.4, 0.6, 0.4], width=0.16, label="Baseline")
ax.legend(title="Cohort")

# Capture even when a script saves and then closes its figure.
fig.savefig(fname="fixture-source-output.png", bbox_inches="tight")
plt.close(fig)
