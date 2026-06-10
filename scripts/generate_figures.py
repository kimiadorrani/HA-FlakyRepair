"""Generate thesis figures for the evaluation chapter."""

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap
import os

OUT = "/Users/admin/Desktop/POLITO/Thesis/HA-FlakyRepair/Figures"

MODELS = ["MiniMax M2.7", "DeepSeek V4 Flash", "GPT-OSS 120B"]
CATS   = ["NIO", "NOD", "OD-Vic", "OD-Brit"]
COLORS = ["#4C72B0", "#DD8452", "#55A868"]

plt.rcParams.update({
    "font.family":       "serif",
    "font.size":         11,
    "axes.titlesize":    12,
    "axes.labelsize":    11,
    "xtick.labelsize":   11,
    "ytick.labelsize":   10,
    "legend.fontsize":   10,
    "figure.dpi":        150,
    "axes.spines.top":   False,
    "axes.spines.right": False,
})


# ─────────────────────────────────────────────────────────────────────────────
# Figure 1 — Per-category F1 comparison
# ─────────────────────────────────────────────────────────────────────────────
f1 = {
    "MiniMax M2.7":      [0.879, 0.000, 0.731, 0.764],
    "DeepSeek V4 Flash": [0.871, 0.000, 0.767, 0.904],
    "GPT-OSS 120B":      [0.792, 0.000, 0.783, 0.949],
}

fig, ax = plt.subplots(figsize=(9, 5))
x = np.arange(len(CATS))
w = 0.24

all_bars = []
for i, (model, vals) in enumerate(f1.items()):
    bars = ax.bar(x + (i - 1) * w, vals, w,
                  label=model, color=COLORS[i], alpha=0.88,
                  edgecolor="white", linewidth=0.6)
    all_bars.append((bars, vals))

# Label bars only outside NOD (which are all 0)
for bars, vals in all_bars:
    for bar, v in zip(bars, vals):
        if v > 0:
            ax.text(bar.get_x() + bar.get_width() / 2,
                    v + 0.018,
                    f"{v:.2f}",
                    ha="center", va="bottom",
                    fontsize=7.5, color="#222222")

ax.set_xticks(x)
ax.set_xticklabels(CATS, fontsize=11)
ax.set_ylabel("F1 Score")
ax.set_ylim(0, 1.12)
ax.set_title("Per-Category F1 Score by Model (reproduced tests only)", pad=10)
ax.legend(loc="upper left", framealpha=0.85)
ax.axhline(0, color="black", linewidth=0.6)

# Shade NOD column
ax.axvspan(x[1] - 0.45, x[1] + 0.45, alpha=0.07, color="gray", zorder=0)
ax.text(x[1], 0.06, "all models\nF1 = 0.000",
        ha="center", va="bottom", fontsize=8, color="gray", style="italic")

fig.tight_layout()
fig.savefig(os.path.join(OUT, "f1_comparison.pdf"), bbox_inches="tight")
fig.savefig(os.path.join(OUT, "f1_comparison.png"), bbox_inches="tight")
plt.close(fig)
print("Figure 1 saved: f1_comparison")


# ─────────────────────────────────────────────────────────────────────────────
# Figure 2 — Reproduction rate by category and model
# ─────────────────────────────────────────────────────────────────────────────
repro = {
    "MiniMax M2.7":      [94.1, 44.4, 57.2, 81.6],
    "DeepSeek V4 Flash": [95.3, 66.7, 35.3, 45.3],
    "GPT-OSS 120B":      [91.8, 66.7, 54.9, 83.2],
}

fig, ax = plt.subplots(figsize=(9, 5))

all_bars = []
for i, (model, vals) in enumerate(repro.items()):
    bars = ax.bar(x + (i - 1) * w, vals, w,
                  label=model, color=COLORS[i], alpha=0.88,
                  edgecolor="white", linewidth=0.6)
    all_bars.append((bars, vals, i))

# Place labels inside bars (avoids all overlap)
for bars, vals, i in all_bars:
    for bar, v in zip(bars, vals):
        # put label inside the bar, near the top
        label_y = v - 4 if v > 12 else v + 2
        va = "top" if v > 12 else "bottom"
        color = "white" if v > 12 else "#222222"
        ax.text(bar.get_x() + bar.get_width() / 2,
                label_y,
                f"{v:.0f}%",
                ha="center", va=va,
                fontsize=8, color=color, fontweight="bold")

ax.set_xticks(x)
ax.set_xticklabels(CATS, fontsize=11)
ax.set_ylabel("Reproduction Rate (%)")
ax.set_ylim(0, 110)
ax.set_title("Reproduction Rate by Flaky Category and Model", pad=10)
ax.legend(loc="upper right", framealpha=0.85)
ax.axhline(0, color="black", linewidth=0.6)

fig.tight_layout()
fig.savefig(os.path.join(OUT, "reproduction_rate.pdf"), bbox_inches="tight")
fig.savefig(os.path.join(OUT, "reproduction_rate.png"), bbox_inches="tight")
plt.close(fig)
print("Figure 2 saved: reproduction_rate")


# ─────────────────────────────────────────────────────────────────────────────
# Figure 3 — Confusion matrix heatmaps (3 side by side)
# ─────────────────────────────────────────────────────────────────────────────
cms = {
    "MiniMax M2.7": np.array([
        [80,  0,   0,   0],
        [ 2,  0,   1,   1],
        [19,  0, 113,  14],
        [ 1,  0,  49, 105],
    ]),
    "DeepSeek V4 Flash": np.array([
        [81,  0,  0,  0],
        [ 3,  0,  0,  3],
        [20,  0, 56, 14],
        [ 1,  0,  0, 85],
    ]),
    "GPT-OSS 120B": np.array([
        [78,  0,  0,   0],
        [ 3,  0,  0,   3],
        [37,  0, 90,  13],
        [ 1,  0,  0, 157],
    ]),
}

cmap = LinearSegmentedColormap.from_list(
    "thesis", ["#f0f4ff", "#1a5fa8"], N=256
)

fig, axes = plt.subplots(1, 3, figsize=(13, 4.5),
                         gridspec_kw={"wspace": 0.38})

for ax, (model, cm) in zip(axes, cms.items()):
    row_sums = cm.sum(axis=1, keepdims=True).astype(float)
    row_sums[row_sums == 0] = 1
    cm_norm = cm / row_sums

    im = ax.imshow(cm_norm, vmin=0, vmax=1, cmap=cmap, aspect="auto")

    ax.set_xticks(range(4))
    ax.set_yticks(range(4))
    ax.set_xticklabels(CATS, rotation=35, ha="right", fontsize=9.5)
    ax.set_yticklabels(CATS, fontsize=9.5)
    ax.set_xlabel("Predicted", fontsize=10, labelpad=4)
    ax.set_ylabel("Actual",    fontsize=10, labelpad=4)
    ax.set_title(model, fontsize=10, pad=10)

    for r in range(4):
        for c in range(4):
            val  = cm[r, c]
            norm = cm_norm[r, c]
            txt_color = "white" if norm > 0.5 else "#111111"
            ax.text(c, r, str(val),
                    ha="center", va="center",
                    fontsize=9.5, color=txt_color, fontweight="bold")

    cb = fig.colorbar(im, ax=ax, shrink=0.82, pad=0.04,
                      ticks=[0, 0.25, 0.5, 0.75, 1.0])
    cb.ax.tick_params(labelsize=7.5)
    if ax is axes[-1]:
        cb.set_label("Row proportion", fontsize=8, labelpad=4)

fig.suptitle(
    "Confusion Matrices — Detection Agent (reproduced tests only)",
    fontsize=11, y=1.03
)
fig.savefig(os.path.join(OUT, "confusion_matrices.pdf"), bbox_inches="tight")
fig.savefig(os.path.join(OUT, "confusion_matrices.png"), bbox_inches="tight")
plt.close(fig)
print("Figure 3 saved: confusion_matrices")

print("\nAll figures written to", OUT)


# ─────────────────────────────────────────────────────────────────────────────
# Figure 4 — Repair summary: fix rate + efficiency (two panels)
# ─────────────────────────────────────────────────────────────────────────────

models_short = ["MiniMax\nM2.7", "DeepSeek\nV4 Flash", "GPT-OSS\n120B"]
x3 = np.arange(3)

fix_rate    = [84.3, 90.4, 79.9]
not_fixed   = [15.7,  9.6, 20.1]
avg_tokens  = [20795, 37926, 11865]   # per repair attempt
avg_dur     = [49.0,  72.7,  27.9]   # seconds per repair test

fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4.8),
                                gridspec_kw={"wspace": 0.38})

# ── Left panel: stacked bar (fixed / not fixed) ──────────────────────────────
bars_fixed = ax1.bar(x3, fix_rate,  0.45, label="Fixed",
                     color=COLORS, alpha=0.88, edgecolor="white")
bars_nf    = ax1.bar(x3, not_fixed, 0.45, bottom=fix_rate,
                     label="Not fixed", color="#cccccc",
                     alpha=0.75, edgecolor="white")

for bar, v in zip(bars_fixed, fix_rate):
    ax1.text(bar.get_x() + bar.get_width() / 2,
             v / 2,
             f"{v:.1f}%",
             ha="center", va="center",
             fontsize=10, color="white", fontweight="bold")

for bar, v in zip(bars_nf, not_fixed):
    ax1.text(bar.get_x() + bar.get_width() / 2,
             fix_rate[list(not_fixed).index(v)] + v / 2,
             f"{v:.1f}%",
             ha="center", va="center",
             fontsize=9, color="#444444")

ax1.set_xticks(x3)
ax1.set_xticklabels(models_short, fontsize=10)
ax1.set_ylabel("Percentage of repair attempts (%)")
ax1.set_ylim(0, 108)
ax1.set_title("Repair Outcome per Model", pad=8)
ax1.legend(loc="upper right", framealpha=0.85, fontsize=9)
ax1.axhline(0, color="black", linewidth=0.6)
ax1.spines["top"].set_visible(False)
ax1.spines["right"].set_visible(False)
ax1.text(-0.45, 103, "0 regressions across all models",
         fontsize=8, color="#555555", style="italic")

# ── Right panel: grouped bars — tokens and duration ─────────────────────────
w2 = 0.32
ax2r = ax2.twinx()

b_tok = ax2.bar(x3 - w2/2, [t/1000 for t in avg_tokens], w2,
                color=COLORS, alpha=0.75, edgecolor="white",
                label="Avg tokens (×1000)")
b_dur = ax2r.bar(x3 + w2/2, avg_dur, w2,
                 color=COLORS, alpha=0.45, edgecolor="white",
                 hatch="///", label="Avg duration (s)")

for bar, v in zip(b_tok, avg_tokens):
    ax2.text(bar.get_x() + bar.get_width() / 2,
             v/1000 + 0.5,
             f"{v//1000}k",
             ha="center", va="bottom", fontsize=8.5, color="#222222")

for bar, v in zip(b_dur, avg_dur):
    ax2r.text(bar.get_x() + bar.get_width() / 2,
              v + 1.2,
              f"{v:.0f}s",
              ha="center", va="bottom", fontsize=8.5, color="#444444")

ax2.set_xticks(x3)
ax2.set_xticklabels(models_short, fontsize=10)
ax2.set_ylabel("Avg tokens per repair (thousands)")
ax2r.set_ylabel("Avg duration per test (seconds)")
ax2.set_title("Repair Efficiency per Model", pad=8)
ax2.set_ylim(0, 55)
ax2r.set_ylim(0, 110)
ax2.spines["top"].set_visible(False)
ax2r.spines["top"].set_visible(False)

lines1, labels1 = ax2.get_legend_handles_labels()
lines2, labels2 = ax2r.get_legend_handles_labels()
ax2.legend(lines1 + lines2, labels1 + labels2,
           loc="upper left", fontsize=8.5, framealpha=0.85)

fig.suptitle("Repair Stage Summary", fontsize=12, y=1.01)
fig.savefig(os.path.join(OUT, "repair_summary.pdf"), bbox_inches="tight")
fig.savefig(os.path.join(OUT, "repair_summary.png"), bbox_inches="tight")
plt.close(fig)
print("Figure 4 saved: repair_summary")
