"""
coord_math.py -- pure click-coordinate parsing/normalization for PHOENIX MK4.

The MK4 brain (Qwen3-VL-4B) natively grounds clicks on the screenshot and can
emit coordinates as:

  - fractions of the image:        click at 0.512 0.334   (PREFERRED, unambiguous)
  - legacy integer screen pixels:  click at 983 361       (internal / driver format)

Ambiguity policy (documented + unit-tested): a "click at A B" command is read
as FRACTIONS when either number carries a decimal point (or is <= 1), and as
PIXELS otherwise. Pixel-priority for bare integers preserves every internal
command byte-for-byte: the deterministic drivers and the stall guard emit
integer-pixel "click at X Y" commands and MUST keep their meaning. The MK4
system prompt instructs the model to always include the decimal point, so
model output lands in the unambiguous fraction branch.

Nothing in this module touches the GUI -- it is pure math, unit-testable.
"""

import re

_CLICK_AT_RE = re.compile(
    r"^click\s+at\s+\(?([0-9]+(?:\.[0-9]+)?)[\s,]+([0-9]+(?:\.[0-9]+)?)\)?\s*$",
    re.IGNORECASE,
)


def parse_click_at(text):
    """Parse a 'click at A B' command.

    Returns ("frac", fx, fy) for fractional coordinates (clamped to 0..1),
    ("px", x, y) for integer screen pixels, or None when the text is not a
    click-at command at all.
    """
    if not text:
        return None
    m = _CLICK_AT_RE.match(text.strip().lower())
    if not m:
        return None
    raw_a, raw_b = m.group(1), m.group(2)
    a, b = float(raw_a), float(raw_b)
    has_dot = ("." in raw_a) or ("." in raw_b)
    if has_dot or a <= 1.0 or b <= 1.0:
        return ("frac", min(max(a, 0.0), 1.0), min(max(b, 0.0), 1.0))
    return ("px", a, b)


def frac_to_px(fx, fy, screen_w, screen_h):
    """Convert image fractions to clamped integer screen pixels."""
    x = int(round(min(max(fx, 0.0), 1.0) * screen_w))
    y = int(round(min(max(fy, 0.0), 1.0) * screen_h))
    x = min(max(x, 0), screen_w - 1)
    y = min(max(y, 0), screen_h - 1)
    return x, y


def click_target_px_from_command(text, screen_w, screen_h):
    """'click at ...' -> (x, y) screen pixels, or None if not a click-at.

    Accepts fractional and integer-pixel variants so the stall guard and the
    orchestrator can reason about the SAME pixel target regardless of which
    coordinate convention produced the command.
    """
    parsed = parse_click_at(text)
    if parsed is None:
        return None
    kind, a, b = parsed
    if kind == "frac":
        return frac_to_px(a, b, screen_w, screen_h)
    return int(a), int(b)


def canonicalize_click_at(text, screen_w, screen_h):
    """Normalize ANY 'click at ...' variant to the legacy integer-pixel form.

    MK4 design: canonicalization is the ONLY place fractional coordinates are
    translated. After this function runs, every downstream consumer
    (_classify_command, _click_target_px, the orchestrator gate, the stall
    guard, nidle's hands) sees the exact command shape it has always seen,
    e.g. "click at 983 361". Returns None when `text` is not a click-at.
    """
    if not parse_click_at(text):
        return None
    x, y = click_target_px_from_command(text, screen_w, screen_h)
    return f"click at {x} {y}"
