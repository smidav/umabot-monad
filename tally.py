"""
tally.py — Tracks enemy character appearances, strategies, and losses.

Alts are separate character entries:
  "Gold Ship"        → Base
  "Gold Ship-Summer" → Summer alt
"""

import json
import os
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from ocr import CharacterEntry

TALLY_FILE = Path("tally_data.json")


@dataclass
class CharacterStats:
    name: str
    total: int = 0
    losses: int = 0
    strategies: dict = field(default_factory=lambda: defaultdict(int))

    def record(self, strategy: str, lost_to: bool = False):
        self.total += 1
        self.strategies[strategy] += 1
        if lost_to:
            self.losses += 1

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "total": self.total,
            "losses": self.losses,
            "strategies": dict(self.strategies),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CharacterStats":
        obj = cls(name=data["name"], total=data["total"], losses=data.get("losses", 0))
        obj.strategies = defaultdict(int, data.get("strategies", {}))
        return obj


class Tally:
    def __init__(self):
        self.characters: dict[str, CharacterStats] = {}
        self.total_races_processed: int = 0
        self.last_winner: str | None = None   # composed name of last race winner, for undo
        self.load()

    def save(self):
        data = {
            "total_races_processed": self.total_races_processed,
            "last_winner": self.last_winner,
            "characters": {k: v.to_dict() for k, v in self.characters.items()},
        }
        with open(TALLY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def load(self):
        if TALLY_FILE.exists():
            try:
                with open(TALLY_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.total_races_processed = data.get("total_races_processed", 0)
                self.last_winner           = data.get("last_winner")
                self.characters = {
                    k: CharacterStats.from_dict(v)
                    for k, v in data.get("characters", {}).items()
                }
            except (json.JSONDecodeError, KeyError):
                self.characters = {}
                self.total_races_processed = 0
                self.last_winner = None

    def record_entries(self, entries: list[CharacterEntry], winner_name: str | None):
        """
        Record one race. winner_name is the composed character name that beat
        the player (e.g. "Gold Ship-Summer"), or None if the player won.
        """
        self.last_winner = winner_name
        for entry in entries:
            key = entry.name.lower()
            if key not in self.characters:
                self.characters[key] = CharacterStats(name=entry.name)
            lost_to = (winner_name is not None and entry.name == winner_name)
            self.characters[key].record(entry.strategy, lost_to)
        self.total_races_processed += 1
        self.save()

    def reset(self):
        self.characters = {}
        self.total_races_processed = 0
        self.last_winner = None
        if TALLY_FILE.exists():
            os.remove(TALLY_FILE)

    def sorted_entries(self) -> list[CharacterStats]:
        return sorted(self.characters.values(), key=lambda c: c.total, reverse=True)

    def format_tally_text(self, top_n: int = 25) -> str:
        entries = self.sorted_entries()
        if not entries:
            return "No data recorded yet."

        lines = [
            f"📊 **Umamusume Enemy Tally** — {self.total_races_processed} race(s) recorded\n",
            f"{'#':<4} {'Character':<28} {'Apps':>5} {'Losses':>7} {'Front':>7} {'Pace':>6} {'Late':>6} {'End':>5}",
            "─" * 72,
        ]
        for rank, stats in enumerate(entries[:top_n], 1):
            s = stats.strategies
            lines.append(
                f"{rank:<4} {stats.name:<28} {stats.total:>5} {stats.losses:>7} "
                f"{s.get('Front',0):>7} {s.get('Pace',0):>6} "
                f"{s.get('Late',0):>6} {s.get('End',0):>5}"
            )
        if len(entries) > top_n:
            lines.append(f"\n… and {len(entries)-top_n} more. Use `!uma export` for the full list.")
        return "\n".join(lines)

    def to_csv(self) -> str:
        lines = ["Rank,Character,Appearances,Losses,Front,Pace,Late,End"]
        for rank, stats in enumerate(self.sorted_entries(), 1):
            s = stats.strategies
            lines.append(
                f"{rank},{stats.name},{stats.total},{stats.losses},"
                f"{s.get('Front',0)},{s.get('Pace',0)},"
                f"{s.get('Late',0)},{s.get('End',0)}"
            )
        return "\n".join(lines)
