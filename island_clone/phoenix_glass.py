"""
phoenix_glass.py - the semi-transparency behind the main app window.

The window is a frosted slab you can see your desktop through.  Two separate
things do that, and they are not the same thing:

  1. LEVEL.  How much of the desktop shows through.  Tk can do this itself
     with ``wm attributes -alpha``, but this module also talks to
     SetLayeredWindowAttributes directly so the level survives a Tk that
     does not implement it (and so the island's own proven Win32 path is the
     one in charge).

  2. BACKDROP.  Whether Windows blurs the desktop *before* the window is
     composited on top of it.  Tk has no API for this at all, so it is a
     DWM call: the native Win11 ``DWMWA_SYSTEMBACKDROP_TYPE`` (acrylic,
     build 22621+) with the older undocumented
     ``SetWindowCompositionAttribute`` accent states behind it.

WHY THE PALETTE IS DARK.  The obvious way to build glass - lighten the
panels - makes it *less* readable, because a lighter panel eats the contrast
with light text.  So this palette goes the other way: the backgrounds are
deep and slightly blue, and the muted text is a little lighter than it used
to be.  The measured result is that every text/background pair still clears
WCAG AA (4.5:1) against the worst possible backdrop - a pure white desktop -
all the way down to alpha 0.80, instead of only 0.93.  That headroom is what
buys a translucency you can actually notice.

    worst pair (muted text on a card):
        alpha 1.00   7.24:1
        alpha 0.88   5.55:1   <- the shipped default
        alpha 0.85   5.14:1
        alpha 0.80   4.51:1   <- the floor

Nothing here raises.  Every Win32 call is guarded and reports what happened,
because a window that cannot go glass must still open.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from typing import Dict, Optional, Tuple

IS_WINDOWS = os.name == "nt"

#: Fully opaque.  Glass "off" is not a separate palette, just this level.
GLASS_OFF = 1.0
#: Lowest level the palette can take before the dimmest text drops under
#: 4.5:1 on a white desktop.  Measured, not guessed - see the module header.
GLASS_MIN = 0.80
#: What the window ships with: noticeably see-through, 5.55:1 worst case.
GLASS_DEFAULT = 0.88

#: The window's colours.  Deliberately deeper than a flat dark UI so that the
#: translucency has somewhere to go without costing contrast.
BG = "#0d1119"
PANEL = "#131a26"
CARD = "#1a2333"
CARD_ON = "#2b3a55"
EDGE = "#3a4a68"
TEXT = "#e9eefa"
MUTED = "#a6b0c6"
ACCENT = "#78b9ff"

RGB = {
    "BG": (13, 17, 25),
    "PANEL": (19, 26, 38),
    "CARD": (26, 35, 51),
    "TEXT": (233, 238, 250),
    "MUTED": (166, 176, 198),
    "ACCENT": (120, 185, 255),
}

#: (name, foreground, background) for every pair the window actually draws.
TEXT_PAIRS: Tuple[Tuple[str, Tuple[int, int, int], str], ...] = (
    ("title on panel", RGB["TEXT"], "PANEL"),
    ("title on card", RGB["TEXT"], "CARD"),
    ("body on panel", RGB["TEXT"], "PANEL"),
    ("muted on panel", RGB["MUTED"], "PANEL"),
    ("muted on card", RGB["MUTED"], "CARD"),
    ("muted on background", RGB["MUTED"], "BG"),
    ("accent on card", RGB["ACCENT"], "CARD"),
    ("accent on background", RGB["ACCENT"], "BG"),
)


# ----------------------------------------------------------------------
# the maths, so the check script and the window agree on the floor
# ----------------------------------------------------------------------
def clamp_level(value, lo: float = GLASS_MIN, hi: float = 1.0) -> float:
    """Any input in, a sane level out.  Garbage becomes the default."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return GLASS_DEFAULT
    return max(lo, min(hi, v))


def _luminance(c) -> float:
    def ch(v):
        v /= 255.0
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(x) for x in c[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(fg, bg) -> float:
    a, b = _luminance(fg), _luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def over(colour, level: float, backdrop=(255, 255, 255)):
    """What a colour looks like at `level` against a solid backdrop."""
    return tuple((1.0 - level) * backdrop[i] + level * colour[i]
                 for i in range(3))


def pair_ratios(level: float, backdrop=(255, 255, 255)) -> Dict[str, float]:
    return {name: contrast(over(fg, level, backdrop),
                           over(RGB[bg], level, backdrop))
            for name, fg, bg in TEXT_PAIRS}


def worst_contrast(level: float, backdrop=(255, 255, 255)) -> float:
    return min(pair_ratios(level, backdrop).values())


def readable_floor(limit: float = 4.5, backdrop=(255, 255, 255)) -> float:
    """The lowest level that keeps EVERY pair at or above `limit`."""
    level = 1.0
    found = None
    while level >= 0.40:
        if worst_contrast(level, backdrop) >= limit:
            found = level
        level = round(level - 0.005, 4)
    return found if found is not None else 0.0


# ----------------------------------------------------------------------
# the Win32 half
# ----------------------------------------------------------------------
#: DWMWA_SYSTEMBACKDROP_TYPE, and the two backdrop kinds worth using.
DWMWA_SYSTEMBACKDROP_TYPE = 38
DWMSBT_TRANSIENTWINDOW = 3      # acrylic
DWMSBT_MAINWINDOW = 2            # mica
#: WCA_ACCENT_POLICY, and the undocumented accent states.
WCA_ACCENT_POLICY = 19
ACCENT_ENABLE_BLURBEHIND = 3
ACCENT_ENABLE_ACRYLIC_BLURBEHIND = 4
ACCENT_ENABLE_HOSTBACKDROP = 5

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
LWA_ALPHA = 0x00000002


class _Accent(ctypes.Structure):
    _fields_ = [("AccentState", wintypes.DWORD),
                ("AccentColor", wintypes.DWORD),
                ("GradientColor", wintypes.DWORD)]


class _WinCompatAttrData(ctypes.Structure):
    _fields_ = [("Attribute", wintypes.DWORD),
                ("Data", ctypes.POINTER(_Accent)),
                ("SizeOfData", wintypes.DWORD)]


def hwnd_of(tk_root) -> Optional[int]:
    """The real top-level HWND behind a Tk window (Tk hands back the client)."""
    if not IS_WINDOWS or tk_root is None:
        return None
    try:
        return int(ctypes.windll.user32.GetAncestor(int(tk_root.winfo_id()), 2))
    except Exception:
        return None


def apply_level(hwnd, level: float) -> bool:
    """Make the window composite at `level`.

    Tk's own -alpha does the same job; doing it here as well means the level
    is set by the module that owns the readability maths, and it still works
    if Tk silently ignores -alpha.  Both routes are harmless together.
    """
    level = clamp_level(level)
    if not IS_WINDOWS or not hwnd:
        return False
    try:
        user32 = ctypes.windll.user32
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if not style & WS_EX_LAYERED:
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED)
        ok = user32.SetLayeredWindowAttributes(
            hwnd, 0, int(round(level * 255)), LWA_ALPHA)
        return bool(ok)
    except Exception:
        return False


def read_level(hwnd) -> Optional[Dict[str, object]]:
    """Ask Windows what alpha it is ACTUALLY compositing this window at.

    This is the check that matters.  apply_level() returning True only says
    the call was accepted; GetLayeredWindowAttributes reads back the value
    Windows is really using, so a Tk that quietly ignored -alpha (or a DWM
    that reset it) shows up as a mismatch instead of passing.

    Photographing the window and measuring the pixels is the other obvious
    check and it is a trap: the screen behind it is whatever else is open,
    the acrylic tint adds its own wash, and every capture is a race with the
    compositor.  Reading the attribute back is deterministic.
    """
    if not IS_WINDOWS or not hwnd:
        return None
    try:
        # GetLayeredWindowAttributes is exported by USER32, not gdi32 -
        # asking gdi32 for it silently returns no function and this whole
        # read-back comes back None, which looks like "unsupported" rather
        # than "wrong library".
        user32 = ctypes.windll.user32
        key = wintypes.COLORREF(0)
        alpha = ctypes.c_ubyte(0)
        flags = wintypes.DWORD(0)
        ok = user32.GetLayeredWindowAttributes(
            wintypes.HWND(hwnd), ctypes.byref(key), ctypes.byref(alpha),
            ctypes.byref(flags))
        if not ok:
            return None
        return {"alpha": alpha.value, "level": round(alpha.value / 255.0, 4),
                "flags": flags.value,
                "alpha_mode": bool(flags.value & LWA_ALPHA),
                "colorkey_mode": bool(flags.value & 0x1)}
    except Exception:
        return None


def clear_level(hwnd) -> bool:
    """Drop WS_EX_LAYERED so the window stops being composited as a layer.

    Not what "glass off" uses: an opaque layered window (alpha 255) already
    reads back as fully opaque and composites correctly, which is what the
    Glass checkbox does.  This is here for tearing the layer down for good -
    a plain window with no layered compositing at all.
    """
    if not IS_WINDOWS or not hwnd:
        return False
    try:
        user32 = ctypes.windll.user32
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if style & WS_EX_LAYERED:
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style & ~WS_EX_LAYERED)
        return True
    except Exception:
        return False


def apply_backdrop(hwnd) -> Optional[str]:
    """Ask DWM to blur the desktop behind this window.

    Returns the mechanism that took, or None if none did.  Order matters:
    native acrylic first (Win11 22H2+), then the undocumented accent states
    that cover everything older.
    """
    if not IS_WINDOWS or not hwnd:
        return None
    # 1. native backdrop type
    try:
        dwm = ctypes.windll.dwmapi
        value = ctypes.c_int(DWMSBT_TRANSIENTWINDOW)
        hr = dwm.DwmSetWindowAttribute(
            wintypes.HWND(hwnd), wintypes.DWORD(DWMWA_SYSTEMBACKDROP_TYPE),
            ctypes.byref(value), ctypes.sizeof(value))
        if hr == 0:
            return "acrylic"
        value = ctypes.c_int(DWMSBT_MAINWINDOW)
        hr = dwm.DwmSetWindowAttribute(
            wintypes.HWND(hwnd), wintypes.DWORD(DWMWA_SYSTEMBACKDROP_TYPE),
            ctypes.byref(value), ctypes.sizeof(value))
        if hr == 0:
            return "mica"
    except Exception:
        pass
    # 2. the undocumented accent policy
    try:
        user32 = ctypes.windll.user32
        setter = user32.SetWindowCompositionAttribute
        for state, label in ((ACCENT_ENABLE_ACRYLIC_BLURBEHIND, "acrylic-legacy"),
                             (ACCENT_ENABLE_HOSTBACKDROP, "host-backdrop"),
                             (ACCENT_ENABLE_BLURBEHIND, "blur")):
            tint = (0x99000000 & 0xFFFFFFFF) | (0x00FFFFFF)
            accent = _Accent(state, tint, tint)
            data = _WinCompatAttrData(WCA_ACCENT_POLICY,
                                      ctypes.pointer(accent),
                                      ctypes.sizeof(accent))
            try:
                if setter(wintypes.HWND(hwnd), 1,
                          ctypes.byref(data), ctypes.sizeof(data)):
                    return label
            except Exception:
                continue
    except Exception:
        pass
    return None


def clear_backdrop(hwnd) -> bool:
    if not IS_WINDOWS or not hwnd:
        return False
    try:
        dwm = ctypes.windll.dwmapi
        value = ctypes.c_int(1)          # DWMSBT_NONE
        return dwm.DwmSetWindowAttribute(
            wintypes.HWND(hwnd), wintypes.DWORD(DWMWA_SYSTEMBACKDROP_TYPE),
            ctypes.byref(value), ctypes.sizeof(value)) == 0
    except Exception:
        return False


def support() -> Dict[str, object]:
    """What this machine can do, for the Settings tab to report honestly."""
    out = {"windows": IS_WINDOWS, "dwmapi": False,
           "composition_attribute": False, "os_build": None,
           "native_backdrop": False}
    if not IS_WINDOWS:
        return out
    try:
        ctypes.windll.dwmapi
        out["dwmapi"] = True
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetWindowCompositionAttribute
        out["composition_attribute"] = True
    except Exception:
        pass
    try:
        # sys.getwindowsversion().build is the real NT build.
        # platform.version() is "10.0.26300" here, which is major.minor.build
        # in one field - int() on the whole string fails and silently cost us
        # the Win11 22H2 detection.
        import sys
        build = int(sys.getwindowsversion().build)
        out["os_build"] = build
        out["native_backdrop"] = build >= 22621       # Win11 22H2
    except Exception:
        out["native_backdrop"] = False
    return out