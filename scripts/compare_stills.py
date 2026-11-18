"""Compare two still images of the same view, for the before/after image-quality check.

Usage:
    python scripts/compare_stills.py BASELINE.png FIX.png OUT.png

Prints how different the images are and writes one figure with a zoomed crop of
each image and a difference map. A speedup only counts as a fix if the image
still looks right, so this goes next to the timing result in the report.

PSNR (peak signal-to-noise ratio, in dB) is the standard single number for this:
higher means closer; above about 40 dB differences are very hard to see, and
identical images have no finite PSNR.
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.image as mpimg  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402


def load_rgb(path: Path) -> np.ndarray:
    img = mpimg.imread(path)
    if img.dtype != np.float32 and img.dtype != np.float64:
        img = img / 255.0
    return np.asarray(img[..., :3], dtype=np.float64)


def image_difference(a: np.ndarray, b: np.ndarray) -> dict:
    if a.shape != b.shape:
        raise ValueError(f"images differ in size: {a.shape} vs {b.shape}")
    diff = np.abs(a - b)
    mse = float(np.mean((a - b) ** 2))
    return {
        "mean_abs_diff_8bit": float(diff.mean() * 255),
        "max_abs_diff_8bit": float(diff.max() * 255),
        "pixels_changed_pct": float(100 * np.mean(diff.max(axis=2) * 255 >= 2)),
        "psnr_db": math.inf if mse == 0 else 10 * math.log10(1.0 / mse),
        "diff": diff.max(axis=2),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("fix", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()

    a, b = load_rgb(args.baseline), load_rgb(args.fix)
    d = image_difference(a, b)
    h, w = a.shape[:2]
    # Zoom into the center quarter, where shadows of the nearest objects fall.
    y0, x0 = h // 2 - h // 8, w // 2 - w // 8
    crop = (slice(y0, y0 + h // 4), slice(x0, x0 + w // 4))

    fig, axes = plt.subplots(1, 3, figsize=(15, 4.6), facecolor="#fcfcfb")
    for ax, img, title in [(axes[0], a[crop], "Baseline (zoomed)"), (axes[1], b[crop], "Fix (zoomed)")]:
        ax.imshow(img, interpolation="nearest")
        ax.set_title(title, fontsize=11)
    im = axes[2].imshow(d["diff"][crop] * 255, cmap="magma", vmin=0, vmax=max(8, d["max_abs_diff_8bit"]))
    axes[2].set_title("Difference (0 to 255 per pixel)", fontsize=11)
    fig.colorbar(im, ax=axes[2], fraction=0.046)
    for ax in axes:
        ax.set_xticks([])
        ax.set_yticks([])
    psnr = "identical" if math.isinf(d["psnr_db"]) else f"PSNR {d['psnr_db']:.1f} dB"
    fig.suptitle(f"Image check: {psnr}, mean difference {d['mean_abs_diff_8bit']:.2f} of 255, "
                 f"{d['pixels_changed_pct']:.1f}% of pixels changed", x=0.01, ha="left", fontsize=12)
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(args.out, dpi=120, facecolor="#fcfcfb")

    print(f"PSNR: {d['psnr_db']:.2f} dB")
    print(f"Mean absolute difference: {d['mean_abs_diff_8bit']:.3f} of 255")
    print(f"Max absolute difference: {d['max_abs_diff_8bit']:.0f} of 255")
    print(f"Pixels changed by 2 or more levels: {d['pixels_changed_pct']:.2f}%")
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
