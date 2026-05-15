"""
ocr.py — Local CV-based OCR for Umamusume race screenshots.
Uses OpenCV for card segmentation and pytesseract for text recognition.
Zero API calls. Requires: opencv-python-headless, pytesseract, tesseract binary.

  Ubuntu/Debian: sudo apt install tesseract-ocr
  macOS:         brew install tesseract
  Windows:       installer from https://github.com/UB-Mannheim/tesseract/wiki
                 (default path set below — adjust if you installed elsewhere)

Validated against three screenshots:
  - 1164x543  phone landscape  (test1.png)
  - 2304x1440 tablet landscape (Screenshot_20260501_151514_Umamusume.jpg)
  - 1202x707  PC windowed      (1777777271673_image.png)
  All: 18/18 correct names and strategies.

── Card segmentation ──────────────────────────────────────────────────────────
Teal racetrack background (HSV H 85-130, S ≥ 40) is used as the separator
between cards. Contiguous non-background runs ≥ 40px wide are card regions.
Player cards (>50% golden-yellow mid-band columns) are skipped.

── Name OCR ───────────────────────────────────────────────────────────────────
After rotating the name crop 90° CCW, Tesseract returns words with bounding
boxes. Words are grouped into clusters by vertical position gap (≥ 40px).
Every cluster and every adjacent-cluster pair is scored against the full
canonical name list. The best-scoring group wins — this avoids the assumption
that the character name is always the first or last cluster, which fails when
top-of-image noise (number badge artefacts, banner bleed) creates a cluster
above the actual name.

Scoring uses fuzzy ratio + substring matching + nospace matching (for OCR
that merges words like "MayanoTopGun" or "ChiyonoO"), and handles abbreviated
names like "T.M." that would otherwise be filtered as non-words.

── Strategy OCR ───────────────────────────────────────────────────────────────
Wider vertical crop (48%-84%) covers phone, tablet, and PC layouts where the
strategy pill sits at slightly different relative heights.
"""

import os
import sys
import cv2
import numpy as np
import pytesseract
import requests
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Optional

from constants import ALTS_DICT

# ── Tesseract binary path ─────────────────────────────────────────────────────
# Windows: adjust this path if Tesseract is installed elsewhere.
# macOS/Linux: the binary is found on PATH automatically; these lines are no-ops.
if sys.platform == "win32":
    _win_path = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    if os.path.exists(_win_path):
        pytesseract.pytesseract.tesseract_cmd = _win_path

# ── Constants ─────────────────────────────────────────────────────────────────

VALID_STRATEGIES = {"Front", "Pace", "Late", "End"}
MIN_CARD_WIDTH_PX = 40

CANONICAL_NAMES: list[str]       = list(ALTS_DICT.keys())
_LOWER_TO_CANONICAL: dict[str, str] = {n.lower(): n for n in CANONICAL_NAMES}
# Nospace lookup handles OCR-merged tokens like "MayanoTopGun" → "Mayano Top Gun"
_NOSPACE_TO_CANONICAL: dict[str, str] = {
    n.replace(' ', '').replace('.', '').lower(): n for n in CANONICAL_NAMES
}


# ── Data type ─────────────────────────────────────────────────────────────────

@dataclass
class CharacterEntry:
    name: str
    strategy: str
    gate: int = 0                  # gate number (1-9) derived from left-to-right card position
    alt: Optional[str] = None      # e.g. "Base", "Christmas" — None until user selects
    name_corrected: bool = False   # True when raw OCR was snapped to a canonical name

    def __repr__(self):
        flag    = " (corrected)" if self.name_corrected else ""
        alt_str = f" [{self.alt}]" if self.alt else ""
        return f"Gate {self.gate} {self.name}{alt_str}{flag} ({self.strategy})"


# ── Image loading ─────────────────────────────────────────────────────────────

def load_image(source: str) -> np.ndarray:
    """Load an image from a URL or local file path into a BGR numpy array."""
    if source.startswith("http://") or source.startswith("https://"):
        resp = requests.get(source, timeout=15)
        resp.raise_for_status()
        arr = np.frombuffer(resp.content, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    else:
        img = cv2.imread(source)
    if img is None:
        raise ValueError(f"Could not load image from: {source}")
    return img


# ── Card segmentation ─────────────────────────────────────────────────────────

def _is_background(col_hsv: np.ndarray) -> bool:
    """True if this column's average HSV matches the teal racetrack background."""
    hm = float(np.mean(col_hsv[:, 0]))
    sm = float(np.mean(col_hsv[:, 1]))
    return 85 <= hm <= 130 and sm >= 40


def _is_player_card(hsv: np.ndarray, x1: int, x2: int, y1: int, y2: int) -> bool:
    """True if >50% of mid-band columns are golden-yellow (player card)."""
    width = x2 - x1
    yellow = 0
    for x in range(x1, x2 + 1):
        col = hsv[y1:y2, x, :]
        hm = float(np.mean(col[:, 0]))
        sm = float(np.mean(col[:, 1]))
        vm = float(np.mean(col[:, 2]))
        if 15 <= hm <= 45 and sm >= 40 and vm >= 150:
            yellow += 1
    return yellow / width > 0.5


def find_enemy_card_regions(img: np.ndarray) -> list[tuple[int, int]]:
    """Return (x_start, x_end) for each enemy card column in the image."""
    h, w = img.shape[:2]
    hsv  = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

    y1 = int(h * 0.30)
    y2 = int(h * 0.55)

    labels = ['B' if _is_background(hsv[y1:y2, x, :]) else 'C' for x in range(w)]

    regions: list[tuple[int, int]] = []
    in_card, start = False, 0
    for x, lbl in enumerate(labels):
        if lbl == 'C' and not in_card:
            start = x
            in_card = True
        elif lbl == 'B' and in_card:
            if x - start >= MIN_CARD_WIDTH_PX:
                regions.append((start, x - 1))
            in_card = False
    if in_card and w - start >= MIN_CARD_WIDTH_PX:
        regions.append((start, w - 1))

    return [(x1, x2) for x1, x2 in regions
            if not _is_player_card(hsv, x1, x2, y1, y2)]


# ── Image preprocessing ───────────────────────────────────────────────────────

def _upscale(img: np.ndarray, factor: int) -> np.ndarray:
    return cv2.resize(img, None, fx=factor, fy=factor,
                      interpolation=cv2.INTER_CUBIC)


def _binarise(img: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if len(img.shape) == 3 else img
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return binary


# ── Name matching ─────────────────────────────────────────────────────────────

def _is_real_word(s: str) -> bool:
    """
    True if a token has enough alphabetic content to be real text.
    Also accepts abbreviated tokens like "T.M." (dots stripped before check).
    """
    stripped = s.replace('.', '').replace('-', '')
    alpha    = sum(c.isalpha() for c in stripped)
    return len(stripped) >= 2 and alpha / max(len(stripped), 1) > 0.5


def _score_group(word_list: list[str]) -> tuple[str, float]:
    """
    Score a list of OCR word tokens against all canonical names.
    Returns (best_canonical_name, score).

    Signals used (highest to lowest priority):
      1. Exact match (case-insensitive)
      2. Nospace exact match — handles CamelCase merges (MayanoTopGun)
      3. Fuzzy ratio on full joined string (× 10 weight)
      4. Per-token substring hits in canonical name (with and without spaces)
      5. Whole canonical name found inside a single merged token
    """
    raw       = ' '.join(word_list)
    raw_lower = raw.lower()
    raw_ns    = raw_lower.replace(' ', '').replace('.', '')

    if raw_lower in _LOWER_TO_CANONICAL:
        return _LOWER_TO_CANONICAL[raw_lower], 999.0
    if raw_ns in _NOSPACE_TO_CANONICAL:
        return _NOSPACE_TO_CANONICAL[raw_ns], 998.0

    scores: dict[str, float] = {}
    for c in CANONICAL_NAMES:
        c_lower = c.lower()
        c_ns    = c_lower.replace(' ', '').replace('.', '')

        s = SequenceMatcher(None, raw_lower, c_lower).ratio() * 10

        for tok in word_list:
            tok_ns = tok.lower().replace('.', '')
            if len(tok_ns) < 3:
                continue
            if tok_ns in c_ns:    s += 2   # token is substring of canonical (nospace)
            if tok_ns in c_lower: s += 2   # token is substring of canonical (with spaces)
            if len(tok_ns) >= 6 and c_ns in tok_ns:
                s += 3                      # whole canonical name inside merged token

        scores[c] = s

    best = max(scores, key=scores.get)
    return best, scores[best]


def _cluster_words(
    words: list[tuple[int, str]], gap_threshold: int = 40
) -> list[list[tuple[int, str]]]:
    """Split sorted (top, text) word list into clusters at gaps > threshold."""
    if not words:
        return []
    clusters, current = [], [words[0]]
    for i in range(1, len(words)):
        if words[i][0] - words[i - 1][0] > gap_threshold:
            clusters.append(current)
            current = []
        current.append(words[i])
    clusters.append(current)
    return clusters


# ── Per-card OCR ──────────────────────────────────────────────────────────────

def ocr_name(card_img: np.ndarray) -> tuple[str, bool]:
    """
    Read the character name from an enemy card. Returns (name, was_corrected).

    After rotating 90° CCW, Tesseract returns words at various vertical
    positions. We cluster them by gap and score every cluster (and every
    adjacent-cluster pair) against the canonical name list. The best-scoring
    group is the character name — this correctly handles:
      - Noise at y≈0 from the number badge / banner bleed
      - Character name in the middle
      - Trainer name at the bottom
    """
    ch, cw = card_img.shape[:2]
    # Crop name zone: below badge (8%), above strategy area (58%), excluding banner (78%)
    name_roi = card_img[int(ch * 0.08):int(ch * 0.58), :int(cw * 0.78)]
    rotated  = cv2.rotate(name_roi, cv2.ROTATE_90_COUNTERCLOCKWISE)
    proc     = _binarise(_upscale(rotated, 3))

    data = pytesseract.image_to_data(
        proc, config='--psm 6 --oem 3',
        output_type=pytesseract.Output.DICT
    )

    words = sorted([
        (data['top'][j], data['text'][j].strip())
        for j in range(len(data['text']))
        if data['text'][j].strip()
        and int(data['conf'][j]) >= 15
        and _is_real_word(data['text'][j].strip())
    ])

    if not words:
        return "Unknown", False

    clusters  = _cluster_words(words)
    # Candidates: each individual cluster + each adjacent pair of clusters
    candidates = list(clusters) + [
        clusters[i] + clusters[i + 1] for i in range(len(clusters) - 1)
    ]

    best_name, best_score = "Unknown", -1.0
    for group in candidates:
        tokens = [txt for _, txt in group]
        name, score = _score_group(tokens)
        if score > best_score:
            best_score = score
            best_name  = name

    corrected = best_name.lower() not in _LOWER_TO_CANONICAL
    return best_name, corrected


def ocr_strategy(card_img: np.ndarray) -> str:
    """
    Read the strategy pill (Front / Pace / Late / End).
    Wider crop (48%-84%) covers phone, tablet, and PC screen resolutions.
    """
    ch, cw = card_img.shape[:2]
    roi = card_img[int(ch * 0.48):int(ch * 0.84), :int(cw * 0.78)]
    txt = pytesseract.image_to_string(
        _binarise(_upscale(roi, 3)),
        config='--psm 6 --oem 3'
    )
    return next(
        (s for line in txt.splitlines() for s in VALID_STRATEGIES
         if s.lower() in line.lower()),
        "Unknown"
    )



# ── Public API ────────────────────────────────────────────────────────────────

def extract_race_data(image_source: str) -> list[CharacterEntry]:
    """
    Given a race lineup screenshot (URL or local path), return a CharacterEntry
    for each enemy character detected.

    Gate numbers are derived from left-to-right card position across ALL cards
    (player + enemy): cards appear highest-gate-left, so gate = total_cards - index.
    Alt is always None on return — the Discord UI handles selection after the fact.
    """
    img = load_image(image_source)

    # Get ALL card regions (player + enemy) to compute gate numbers correctly
    h, w   = img.shape[:2]
    hsv    = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    y1     = int(h * 0.30)
    y2     = int(h * 0.55)
    labels = ['B' if _is_background(hsv[y1:y2, x, :]) else 'C' for x in range(w)]

    all_regions: list[tuple[int, int]] = []
    in_card, start = False, 0
    for x, lbl in enumerate(labels):
        if lbl == 'C' and not in_card:
            start = x; in_card = True
        elif lbl == 'B' and in_card:
            if x - start >= MIN_CARD_WIDTH_PX:
                all_regions.append((start, x - 1))
            in_card = False
    if in_card and w - start >= MIN_CARD_WIDTH_PX:
        all_regions.append((start, w - 1))

    if not all_regions:
        raise ValueError(
            "No card regions detected. Ensure the image is an unmodified "
            "race lineup screenshot and is not heavily cropped."
        )

    total = len(all_regions)

    entries = []
    for pos, (x1, x2) in enumerate(all_regions):
        if _is_player_card(hsv, x1, x2, y1, y2):
            continue
        gate = total - pos          # leftmost card = highest gate number
        card = img[:, x1:x2 + 1]
        name, corrected = ocr_name(card)
        entries.append(CharacterEntry(
            name           = name,
            strategy       = ocr_strategy(card),
            gate           = gate,
            alt            = None,
            name_corrected = corrected,
        ))

    if not entries:
        raise ValueError("No enemy card regions detected.")

    return entries


# ── Debug helper ──────────────────────────────────────────────────────────────

def save_debug_image(image_source: str, output_path: str = "debug_cards.png"):
    """
    Save an annotated screenshot showing detected card regions.
    Green = enemy card. Yellow = player card (skipped).
    Run from a plain script, not the bot:

        from ocr import save_debug_image
        save_debug_image("screenshot.png")
    """
    img = load_image(image_source)
    h, w = img.shape[:2]
    hsv  = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

    y1, y2 = int(h * 0.30), int(h * 0.55)
    labels = ['B' if _is_background(hsv[y1:y2, x, :]) else 'C' for x in range(w)]

    regions: list[tuple[int, int]] = []
    in_card, start = False, 0
    for x, lbl in enumerate(labels):
        if lbl == 'C' and not in_card: start = x; in_card = True
        elif lbl == 'B' and in_card:
            if x - start >= MIN_CARD_WIDTH_PX: regions.append((start, x - 1))
            in_card = False
    if in_card and w - start >= MIN_CARD_WIDTH_PX:
        regions.append((start, w - 1))

    overlay = img.copy()
    for x1, x2 in regions:
        is_player = _is_player_card(hsv, x1, x2, y1, y2)
        color = (0, 200, 255) if is_player else (60, 220, 60)
        label = "player" if is_player else "enemy"
        cv2.rectangle(overlay, (x1, 0), (x2, h - 1), color, 3)
        cv2.putText(overlay, label, (x1 + 4, 24),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)

    cv2.imwrite(output_path, overlay)
    print(f"[debug] Saved → {output_path}  ({len(regions)} regions total)")
