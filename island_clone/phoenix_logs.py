"""
phoenix_logs.py - the server, bot and island logs, as files you can read.

    from phoenix_logs import SOURCES, LogTail, install_tee

The floating island is a HUD.  It should never be where you read a log: there
is nowhere to scroll, nowhere to select, and it covers the screen you are
trying to debug.  So all three logs are surfaced in the main app window's
Logs tab instead, and this module is the part that does not care about Tk -
which means it can be tested on its own.

Three sources, all plain files on disk:

    Bot      ~/.phoenix/bot.log        the launcher's and the bot's own
                                      stdout/stderr, teed by phoenix_live
    Server   <root>/llama_server.log   llama.cpp: model load, slots, timing
    Island   <root>/phoenix_island.log M.PY's own debug trace

LogTail follows a file the way `tail -f` does, but bounded: it keeps at most
MAX_LINES lines in memory and re-reads from a saved offset, so a log that
grows to hundreds of megabytes over a week cannot take the window down with
it.  It also survives the two things that actually happen to log files:
being truncated ("clear the log") and being rotated out from under it.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

#: Keep this many lines.  At ~100 bytes a line that is ~80 KB of text, which
#: is a lot to scroll but nothing to hold in memory.
MAX_LINES = 4000

#: Never read more than this many bytes on one poll, so a log that just
#: gained 400 MB cannot freeze the window for a minute.
MAX_READ = 1 << 20

BOT_LOG = os.path.join(os.path.expanduser("~"), ".phoenix", "bot.log")
SERVER_LOG = os.path.join(ROOT, "llama_server.log")
ISLAND_LOG = os.path.join(ROOT, "phoenix_island.log")

#: key -> (label, path, blurb).  Ordered the way you want to read them.
SOURCES: Tuple[Tuple[str, str, str, str], ...] = (
    ("bot", "Bot", BOT_LOG,
     "what PHOENIX says and does - teed from stdout/stderr"),
    ("server", "Server", SERVER_LOG,
     "llama.cpp: model load, slots, tokens/sec"),
    ("island", "Island", ISLAND_LOG,
     "the Dynamic Island's own debug trace"),
)

#: llama.cpp writes "  0.04.948.628 I srv   message" and friends.  The
#: timestamp is its own group - matching the whole prefix and then slicing
#: line[:m.start()] returns "" because the match starts at column 0.
_LLAMA_RE = re.compile(r"^\s*([\d.:]+)\s+([EWID])\s")
_BRACKET_RE = re.compile(r"^\[([^\]]+)\]")


def level_of(line: str) -> str:
    """Best-effort severity for a line.  Unlabelled text is INFO."""
    m = _LLAMA_RE.match(line or "")
    if m:
        return m.group(2)
    low = (line or "").lower()
    for word, lvl in (("error", "E"), ("traceback", "E"), ("fatal", "E"),
                      ("exception", "E"), ("warn", "W"), ("warning", "W"),
                      ("debug", "D")):
        if word in low[:160]:
            return lvl
    return "I"


def timestamp_of(line: str) -> str:
    """Pull the timestamp off the front, if the line has one."""
    m = _LLAMA_RE.match(line or "")
    if m:
        return m.group(1)
    m = _BRACKET_RE.match(line or "")
    if m:
        return m.group(1)
    return ""


def source_paths() -> Dict[str, str]:
    return {k: p for k, _label, p, _blurb in SOURCES}


class LogTail:
    """Follow one log file, bounded, and survive it being cleared.

        t = LogTail(SERVER_LOG)
        t.poll()
        for line in t.lines: ...

    `lines` is a list of (text, level) and `path` may be None - the file may
    not exist yet, which is the normal state before the bot has booted.
    """

    def __init__(self, path: str, max_lines: int = MAX_LINES,
                 encoding: str = "utf-8"):
        self.path = path
        self.max_lines = max(max_lines, 1)
        self.encoding = encoding
        self.offset = 0
        self.lines: List[Tuple[str, str]] = []
        self.truncated = False            # the file is larger than we hold
        self.missing = not os.path.isfile(path)
        self.error: Optional[str] = None
        self.reads = 0
        self._partial = b""               # a half-written line across polls

    # -- the reader ----------------------------------------------------
    def poll(self) -> int:
        """Read whatever is new.  Returns the number of lines added."""
        self.error = None
        if os.path.isdir(self.path):
            # A directory where a log should be.  getsize() answers 0 for
            # one on Windows, which would otherwise look like "an empty file
            # that has never grown" - silently nothing, forever.
            self.error = f"{self.path} is a directory, not a log file"
            return 0
        try:
            size = os.path.getsize(self.path)
        except FileNotFoundError:
            # not created yet - the usual case before the bot boots
            if not self.missing:
                self.missing = True
                self.lines = []
                self.offset = 0
            return 0
        except OSError as exc:
            self.error = str(exc)
            return 0
        self.missing = False

        # Truncated or rotated: start over rather than read from a stale
        # offset that no longer means anything.
        if size < self.offset:
            self.offset = 0
            self.lines = []
            self._partial = b""
            self.truncated = False

        if size == self.offset:
            return 0

        # A file bigger than MAX_READ since last time: jump to the tail and
        # say so, rather than reading a gigabyte into a Tk text widget.
        if size - self.offset > MAX_READ:
            self.offset = size - MAX_READ
            self.truncated = True

        try:
            with open(self.path, "rb") as fh:
                fh.seek(self.offset)
                chunk = fh.read(MAX_READ)
                self.offset = fh.tell()
        except OSError as exc:
            self.error = str(exc)
            return 0
        if not chunk:
            return 0
        self.reads += 1

        data = self._partial + chunk
        # a file being written to will often end mid-line; hold the remainder
        # back until the rest of it arrives, or the last line flickers
        tail_nl = data.rfind(b"\n")
        if tail_nl < 0:
            self._partial = data
            return 0
        self._partial = data[tail_nl + 1:]
        text = data[:tail_nl].decode(self.encoding, "replace")
        added = []
        for raw in text.split("\n"):
            raw = raw.rstrip("\r")
            if raw.strip():
                added.append((raw, level_of(raw)))
        self.lines.extend(added)
        if len(self.lines) > self.max_lines:
            self.lines = self.lines[-self.max_lines:]
            self.truncated = True
        return len(added)

    # -- convenience ---------------------------------------------------
    def clear(self):
        """Forget what we have read (does NOT touch the file)."""
        self.lines = []
        self.offset = 0
        self._partial = b""
        self.truncated = False

    def reopen(self):
        """Re-read from the start of whatever is on disk now."""
        self.clear()
        self.missing = not os.path.isfile(self.path)
        self.poll()

    def size(self) -> int:
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0

    def matches(self, needle: str) -> List[Tuple[str, str]]:
        """Filtered view.  An empty needle means everything."""
        if not needle:
            return list(self.lines)
        low = needle.lower()
        return [ln for ln in self.lines if low in ln[0].lower()]


# ----------------------------------------------------------------------
# the tee that gives the bot a log file at all
# ----------------------------------------------------------------------
class Tee:
    """Write to a file AND to the console, from one print().

    The bot is never edited - it prints to stdout and that is all.  Swapping
    sys.stdout for this at the launcher captures both the launcher's own
    lines and the bot's, in one file, without touching either.

    The file is UTF-8 and never raises.  The console copy is best effort: on
    this machine it is cp1252 and cannot print an emoji, so a stray one must
    not take the launcher down mid-turn - it gets escaped instead.
    """

    def __init__(self, path: str, stream=None, max_bytes: int = 4 << 20):
        self.path = path
        self.stream = stream
        self.max_bytes = max_bytes
        self.lines = 0
        self.dropped = 0
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            self._fh = open(path, "a", encoding="utf-8", errors="replace")
        except OSError as exc:
            log = f"[logs] cannot write {path}: {exc}\n"
            sys.stderr.write(log)
            self._fh = None

    def write(self, data: str) -> int:
        if self._fh is not None:
            try:
                # keep the file from growing without bound across restarts
                if self._fh.tell() > self.max_bytes:
                    self._fh.close()
                    self._fh = open(self.path, "w", encoding="utf-8",
                                    errors="replace")
                    self.dropped += 1
                self._fh.write(data)
            except (OSError, ValueError):
                self._fh = None
        if self.stream is not None:
            try:
                self.stream.write(data)
            except Exception:
                # cp1252 consoles raise UnicodeEncodeError on emoji
                try:
                    enc = getattr(self.stream, "encoding", None) or "ascii"
                    self.stream.write(data.encode(enc, "backslashreplace")
                                      .decode(enc, "replace"))
                except Exception:
                    pass
        return len(data)

    def flush(self):
        if self._fh is not None:
            try:
                self._fh.flush()
            except (OSError, ValueError):
                pass
        if self.stream is not None:
            try:
                self.stream.flush()
            except Exception:
                pass

    def isatty(self) -> bool:
        return bool(self.stream is not None and getattr(self.stream, "isatty", lambda: False)())

    def fileno(self):
        # deliberately raises: anything that wanted the real fd must not get
        # it, or writes would bypass the file entirely
        raise OSError("tee has no file descriptor")

    @property
    def encoding(self) -> str:
        return getattr(self.stream, "encoding", None) or "utf-8"

    def close(self):
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None


def install_tee(path: str = BOT_LOG, also_stderr: bool = True):
    """Point stdout (and stderr) at a Tee.  Returns (stdout_tee, stderr_tee)."""
    out = Tee(path, sys.stdout)
    sys.stdout = out
    err = None
    if also_stderr:
        err = Tee(path, sys.stderr)
        sys.stderr = err
    return out, err