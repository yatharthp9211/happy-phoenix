"""
phoenix_store.py - the shared brain of the Phoenix main app and the island.

The floating Dynamic Island used to own the personality picker.  It does not
any more: the picker (and the mascot gallery it now lives next to) belongs to
the Tk main window, island_clone\\phoenix_control.py.  Both halves need to agree
on the same little pile of state - which mascot is on screen, what colour it
is, how big, which personality preset drives its animation, and the feature
switches from the island's Settings tab - so that pile lives here.

This module is deliberately dependency-free (json + os only).  The island is
loaded through a SourceFileLoader from the project root, the Tk window runs
from island_clone\\, and the offline checks import it directly; a file that
imports nothing cannot break in any of those three places.

    store = MascotStore()
    store.load()
    island.apply_store(store)          # -> island.set_mascots / set_mascot
    store.active = "nova"
    store.save()

Everything is validated on the way in.  A hand-edited or truncated JSON file
must never take the island down, so bad entries are dropped and replaced by
defaults rather than raising.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

#: Where the state lives, next to the chat history the island already writes.
STORE_PATH = os.path.join(os.path.expanduser("~"), ".phoenix", "control.json")

#: The six animation personalities the island knows about.  Kept as plain data
#: here (and in the island) so the picker can be built without pygame running.
PERSONALITIES: Dict[str, str] = {
    "normal": "Normal",
    "butler": "Butler",
    "brother": "Brother",
    "anime_girl": "Anime",
    "italian": "Italian",
    "russian": "Russian",
}

#: Shapes _draw_avatar knows how to outline.  "blob" is the original soft
#: rounded capsule; the rest are plain polygons.
SHAPES: Tuple[str, ...] = ("blob", "circle", "hex", "diamond", "triangle",
                           "star", "square", "emoji")

#: A small palette.  These are all light enough that the dimmest text drawn on
#: them still clears the 4.5:1 floor measured by scratch/check_transparency.py.
PALETTE: Tuple[Tuple[int, int, int], ...] = (
    (245, 248, 252),   # porcelain
    (255, 240, 246),   # blush
    (250, 244, 236),   # sand
    (243, 248, 246),   # mint
    (238, 242, 248),   # ice
    (244, 240, 252),   # lilac
    (255, 248, 232),   # cream
    (232, 244, 250),   # sky
)

ACCENTS: Tuple[Tuple[int, int, int], ...] = (
    (90, 170, 255),    # blue
    (255, 120, 175),   # pink
    (225, 120, 60),    # orange
    (90, 205, 170),    # teal
    (200, 170, 110),   # gold
    (120, 150, 215),   # periwinkle
    (255, 95, 95),     # red
    (140, 200, 90),    # lime
)

#: Emoji offered in the shape picker.  Short, widely supported, and each one
#: actually renders through pygame on Windows (verified in test_control_app).
EMOJI: Tuple[str, ...] = (
    "\U0001F600",   # grinning
    "\U0001F60A",   # smiling with hearts
    "\U0001F916",   # robot
    "\U0001F47B",   # ghost
    "\U0001F98A",   # fox
    "\U0001F419",   # octopus
    "\U0001F984",   # unicorn
    "\U0001F43C",   # panda
    "\U0001F414",   # chicken
    "\U0001F577",   # spider
    "\U0001F4A1",   # light bulb
    "\U0001F680",   # rocket
)

SIZE_MIN = 0.6
SIZE_MAX = 1.8
SIZE_DEFAULT = 1.0


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(v)))


def _rgb(value: Any, fallback: Tuple[int, int, int]) -> Tuple[int, int, int]:
    """Coerce anything into a sane 0-255 RGB triple."""
    try:
        seq = list(value)
    except TypeError:
        return fallback
    if len(seq) < 3:
        return fallback
    try:
        r, g, b = (int(seq[0]), int(seq[1]), int(seq[2]))
    except (TypeError, ValueError):
        return fallback
    clamp = lambda c: max(0, min(255, c))          # noqa: E731
    return (clamp(r), clamp(g), clamp(b))


def hex_to_rgb(text: Any, fallback: Tuple[int, int, int] = (245, 248, 252)):
    """'#rrggbb' -> (r, g, b).  Tk hands colour over as hex, so we speak hex."""
    if not isinstance(text, str):
        return fallback
    s = text.strip().lstrip("#")
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    if len(s) != 6 or not re.fullmatch(r"[0-9a-fA-F]{6}", s):
        return fallback
    return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))


def rgb_to_hex(rgb: Tuple[int, int, int]) -> str:
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(c))) for c in rgb)


_SLUG_BAD = re.compile(r"[^a-z0-9_]+")


def slugify(text: str, fallback: str = "mascot") -> str:
    """A stable, file-safe key for a mascot name."""
    s = unicodedata.normalize("NFKD", str(text or "")).encode(
        "ascii", "ignore").decode("ascii").lower()
    s = _SLUG_BAD.sub("_", s).strip("_")
    return s or fallback


def default_mascots() -> List[Dict[str, Any]]:
    """Three starters, each with its own personality, so the gallery is never
    empty on a fresh install and the difference is visible immediately."""
    return [
        dict(key="phoenix", name="Phoenix", personality="normal",
             head=PALETTE[0], accent=ACCENTS[0], shape="blob",
             emoji=EMOJI[0], size=1.0),
        dict(key="butler", name="Mr Butler", personality="butler",
             head=PALETTE[6], accent=ACCENTS[4], shape="square",
             emoji=EMOJI[5], size=0.9),
        dict(key="ember", name="Ember", personality="anime_girl",
             head=PALETTE[1], accent=ACCENTS[1], shape="star",
             emoji=EMOJI[2], size=1.15),
    ]


def normalise_mascot(raw: Any, fallback_key: str = "mascot") -> Dict[str, Any]:
    """One mascot dict, guaranteed to have every field the island needs.

    Unknown shapes and personalities fall back rather than raise: a bad file
    must not be able to stop the island from booting.
    """
    raw = raw if isinstance(raw, dict) else {}
    name = str(raw.get("name") or "").strip()[:24] or fallback_key.title()
    key = slugify(str(raw.get("key") or ""), slugify(name, fallback_key))
    personality = str(raw.get("personality") or "normal").strip().lower()
    if personality not in PERSONALITIES:
        personality = "normal"
    shape = str(raw.get("shape") or "blob").strip().lower()
    if shape not in SHAPES:
        shape = "blob"
    emoji = raw.get("emoji")
    emoji = emoji if isinstance(emoji, str) and emoji else ""
    return {
        "key": key[:32],
        "name": name,
        "personality": personality,
        "head": _rgb(raw.get("head"), PALETTE[0]),
        "accent": _rgb(raw.get("accent"), ACCENTS[0]),
        "shape": shape,
        "emoji": emoji,
        "size": round(_clamp(raw.get("size", SIZE_DEFAULT), SIZE_MIN, SIZE_MAX), 3),
    }


class MascotStore:
    """Load / save the shared state.  Every method is safe to call twice."""

    def __init__(self, path: Optional[str] = None):
        self.path = path or STORE_PATH
        self.mascots: List[Dict[str, Any]] = default_mascots()
        self.active: str = self.mascots[0]["key"]
        self.settings: Dict[str, Any] = {}
        self.last_error: Optional[str] = None

    # -- persistence -------------------------------------------------------
    def load(self) -> "MascotStore":
        """Read the file.  A missing or corrupt file is not an error the user
        should ever see - it just means we start from the defaults."""
        self.last_error = None
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return self
        except Exception as exc:                       # unreadable / bad JSON
            self.last_error = f"could not read {self.path}: {exc}"
            return self
        if not isinstance(data, dict):
            self.last_error = f"{self.path} is not a JSON object"
            return self
        self.apply(data)
        return self

    def apply(self, data: Dict[str, Any]):
        raw = data.get("mascots")
        if isinstance(raw, list) and raw:
            seen = set()
            built = []
            for i, item in enumerate(raw):
                m = normalise_mascot(item, fallback_key="mascot%d" % (i + 1))
                if m["key"] in seen:                   # duplicate keys break
                    m["key"] = "%s_%d" % (m["key"], i + 1)   # the active lookup
                    seen.add(m["key"])
                else:
                    seen.add(m["key"])
                built.append(m)
            self.mascots = built
        active = str(data.get("active") or "").strip()
        self.active = active if active in seen else self.mascots[0]["key"]
        settings = data.get("settings")
        if isinstance(settings, dict):
            self.settings = dict(settings)
        return self

    def save(self) -> bool:
        """Write atomically.  Returns False and sets last_error on failure -
        the caller keeps running, it just will not remember the change."""
        self.last_error = None
        payload = {"version": 1, "active": self.active,
                   "mascots": self.mascots, "settings": self.settings}
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
            os.replace(tmp, self.path)
            return True
        except Exception as exc:
            self.last_error = f"could not write {self.path}: {exc}"
            return False

    def to_dict(self) -> Dict[str, Any]:
        return {"version": 1, "active": self.active,
                "mascots": self.mascots, "settings": self.settings}

    # -- the API the two windows talk through ------------------------------
    def get(self, key: str) -> Optional[Dict[str, Any]]:
        for m in self.mascots:
            if m["key"] == key:
                return m
        return None

    def active_mascot(self) -> Dict[str, Any]:
        return self.get(self.active) or self.mascots[0]

    def upsert(self, mascot: Dict[str, Any]) -> Dict[str, Any]:
        clean = normalise_mascot(mascot)
        for i, m in enumerate(self.mascots):
            if m["key"] == clean["key"]:
                self.mascots[i] = clean
                return clean
        self.mascots.append(clean)
        return clean

    def delete(self, key: str) -> bool:
        """Never delete the last one - the island always needs a mascot."""
        if len(self.mascots) <= 1 or key == self.active:
            return False
        before = len(self.mascots)
        self.mascots = [m for m in self.mascots if m["key"] != key]
        return len(self.mascots) != before

    def unique_key(self, wanted: str) -> str:
        """A key nobody is using, so 'Nova' twice gives nova and nova_2."""
        taken = {m["key"] for m in self.mascots}
        key = slugify(wanted)
        if key not in taken:
            return key
        n = 2
        while f"{key}_{n}" in taken:
            n += 1
        return f"{key}_{n}"