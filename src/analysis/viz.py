"""Shared chart styling, so every figure in the project reads as one system.

Colour is assigned by the job it does, never by taste:

* **Categorical** (archetype, channel, arm) — a fixed hue order, assigned by slot
  and never cycled, so a chart that drops a series does not repaint the
  survivors. A reader who learned "early_dropper is orange" keeps that.
* **Sequential** (magnitude) — a single hue, light to dark.
* **Status** (good / critical) — a small reserved scale on steps deliberately
  distinct from the categorical slots, so a status colour can never be mistaken
  for "another series". Always paired with a label, never carrying meaning alone.

Palette provenance
------------------
These are documented, pre-validated hex values, not eyeballed ones. The four
categorical slots clear every hard gate in both modes on the *adjacent* pairlist,
which is the correct pairlist for lines and bars where only neighbouring series
touch:

=====  =======================  ================================
mode   worst adjacent CVD ΔE    worst adjacent normal-vision ΔE
=====  =======================  ================================
light  9.1  (target ≥ 8)        22.9 (floor ≥ 15)
dark   8.4  (target ≥ 8)        19.8 (floor ≥ 15)
=====  =======================  ================================

Two light-mode slots — aqua ``#1baf7a`` at 2.74:1 and yellow ``#eda100`` at
2.11:1 — sit below the 3:1 contrast-vs-surface bar. That relaxation is legal
*only* where the values are readable through another channel, so every
multi-series figure here ships direct end-labels and prints its underlying
numbers to the log as a table. Those are the relief that makes the two slots
legal; they are not decoration, and removing them would make the chart
non-compliant rather than merely plainer.

Dark mode is a *selected* set of steps for the dark surface, not an inversion of
light. Flipping light hues lands them outside the dark lightness band, where they
stop being distinguishable from each other.

House rules encoded below so they cannot be forgotten per-chart: lines 2px,
markers ≥ 8px carrying a 2px surface-coloured ring, confidence bands as a ~10%
wash rather than a saturated block, grid and axes as solid recessive hairlines
(never dashed — a dashed rule reads as a threshold), text always in a text token
rather than the series colour, and never a second y-axis.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import matplotlib as mpl
import matplotlib.pyplot as plt

FIGURE_DIR = Path("reports/figures")

# System UI sans first, with the faces matplotlib actually ships as the tail.
FONT_STACK = ["Helvetica Neue", "Helvetica", "Arial", "DejaVu Sans", "sans-serif"]


@dataclass(frozen=True)
class Theme:
    """One resolved set of colour tokens."""

    name: str
    surface: str
    ink_primary: str
    ink_secondary: str
    ink_muted: str
    grid: str
    axis: str
    series: tuple[str, ...]
    status_good: str
    status_critical: str
    deemphasis: str


LIGHT = Theme(
    name="light",
    surface="#fcfcfb",
    ink_primary="#0b0b0b",
    ink_secondary="#52514e",
    ink_muted="#898781",
    grid="#e1e0d9",
    axis="#c3c2b7",
    series=("#2a78d6", "#eb6834", "#1baf7a", "#eda100"),
    status_good="#0ca30c",
    status_critical="#d03b3b",
    deemphasis="#c3c2b7",
)

DARK = Theme(
    name="dark",
    surface="#1a1a19",
    ink_primary="#ffffff",
    ink_secondary="#c3c2b7",
    ink_muted="#898781",
    grid="#2c2c2a",
    axis="#383835",
    series=("#3987e5", "#d95926", "#199e70", "#c98500"),
    status_good="#0ca30c",
    status_critical="#d03b3b",
    deemphasis="#52514e",
)

THEMES: dict[str, Theme] = {"light": LIGHT, "dark": DARK}

# Slot per archetype, fixed across every figure so identity never shifts.
ARCHETYPE_SLOT = {
    "sticky_former": 0,
    "early_dropper": 1,
    "weekday_only": 2,
    "payday_spiker": 3,
}


def archetype_color(name: str, theme: Theme) -> str:
    """Stable colour for an archetype, by slot rather than by iteration order."""
    return theme.series[ARCHETYPE_SLOT.get(name, 0) % len(theme.series)]


def apply_style(theme: Theme) -> None:
    """Install the house matplotlib style for a theme. Call before plotting."""
    mpl.rcParams.update(
        {
            "figure.facecolor": theme.surface,
            "axes.facecolor": theme.surface,
            "savefig.facecolor": theme.surface,
            "font.family": "sans-serif",
            "font.sans-serif": FONT_STACK,
            "font.size": 10,
            "text.color": theme.ink_primary,
            "axes.edgecolor": theme.axis,
            "axes.linewidth": 0.8,
            "axes.grid": True,
            "axes.grid.axis": "y",
            "grid.color": theme.grid,
            "grid.linewidth": 0.7,
            "grid.linestyle": "-",  # solid: dashes read as a threshold, not a grid
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.labelcolor": theme.ink_secondary,
            "axes.titlecolor": theme.ink_primary,
            "axes.titlesize": 12,
            "axes.titleweight": "600",
            "axes.titlelocation": "left",
            "axes.titlepad": 12,
            "xtick.color": theme.ink_muted,
            "ytick.color": theme.ink_muted,
            "xtick.labelsize": 9,
            "ytick.labelsize": 9,
            "legend.frameon": False,
            "legend.fontsize": 9,
            "legend.labelcolor": theme.ink_secondary,
            "lines.linewidth": 2.0,
            "lines.solid_capstyle": "round",
            "lines.solid_joinstyle": "round",
            "figure.dpi": 130,
            "savefig.dpi": 160,
        }
    )


def marker(ax: plt.Axes, x: float, y: float, color: str, theme: Theme, size: float = 8.0) -> None:
    """Draw an end-marker carrying the 2px surface ring.

    The ring is what keeps a marker legible where it sits on its own line or
    overlaps a neighbour. It is negative space doing the separating — not a
    border stroked around the mark, which would add ink that isn't data.
    """
    ax.plot(
        [x],
        [y],
        marker="o",
        markersize=size,
        color=color,
        markeredgecolor=theme.surface,
        markeredgewidth=2.0,
        linestyle="none",
        zorder=6,
    )


def label_line_end(
    ax: plt.Axes,
    x: float,
    y: float,
    text: str,
    theme: Theme,
    *,
    dx: float = 8.0,
    dy: float = 0.0,
) -> None:
    """Direct-label a curve at its right end, in a text token.

    Deliberately *not* in the series colour. Two of the four categorical slots
    are below 3:1 on the light surface and would be unreadable as text; identity
    is carried by the coloured marker placed beside the label, never by tinting
    the words.
    """
    ax.annotate(
        text,
        xy=(x, y),
        xytext=(dx, dy),
        textcoords="offset points",
        va="center",
        fontsize=9,
        color=theme.ink_secondary,
    )


def save(fig: plt.Figure, name: str, theme: Theme) -> Path:
    """Write a figure under ``reports/figures/<theme>`` and close it.

    ``reports/`` is gitignored: figures are regenerable output, and a binary that
    changes on every run has no place in version history.
    """
    out_dir = FIGURE_DIR / theme.name
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{name}.png"
    fig.savefig(path, bbox_inches="tight", facecolor=theme.surface)
    plt.close(fig)
    return path
