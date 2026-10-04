"""
island_brain.py — what the island knows about the bot, and what the bot knows
about the island.

Four wrappers, no edits to `bot_mk8_vlm.py` and none to `M.PY`:

    estimate_user_mood(text)   ONE SHOT needle classification -> the emotion on
                               the face while the turn runs.
    needs_vision(text)         a greeting (or any simple query) never drags the
                               screen parser in; an action word always does.
    has_scene_changed/frames   the island's own window is masked out of every
                               frame the parser and the VLM see.
    build_system_prompt()      the app the user clicked on the island is named,
                               so "play a song" does not fall back to another app.

The emotion is whatever needle says.  It is a small router model: it is fast
(0.1-0.6 s) and it is sometimes wrong, which is fine — the label is taken as-is
and the island falls back to the bot's own mood when there is no label.
"""

import ctypes
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

# M.PY's emotion vocabulary, minus idle/dizzy (those are the island's own).
# 'error' is in here too: the user's own words can report a failure.
EMOTIONS = ("greeting", "happy", "loving", "thinking", "bored", "exhausted",
            "sleepy", "error")

EMOTION_HOLD = 4.0          # how long the user's emotion stays on the face
APP_FRESH = 15 * 60.0       # a clicked app stays "current" this long

# The one-shot classification tool.  A single tool with a required enum is what
# needle handles best; needle.extract() silently drops a *list* of tools.
TURN_TOOL = {
    "name": "note_user_message",
    "description": ("Read ONE message the user typed to a desktop assistant and report "
                    "the reaction it calls for: the emotion the message carries, whether "
                    "answering it needs to look at the screen, and any app it names."),
    "parameters": {
        "type": "object",
        "properties": {
            "emotion": {"type": "string", "enum": list(EMOTIONS),
                        "description": "the emotion of the user's own message"},
            "needs_screen": {"type": "boolean",
                             "description": ("true only when answering needs the screen: "
                                             "clicking, typing, opening, finding, reading "
                                             "the page, or acting on the current window")},
            "app": {"type": "string",
                    "description": "app named by the user, empty when none"},
        },
        "required": ["emotion", "needs_screen"],
    },
}
CLASSIFY_SYSTEM = ("You label one user message for a desktop assistant's HUD. "
                   "One message in, one call out.")

# an explicit action word always keeps the screen parser on, whatever the label
ACTION_WORDS = re.compile(
    r"\b(click|double[- ]?click|right[- ]?click|tap|type|typing|press|hotkey|drag|"
    r"drop|scroll|select|check|open|launch|start|play|search|find|read|read\s+this|"
    r"move|cursor|mouse|button|tab|link|window|screen|desktop|browser|bar|box|"
    r"coordinate|screenshot|page)\b", re.I)

APP_WORDS = (
    ("youtube", "YouTube"), ("spotify", "Spotify"), ("whatsapp", "WhatsApp"),
    ("snapchat", "Snapchat"), ("discord", "Discord"), ("instagram", "Instagram"),
    ("telegram", "Telegram"), ("netflix", "Netflix"), ("gmail", "Gmail"),
    ("chrome", "Chrome"), ("vscode", "VS Code"), ("vs code", "VS Code"),
    ("notepad", "Notepad"), ("terminal", "Terminal"), ("bash", "Terminal"),
    ("powershell", "Terminal"), ("browser", "the browser"),
)
APP_RE = re.compile("|".join(re.escape(k) for k, _n in APP_WORDS), re.I)
APP_NAME = {k: n for k, n in APP_WORDS}

# one short instruction per label, so the model's tone follows the user's
MOOD_KEYS = {
    "greeting": "warm and brief",
    "happy": "upbeat",
    "loving": "warm",
    "thinking": "focused and precise",
    "bored": "crisp and to the point",
    "exhausted": "steady and apologetic, and keep it short",
    "sleepy": "quiet and brief",
    "error": "calm and reassuring: say plainly what went wrong, and what you are trying instead",
}


@dataclass
class Verdict:
    """What one user message means for the island.  Empty fields mean "no idea"
    and are never guessed: an empty emotion leaves the face to the bot's mood."""
    emotion: str = ""
    needs_screen: Optional[bool] = None
    app: str = ""
    source: str = "none"
    raw: dict = field(default_factory=dict)


class TurnClassifier:
    """One shot needle classification per user message, cached by text."""

    def __init__(self, cache_size=64, max_new_tokens=96):
        self._lock = threading.RLock()
        self._cache = {}
        self._cache_size = cache_size
        self._max_new_tokens = max_new_tokens
        self._needle = None
        self._tried = False
        self.attempts = 0
        self.last_error = ""

    # -- the engine (imported lazily: needle loads a native library)
    def _needle_module(self):
        with self._lock:
            if self._needle is not None or self._tried:
                return self._needle
            self._tried = True
        try:
            import needle
        except Exception as e:                   # needle missing or unhappy
            with self._lock:
                self.last_error = f"{type(e).__name__}: {e}"
            return None
        with self._lock:
            self._needle = needle
        return needle

    def classify(self, text):
        """One shot.  needle.extract() is the API for exactly this: it builds a
        single-tool engine, asks once, and reads back both accepted and
        suppressed calls (an emotion label is never "grounded" in the text, so
        the call usually comes back suppressed)."""
        text = (text or "").strip()
        if not text:
            return Verdict()
        with self._lock:
            hit = self._cache.get(text)
        if hit is not None:
            return hit

        verdict = Verdict()
        needle = self._needle_module()
        if needle is not None:
            try:
                with self._lock:                 # the needle engine is a singleton
                    self.attempts += 1
                    args = needle.extract(text, TURN_TOOL, system=CLASSIFY_SYSTEM,
                                          max_new_tokens=self._max_new_tokens,
                                          strict=False)
                if isinstance(args, dict) and args:
                    emotion = str(args.get("emotion") or "").strip().lower()
                    verdict.emotion = emotion if emotion in EMOTIONS else ""
                    needs = args.get("needs_screen")
                    verdict.needs_screen = needs if isinstance(needs, bool) else None
                    verdict.app = str(args.get("app") or "").strip()
                    verdict.source = "needle"
                    verdict.raw = args
            except Exception as e:
                with self._lock:
                    self.last_error = f"{type(e).__name__}: {e}"
        with self._lock:
            self._cache[text] = verdict
            while len(self._cache) > self._cache_size:
                self._cache.pop(next(iter(self._cache)))
        return verdict

    def wait(self, text, timeout=2.0):
        """The verdict for `text`, classifying it now if it has not been seen."""
        text = (text or "").strip()
        if not text:
            return None
        with self._lock:
            hit = self._cache.get(text)
        if hit is not None:
            return hit
        if timeout <= 0:
            return None
        return self.classify(text)


class AppContext:
    """The app the user last picked on the island."""

    def __init__(self, island=None, fresh=APP_FRESH):
        self.island = island
        self.fresh = fresh
        self.app = ""
        self.at = 0.0
        self.source = ""

    def note_text(self, text):
        """A click on the grid, or the user naming an app in a sentence."""
        match = APP_RE.search(str(text or ""))
        if match:
            self.set(APP_NAME[match.group(0).lower()], "text")
        return self.app

    def set(self, app, source=""):
        app = (app or "").strip()
        if not app:
            return ""
        self.app, self.at, self.source = app, time.time(), source
        if self.island is not None:
            try:
                self.island.set_app(app)
            except Exception:
                pass
        return app

    def active(self):
        return self.app if self.app and time.time() - self.at <= self.fresh else ""

    def block(self, emotion=""):
        """The island's contribution to the system prompt."""
        app = self.active()
        lines = []
        if app:
            lines.append(
                f"The user picked {app} in the island's app grid and is working in it. "
                f"A request that does not name another app targets {app}: \"play a song\" "
                f"means play it in {app}, and you must NOT launch or switch to a different "
                f"app (Spotify, a browser, ...) for it. If the user names another app, "
                f"that wins.")
        if emotion:
            key = MOOD_KEYS.get(emotion, "in that key")
            lines.append(f"Their last message read as {emotion}: answer {key}.")
        lines.append("The floating capsule at the top-centre of the screen is your own HUD "
                     "(the island), not part of the desktop: never click it, never read it as "
                     "a window. The thing you need to click may sit underneath it - click the "
                     "real target's coordinates, not the capsule.")
        lines.append("You do NOT need to type everything: never type your reply, greeting or "
                     "explanation into an app as an ACTION - answer in chat instead. Only "
                     "type what the app itself needs (a search query, a field the user asked "
                     "you to fill) and keep it short.")
        return "\n\n<island_context>\n" + "\n".join(lines) + "\n</island_context>\n"


class ScreenMask:
    """Paint the island's own window out of the frames the parser sees.

    M.PY's window is the top-centre 820x280 rectangle and the visible pill sits
    centred inside it, so the rect is computed from the island's own live
    geometry (window position from the screen metrics, pill size from the
    animating width/height) and scaled into image pixels by whatever DPI
    scaling the screenshot carries.
    """

    def __init__(self, island, margin=8, mode_getter=None, grabber=None,
                 behind_ttl=1.5):
        self.island = island
        self.margin = margin
        self.mode_getter = mode_getter
        self.grabber = grabber          # callable -> full-screen PIL image
        self.behind_ttl = behind_ttl    # reuse a fresh background for this long
        self.hits = 0
        self.repairs = 0
        self.hides = 0
        self.last_rect = None
        self.last_error = ""
        self._behind = None
        self._lock = threading.RLock()

    @staticmethod
    def _logical_width():
        try:
            return int(ctypes.windll.user32.GetSystemMetrics(0))
        except Exception:
            return 0

    def rect(self, size):
        island = self.island
        if island is None or size is None:
            return None
        if self.mode_getter is not None:
            try:
                if self.mode_getter() != "screen":
                    return None                  # a webcam frame has no island in it
            except Exception:
                pass
        try:
            win_w = float(island.screen_width)
            win_h = float(island.screen_height)
            pos_x = float(island.window_pos_x)
            pos_y = float(island.window_pos_y)
            cur_w = float(island.current_width)
            cur_h = float(island.current_height)
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            return None
        if win_w <= 0 or cur_w <= 0 or cur_h <= 0:
            return None
        img_w, img_h = float(size[0]), float(size[1])
        logical = self._logical_width() or win_w
        scale = img_w / logical if logical else 1.0
        left = (pos_x + (win_w - cur_w) / 2.0 - self.margin) * scale
        top = (pos_y - self.margin) * scale
        right = (pos_x + (win_w + cur_w) / 2.0 + self.margin) * scale
        bottom = (pos_y + cur_h + self.margin) * scale
        l = max(0, int(round(left)))
        t = max(0, int(round(top)))
        r = min(int(img_w), int(round(right)))
        b = min(int(img_h), int(round(bottom)))
        if r - l < 4 or b - t < 4:
            return None
        return (l, t, r, b)

    def _grab(self, size):
        """A whole-desktop capture, scaled into the frame's own pixel space."""
        try:
            if self.grabber is not None:
                shot = self.grabber()
            else:
                from PIL import ImageGrab
                shot = ImageGrab.grab()
            if shot is None:
                return None
            if tuple(shot.size) != tuple(size):
                shot = shot.resize((int(size[0]), int(size[1])))
            return shot
        except Exception as e:
            self.last_error = f"grab: {type(e).__name__}: {e}"
            return None

    def repair(self, image):
        """Replace the island's rectangle with what is actually behind it.

        A black box would hide the search icon the island is sitting on, so the
        island steps off screen for one capture and the real background is
        pasted in.  A just-captured background is reused for `behind_ttl`
        seconds, so a turn that parses three times only causes one blink.
        """
        if image is None:
            return image
        rect = self.rect(getattr(image, "size", None))
        if rect is None:
            return image

        with self._lock:
            cached = self._behind
        if cached is not None and cached[0] == rect and \
                time.time() - cached[2] < self.behind_ttl:
            try:
                image.paste(cached[1], (rect[0], rect[1]))
                self.hits += 1
                return image
            except Exception as e:
                self.last_error = f"paste: {type(e).__name__}: {e}"

        island, hide, show = self.island, None, None
        if island is not None:
            hide = getattr(island, "hide_for_scan", None)
            show = getattr(island, "show_after_scan", None)
        if not callable(hide):
            hide = show = None
        try:
            if hide is not None:
                hide()
                self.hides += 1
            shot = self._grab(image.size)
        finally:
            if show is not None:
                show()

        if shot is None:                 # could not see behind: hide ourselves instead
            return self.apply(image)
        try:
            patch = shot.crop(rect)
            image.paste(patch, (rect[0], rect[1]))
            with self._lock:
                self._behind = (rect, patch, time.time())
            self.repairs += 1
            self.hits += 1
            self.last_rect = rect
        except Exception as e:
            self.last_error = f"paste: {type(e).__name__}: {e}"
        return image

    def apply(self, image):
        """Black out the island region in place.  Returns the same object."""
        if image is None:
            return image
        rect = self.rect(getattr(image, "size", None))
        if rect is None:
            return image
        try:
            from PIL import ImageDraw
            ImageDraw.Draw(image).rectangle(rect, fill=(0, 0, 0))
            self.hits += 1
            self.last_rect = rect
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
        return image


class Brain:
    """Installs the island's four ideas into the running bot."""

    def __init__(self, island=None, feed=None, classifier=None, apps=None,
                 mask=None, bot=None):
        self.island = island
        self.feed = feed
        self.classifier = classifier or TurnClassifier()
        self.apps = apps or AppContext(island=island)
        self.mask = mask or ScreenMask(island, mode_getter=self._vision_mode)
        self.bot = bot
        self.last_verdict = Verdict()
        self.installed = []
        self.last_error = ""

    # -- mode guard for the mask ------------------------------------------
    def _vision_mode(self):
        mode = getattr(self.bot, "VISION_MODE", "screen") if self.bot else "screen"
        return mode

    # -- install ----------------------------------------------------------
    def install(self, bot):
        self.bot = bot
        for wrapper in (self._wrap_reaction, self._wrap_needs_vision,
                        self._wrap_prompt, self._wrap_frames):
            try:
                name = wrapper(bot)
                if name:
                    self.installed.append(name)
            except Exception as e:
                self.last_error = f"{wrapper.__name__}: {type(e).__name__}: {e}"
        return self

    # 1. the user's words -> the emotion on the face (and the app they named)
    def _wrap_reaction(self, bot):
        original = getattr(bot, "estimate_user_mood", None)
        if original is None:
            return ""
        brain = self

        def wrapped(text):
            verdict = brain.classifier.classify(text)
            brain.last_verdict = verdict
            brain.show_reaction(verdict)
            return original(text)
        bot.estimate_user_mood = wrapped
        return "estimate_user_mood"

    def show_reaction(self, verdict):
        """Whatever needle said, exactly as it said it (nothing is guessed)."""
        if verdict.emotion and self.island is not None:
            try:
                self.island.set_emotion(verdict.emotion)
                if self.feed is not None:
                    self.feed.hold_emotion(EMOTION_HOLD)
            except Exception as e:
                self.last_error = f"set_emotion: {type(e).__name__}: {e}"
        if verdict.app:
            self.apps.set(verdict.app, "message")

    # 2. a simple query never parses the screen
    def _wrap_needs_vision(self, bot):
        original = getattr(bot, "needs_vision", None)
        if original is None:
            return ""
        brain = self

        def wrapped(text, is_proactive=False):
            if is_proactive or not text:
                return True
            verdict = brain.classifier.wait(text, timeout=2.0)
            if verdict is not None and verdict.needs_screen is False \
                    and not ACTION_WORDS.search(str(text)):
                return False
            return original(text, is_proactive)
        bot.needs_vision = wrapped
        return "needs_vision"

    # 3. the island's pick of app goes into the prompt
    def _wrap_prompt(self, bot):
        original = getattr(bot, "build_system_prompt", None)
        if original is None:
            return ""
        brain = self

        def wrapped(*a, **kw):
            base = original(*a, **kw)
            extra = brain.apps.block(brain.last_verdict.emotion)
            return (base + extra) if extra else base
        bot.build_system_prompt = wrapped
        return "build_system_prompt"

    # 4. the island is not part of the screen
    def _wrap_frames(self, bot):
        names = []
        for name in ("has_scene_changed", "_thumb", "check_ocr_box",
                     "get_som_labeled_img"):
            original = getattr(bot, name, None)
            if original is None or not callable(original):
                continue
            # scene detection only needs "not the island" -> a black hole keeps
            # the animating HUD from firing interrupts; every consumer that
            # reaches the model gets the real background behind the island.
            mode = "black" if name == "has_scene_changed" else "repair"
            setattr(bot, name, self._masking(name, original, mode))
            names.append(name)

        ctm = getattr(bot, "ctm", None)
        if ctm is not None and hasattr(ctm, "check_transition"):
            setattr(ctm, "check_transition", self._ctm_check(ctm.check_transition))
            names.append("ctm.check_transition")
        return ",".join(names)

    def _masking(self, name, original, mode="repair"):
        brain = self

        def wrapped(image=None, *a, **kw):
            try:
                if mode == "black":
                    brain.mask.apply(image)      # in place: the stored frame is clean
                else:
                    brain.mask.repair(image)     # in place: the real background is back
            except Exception as e:
                brain.last_error = f"mask({name}): {type(e).__name__}: {e}"
            return original(image, *a, **kw)
        wrapped.__name__ = name
        return wrapped

    def _ctm_check(self, original):
        """The island taking focus (or animating) is not a window change."""

        def wrapped(current_window="", scene_changed=False, mode="screen"):
            if isinstance(current_window, str) and "dynamic island" in current_window.lower():
                current_window = ""
            return original(current_window, scene_changed, mode)
        wrapped.__name__ = "check_transition"
        return wrapped
