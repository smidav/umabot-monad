"""
alt_matcher.py — Identifies which alt (version) of a character is shown in a
card portrait by comparing HSV colour histograms.

Asset images go in the `assets/` subdirectory, named {uma_number}_{alt:02d}.png:

    assets/
        1006_01.png   ← Oguri Cap, Base
        1006_02.png   ← Oguri Cap, Christmas
        1037_01.png   ← Eishin Flash, Base
        1037_02.png   ← Eishin Flash, Valentine
        ...

Assets are 512×512 RGBA PNGs (transparent background) from the game data.

How it works
────────────
The portrait crop passed in covers the full visible character region on the
card (top ~8% to ~65% of card height) — head, accessories, and collar.

Both the portrait and each asset are histogrammed in hue×saturation space,
with dark pixels (hair, which is identical across alts) and near-white pixels
(card background, skin highlights) excluded. What remains is dominated by
outfit and accessory colours, which DO differ between alts.

The asset is used in full (not cropped to an outfit region) because the
portrait crop already excludes most of the lower body — using the whole asset
gives more matching signal, not less.

Matching is Bhattacharyya distance — lower = more similar. The closest alt wins.

If no assets exist for a character, identify_alt() returns None and the caller
omits the alt field rather than guessing.
"""

import cv2
import numpy as np
from pathlib import Path
from typing import Optional

from constants import ALTS_DICT

# ── Config ────────────────────────────────────────────────────────────────────

ASSETS_DIR = Path(__file__).parent / "assets"

# 2D hue × saturation histogram (value/brightness ignored — too sensitive to
# JPEG compression and lighting differences between asset renders and screenshots)
HIST_BINS  = [18, 8]
HIST_RANGE = [0, 180, 0, 256]

# ── Name → uma_number lookup ──────────────────────────────────────────────────

_NAME_TO_NUMBER: dict[str, str] = {
    name: info["uma_number"] for name, info in ALTS_DICT.items()
}

# ── Asset cache ───────────────────────────────────────────────────────────────
# Populated lazily on first call to identify_alt().
# Key: (uma_number, alt_index)  Value: normalised histogram or None

_asset_cache: dict[tuple[str, int], Optional[np.ndarray]] = {}
_cache_built = False


# ── Histogram helpers ─────────────────────────────────────────────────────────

def _build_hist(bgr: np.ndarray, mask: np.ndarray) -> Optional[np.ndarray]:
    """
    Build a normalised 2D hue×saturation histogram over pixels selected by
    `mask`, with two additional exclusions applied on top:
      - Very dark pixels (V ≤ 80): hair, which looks the same across alts
      - Near-white pixels (S < 20 and V > 230): background and skin highlights

    Returns None if fewer than 50 usable pixels remain.
    """
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    v   = hsv[:, :, 2]
    s   = hsv[:, :, 1]

    colour_mask = mask & (v > 80) & ~((s < 20) & (v > 230))

    if colour_mask.sum() < 50:
        return None

    hist = cv2.calcHist(
        [hsv], [0, 1],
        colour_mask.astype(np.uint8),
        HIST_BINS, HIST_RANGE,
    )
    cv2.normalize(hist, hist)
    return hist


def _load_asset_hist(path: Path) -> Optional[np.ndarray]:
    """
    Load one asset PNG and return its colour histogram over the full image.

    We use the FULL asset (not cropped to an outfit region) because the
    portrait crop already shows mostly head/collar — using the whole asset
    gives more signal to match against, not less.
    """
    img = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if img is None:
        return None

    bgr = img[:, :, :3]
    if img.shape[2] == 4:
        fg_mask = img[:, :, 3] > 30
    else:
        gray    = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        fg_mask = gray < 245

    return _build_hist(bgr, fg_mask)


# ── Cache population ──────────────────────────────────────────────────────────

def _ensure_cache() -> None:
    """Scan assets/ and populate _asset_cache on first call."""
    global _cache_built
    if _cache_built:
        return
    _cache_built = True

    if not ASSETS_DIR.exists():
        return

    for path in sorted(ASSETS_DIR.glob("*.png")):
        parts = path.stem.split("_")
        if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        uma_number = parts[0]
        alt_index  = int(parts[1])
        _asset_cache[(uma_number, alt_index)] = _load_asset_hist(path)


# ── Public API ────────────────────────────────────────────────────────────────

def identify_alt(character_name: str, portrait_bgr: np.ndarray) -> Optional[str]:
    """
    Return the alt label (e.g. "Base", "Valentine") for the character shown
    in the portrait crop, or None if no assets are loaded for this character.

    Args:
        character_name: Canonical name from ALTS_DICT (e.g. "Eishin Flash")
        portrait_bgr:   Full character region crop from the card — should span
                        from just below the number badge down to where the
                        racetrack background starts (~8%–65% of card height).
                        Background pixels are masked out internally.
    """
    _ensure_cache()

    uma_number = _NAME_TO_NUMBER.get(character_name)
    if uma_number is None:
        return None

    # Build portrait histogram, masking teal racetrack background
    p_hsv = cv2.cvtColor(portrait_bgr, cv2.COLOR_BGR2HSV)
    not_teal  = ~((p_hsv[:, :, 0] >= 85) & (p_hsv[:, :, 0] <= 130) &
                  (p_hsv[:, :, 1] >= 40))
    not_white = ~((p_hsv[:, :, 1] < 20) & (p_hsv[:, :, 2] > 230))

    portrait_hist = _build_hist(portrait_bgr, not_teal & not_white)
    if portrait_hist is None:
        return None

    # Compare against every loaded alt for this character
    best_score: float         = float("inf")
    best_label: Optional[str] = None

    for alt_index, alt_label in ALTS_DICT[character_name]["alts"].items():
        asset_hist = _asset_cache.get((uma_number, alt_index))
        if asset_hist is None:
            continue
        score = cv2.compareHist(portrait_hist, asset_hist, cv2.HISTCMP_BHATTACHARYYA)
        if score < best_score:
            best_score = score
            best_label = alt_label

    return best_label


def loaded_characters() -> list[str]:
    """Return canonical names of characters that have at least one asset loaded."""
    _ensure_cache()
    loaded_numbers = {uma for (uma, _), h in _asset_cache.items() if h is not None}
    number_to_name = {v: k for k, v in _NAME_TO_NUMBER.items()}
    return sorted(number_to_name[n] for n in loaded_numbers if n in number_to_name)
