"""Two Axes and one colorbar constitute an upstream semantic group."""

import matplotlib.pyplot as plt
from matplotlib.colors import Normalize

fig, (left, right) = plt.subplots(ncols=2, figsize=(6, 3), layout="constrained")
scale = Normalize(0, 4)
image = left.imshow([[1, 2], [3, 4]], cmap="viridis", norm=scale)
right.imshow([[4, 3], [2, 1]], cmap="viridis", norm=scale)
left.set(title="Left panel", xlabel="Left X")
right.set(title="Right panel", xlabel="Right X")
fig.colorbar(image, ax=[left, right], label="Shared scale")
