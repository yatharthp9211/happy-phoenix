"""
lightweight streaming tag filter + silence guard (no heavy imports).
Kept out of bot_mk3.py so it can be unit-tested without loading the LLM.
"""

import re


class StreamThoughtFilter:
    """Streaming filter that suppresses <thought>/ thinking/<ACTION>/<runtime>
    blocks - including truncated fragments like '<th' or '< ACTION>' - so
    they are never spoken. Returns the newly speakable slice per chunk."""

    TAG_NAMES = ("thought", "think", "thinking", "action", "runtime", "conversation")

    @staticmethod
    def _norm(name: str) -> str:
        return "think" if name == "thinking" else name

    def __init__(self):
        self.raw = ""
        self.buf = ""
        self.state = "OUT"     # OUT | START (inside a possible '<...')
        self.suppress = False  # inside the body of a recognized tag
        self.open_tag = None   # which tag's body we are suppressing
        self.hold = ""
        self.tail = ""         # small window of most recent body chars

    def process_chunk(self, chunk: str) -> str:
        for ch in chunk:
            self.raw += ch
            if self.state == "START":
                self.hold += ch
                inner = self.hold[1:]
                if ch == ">":
                    head = inner[:-1].strip().lower()
                    base = head.lstrip("/")
                    if base in self.TAG_NAMES:
                        if base == "conversation":
                            # section header, not a wrapper: drop the tag, keep going
                            self.hold = ""
                            self.state = "OUT"
                            continue
                        if head.startswith("/"):
                            # stray close with no open: keep word separation
                            if not self.suppress:
                                self.buf += " "
                            self.suppress = False
                            self.open_tag = None
                            self.tail = ""
                        else:
                            self.suppress = True
                            self.open_tag = self._norm(base)
                            self.tail = ""
                        self.hold = ""
                        self.state = "OUT"
                    else:
                        self._emit_or_drop(self.hold)
                        self.hold = ""
                        self.state = "OUT"
                    continue
                if ch.isspace():
                    so_far = inner.strip().lower()
                    if so_far == "" or any(n.startswith(so_far) for n in self.TAG_NAMES):
                        continue
                    if so_far in self.TAG_NAMES:
                        if so_far == "conversation":
                            # '< CONVERSATION \n' header: drop it, keep going
                            self.hold = ""
                            self.state = "OUT"
                            continue
                        # '<thought \n' or '< ACTION ' -> tag decided
                        self.suppress = True
                        self.open_tag = self._norm(so_far)
                        self.hold = ""
                        self.state = "OUT"
                    else:
                        self._emit_or_drop(self.hold)
                        self.hold = ""
                        self.state = "OUT"
                    continue
                so_far = inner.strip().lower().lstrip("/")
                if so_far == "" or any(n.startswith(so_far) for n in self.TAG_NAMES):
                    continue  # keep holding
                self._emit_or_drop(self.hold)
                self.hold = ""
                self.state = "OUT"
                continue
            # OUT
            if ch == "<":
                self.hold = "<"
                self.state = "START"
                continue
            if self.suppress:
                # Some Qwen outputs close a <thought> with ' response' instead
                # of '</thought>'. Only honoured inside thought/think bodies.
                if self.open_tag in ("thought", "think"):
                    self.tail += ch
                    if len(self.tail) > 12:
                        self.tail = self.tail[-12:]
                    if re.search(r"(^|\s)response\b", self.tail, re.I):
                        self.suppress = False
                        self.open_tag = None
                        self.tail = ""
                continue
            self.buf += ch
        return self._take()

    def _emit_or_drop(self, text: str):
        if not self.suppress:
            self.buf += text

    def _take(self) -> str:
        out, self.buf = self.buf, ""
        return out


def is_silence(text) -> bool:
    t = (text or "").strip()
    u = t.upper()
    return u == "SILENCE" or u == "[SILENCE]" or u.startswith("[SILENCE]")


def strip_speech_tags(text):
    """Remove model markup before text is spoken or logged."""
    t = re.sub(r"<\s*ACTION\b[\s\S]*?(?:<\s*/\s*ACTION\s*>|$)", "", text, flags=re.IGNORECASE)
    t = re.sub(r"<\s*(?:thought|think)\b[\s\S]*?(?:<\s*/\s*(?:thought|think)\s*>|$)", "", t, flags=re.IGNORECASE)
    t = re.sub(r"<\s*runtime\b[\s\S]*?(?:<\s*/\s*runtime\s*>|$)", "", t, flags=re.IGNORECASE)
    t = re.sub(r"(?:^|\n|<|\[)\s*ACTION\s*(?:>|\]|:)\s*[^<\n]+", "", t, flags=re.IGNORECASE)
    t = re.sub(r"(?:^|\n)\s*ACTION[:\s]+[^\n]+", "", t, flags=re.IGNORECASE)
    t = re.sub(r"(?im)^\s*(?:thought|action)[:]\s*[^\n]*", "", t)
    t = re.sub(r"(?i)<\s*/?\s*conversation\s*>", "", t)
    t = re.sub(r"[ \t]+", " ", t)
    return re.sub(r"^(?:PHOENIX:\s*)?", "", t.strip(), flags=re.IGNORECASE).strip()