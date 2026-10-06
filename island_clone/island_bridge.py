"""
island_bridge.py — the realtime bot <-> island bridge.

The island runs at 60 fps on one thread; the bot runs its turns on worker
threads.  They talk through `IslandState`, a plain lock-guarded object - no
signals, no Qt, no cross-thread widget access (which is exactly what bit the
earlier Tk and Qt versions).

Four hooks, all wrappers, none of them edits:

    model.chat_stream(...)   every token as it is produced -> the live bubble
    log_event(component, ..) what the bot is actually doing -> action chips
    speak(text)              the sentence it is saying     -> speaking face
    status_overlay.set_state waiting / working             -> listening pill

`bot_mk8_vlm.py` is untouched.
"""

import re
import threading
import time

# log lines that are pure plumbing and would only be noise on screen
QUIET = re.compile(r"^(Generation \d+ started|Speaking: |TTS |PRUNE)", re.I)
# lines that mean "something went wrong"
FAILURE = re.compile(r"failed|error|blocked|rejected|timeout|refused|gave up|"
                     r"drifted|malformed|traceback", re.I)
# lines that mean "the bot is doing a thing with its hands"
DOING = re.compile(r"click|typing|type |launch|open |search|scroll|applied|"
                   r"executed|dispatch|driver|intent|payload|contact|expected text|"
                   r"verification|target_app|screen|dom|coordinate|cursor|double|"
                   r"activate|needle|claude|edit|patch|test", re.I)
NOISE_PREFIX = ("[APPD] ", "[NEEDLE] ", "[REGEX-FALLBACK] ", "[AUTO-LAUNCH] ",
                "[MESSAGING] ", "[CAPABILITY-GATE] ", "[SILENCE] ")

MOODS = ("idle", "listening", "thinking", "acting", "speaking", "happy", "alert")


class IslandState:
    """Everything the renderer needs, safe to touch from any thread."""

    def __init__(self):
        self._lock = threading.RLock()
        self.mood = "idle"
        self.headline = "Waking up"
        self.sub = ""
        self.busy = False
        self.turn = 0                      # increments on every turn
        self.transcript = []                # [{"role", "text"}]
        self.stream = ""                    # the bubble currently filling
        self.thinking = False
        self.tokens = 0
        self.actions = []                   # [{"component","message","state"}]
        self.action_count = 0               # actions reported this turn (the island's step)
        self.speech = ""                    # the sentence being spoken now
        self.asked = None                   # the query the user submitted
        self.last_error = ""

    # -- mutators (any thread) ------------------------------------------
    def set(self, **kw):
        with self._lock:
            for k, v in kw.items():
                setattr(self, k, v)

    def set_mood(self, mood, headline=None, sub=None):
        """Mood changes are damped: log lines arrive in bursts and the face
        should not strobe between expressions."""
        with self._lock:
            if mood not in MOODS:
                mood = "idle"
            now = time.time()
            if mood == self.mood and now - self._last_mood_at < 0.35:
                if headline is not None:
                    self.headline = headline
                if sub is not None:
                    self.sub = sub
                return
            self._last_mood_at = now
            self.mood = mood
            if headline is not None:
                self.headline = headline
            if sub is not None:
                self.sub = sub

    def user_said(self, text):
        with self._lock:
            self.asked = text
            self.transcript.append({"role": "user", "text": text})
            self.turn += 1
            self.stream = ""
            self.tokens = 0
            self.actions = []
            self.action_count = 0

    def begin_stream(self):
        with self._lock:
            self.thinking = True
            self.stream = ""

    def token(self, chunk):
        with self._lock:
            self.stream += chunk
            self.tokens += 1

    def end_stream(self, final=""):
        with self._lock:
            self.thinking = False
            text = final.strip() or self.stream.strip()
            if text:
                self.transcript.append({"role": "bot", "text": text})
            self.stream = ""
            # the island shows prose only: unwrap whatever the model wrapped
            # its text in, and keep an answer at its full length
            self.transcript[-1]["text"] = _clean_action(text)

    def action(self, component, message):
        with self._lock:
            comp = str(component).upper()
            text = _tidy(message)
            self.action_count += 1       # every line the island shows counts
            for a in reversed(self.actions):
                if a["component"] == comp and a["state"] == "run":
                    a["message"] = text          # coalesce a burst from one place
                    return a
            a = {"component": comp, "message": text, "state": "run"}
            self.actions.append(a)
            return a

    def resolve_actions(self, ok=True, note=""):
        with self._lock:
            for a in self.actions:
                a["state"] = "done" if ok else "fail"
                if note:
                    a["note"] = note

    def speaking(self, text):
        with self._lock:
            self.speech = text

    # -- snapshot for the renderer (called once per frame) --------------
    def snapshot(self):
        with self._lock:
            return {
                "mood": self.mood, "headline": self.headline, "sub": self.sub,
                "busy": self.busy, "turn": self.turn, "tokens": self.tokens,
                "action_count": self.action_count,
                "transcript": list(self.transcript), "stream": self.stream,
                "thinking": self.thinking,
                "actions": [dict(a) for a in self.actions[-4:]],
                "speech": self.speech, "asked": self.asked,
            }

    _last_mood_at = 0.0


def _tidy(message, limit=58):
    msg = " ".join(str(message).split())
    for prefix in NOISE_PREFIX:
        if msg.startswith(prefix):
            msg = msg[len(prefix):]
    return msg if len(msg) <= limit else msg[:limit - 1] + "\u2026"


def _clean_action(text):
    """Turn '<ACTION>click(...)</ACTION>' into 'click(...)'.

    Anything else keeps its full length: an answer the model wrapped in
    '<thought>...</thought>' is unwrapped, never cut short, so the island's
    chat panel really can show all of it.
    """
    m = re.search(r"<\s*ACTION\s*>(.*?)<\s*/\s*ACTION\s*>", text, re.S | re.I)
    if m:
        inner = " ".join(m.group(1).split())
        return inner if len(inner) <= 120 else inner[:119] + "\u2026"
    out = re.sub(r"<\s*/?\s*[A-Za-z_][\w.-]*\s*>", " ", str(text))
    out = re.sub(r"^PHOENIX:\s*", "", " ".join(out.split()), flags=re.I).strip()
    return out if len(out) <= 2000 else out[:1999] + "\u2026"


class BotBridge:
    """Installs the hooks.  Everything is defensive: a broken hook must never
    take the bot down, only the island."""

    def __init__(self, state, on_turn=None):
        self.state = state
        self.on_turn = on_turn          # callable(text) -> runs the bot turn
        self.bot = None
        self._thread = None
        self._lock = threading.Lock()

    # -- install ---------------------------------------------------------
    def install(self, bot):
        self.bot = bot
        self.hook_chat_stream(bot)
        self.hook_log(bot)
        self.hook_speak(bot)
        self.hook_overlay(bot)
        return self

    def hook_chat_stream(self, bot):
        """Every generated token, exactly as the bot receives it."""
        original = bot.model.chat_stream

        def wrapped(messages, grammar=None, **kw):
            self.state.set_mood("thinking", "Thinking",
                                 "action grammar" if grammar else
                                 f"{len(messages)} messages")
            self.state.begin_stream()
            try:
                for chunk in original(messages, grammar=grammar, **kw):
                    try:
                        delta = chunk.get("choices", [{}])[0].get("delta", {})
                        if "content" in delta and delta["content"]:
                            self.state.token(delta["content"])
                    except Exception:
                        pass
                    yield chunk
            finally:
                self.state.end_stream("")
        bot.model.chat_stream = wrapped
        return original

    def hook_log(self, bot):
        """The bot's own narration -> action chips, and the mood that follows."""
        original = bot.log_event

        def wrapped(component, message):
            try:
                comp = str(component).upper()
                msg = str(message)
                if not QUIET.match(msg):
                    failed = bool(FAILURE.search(msg))
                    self.state.action(comp, msg)
                    if failed:
                        self.state.set_mood("alert", _tidy(msg), comp)
                        self.state.set(last_error=msg)
                    elif DOING.search(msg):
                        self.state.set_mood("acting", _tidy(msg), comp)
                    else:
                        self.state.set_mood("working" if self.state.mood
                                            in ("thinking", "acting") else
                                            self.state.mood, _tidy(msg), comp)
            except Exception:
                pass
            return original(component, message)

        bot.log_event = wrapped
        return original

    def hook_speak(self, bot):
        """The bot talking -> speaking face, waveform and a transcript line."""
        original = bot.speak

        def wrapped(text):
            try:
                clean = re.sub(r"^PHOENIX:\s*", "", str(text), flags=re.I).strip()
                if clean:
                    self.state.speaking(clean)
                    self.state.set_mood("speaking", "Speaking", clean[:58])
            except Exception:
                pass
            return original(text)
        bot.speak = wrapped
        return original

    def hook_overlay(self, bot):
        """bot.status_overlay -> the island.  The production dot never opens."""
        state = self.state

        class IslandOverlay:
            @staticmethod
            def set_state(mode):
                if mode == "working":
                    state.set_mood("working", "Working...", "")
                elif not state.busy:
                    state.set_mood("listening", "Listening", "")

            @staticmethod
            def start():
                pass

        bot.status_overlay = IslandOverlay()
        return bot.status_overlay

    # -- turns -----------------------------------------------------------
    def submit(self, text):
        """Run one full bot turn on a worker thread."""
        text = (text or "").strip()
        if not text or self.bot is None:
            return False
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            self.state.user_said(text)
            self.state.set(busy=True)
            self._thread = threading.Thread(target=self._run_turn, args=(text,),
                                            daemon=True)
            self._thread.start()
            return True

    def _run_turn(self, text):
        ok, note = True, ""
        try:
            if self.on_turn:
                self.on_turn(text)
            else:
                self.bot.submit_text(text, is_proactive=False)
        except Exception as e:
            ok, note = False, f"{type(e).__name__}: {e}"
            self.state.set(last_error=note)
        finally:
            self.state.resolve_actions(ok, note)
            self.state.set(busy=False)
            if ok:
                self.state.set_mood("happy", "Done", "task complete")
            else:
                self.state.set_mood("alert", "Something broke", note[:58])

    def wait(self, timeout=None):
        """Block until the turn in flight finishes (smoke tests, shutdown)."""
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return not self.busy

    def stop(self):
        """Bump the generation id: the bot's stream loop aborts on next chunk."""
        try:
            self.bot.generation_id += 1
            self.bot.stop_tts()
            self.state.resolve_actions(False, "stopped")
            self.state.set_mood("alert", "Stopped", "cancelled")
            return True
        except Exception:
            return False

    @property
    def busy(self):
        with self._lock:
            return bool(self._thread and self._thread.is_alive())