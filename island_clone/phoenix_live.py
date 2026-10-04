"""
phoenix_live.py — the ModernGL Dynamic Island (M.PY) driven by the real bot.

    python island_clone\\phoenix_live.py        (or double-click ..\\run_island.bat)

M.PY stays exactly as it is: it is the front end.  This file is the glue, and it
does not edit bot_mk8_vlm.py either.  Data flows both ways:

    bot_mk8_vlm  --(4 hooks)-->  IslandState  --(poller, ~8 Hz)-->  island.set_state()
                                    set_emotion() / set_thought() / set_action()
                                    set_speech()
    island  --(on_user_input / DROPFILE / shortcut click)-->  BotBridge.submit()  -->  bot

So every token the local LLM produces, every log_event line and every sentence
the bot speaks reaches the island within ~120 ms — while the island keeps its own
60 fps render loop, its own eye tracking and its own file-drop animation.

Env:
    ISLAND_UI_ONLY=1        no bot: a scripted turn so you can watch the island
    ISLAND_ASK="..."        one real turn as soon as the bot is ready, then quit
    ISLAND_LIVE_SECONDS=N   auto-quit after N seconds (handy for checks)
    PHOENIX_NO_VISION=1     skip start_vision() (never captures the screen)
    PHOENIX_CONTROL_OPEN=1  show the main app window at boot, not just on M
    PHOENIX_ISLAND_FILE=... use another island file (default: <root>\\M.PY)

Logs go to the main app window's Logs tab, never to the island: the bot's
stdout/stderr -> ~/.phoenix/bot.log, llama.cpp -> llama_server.log, and the
island's own trace -> phoenix_island.log.
"""

import importlib
import importlib.util
import os
import sys
import threading
import time
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
# running `python island_clone/phoenix_live.py` puts island_clone/ on sys.path,
# never the project root -- the bot lives in the root
for _p in (str(HERE), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("PHOENIX_SILENT", "1")
os.environ.setdefault("PHOENIX_LLAMA_SERVER",
                      r"C:\Users\Yp921\Downloads\llama.cpp\llama-server.exe")

import island_brain  # noqa: E402
from island_bridge import BotBridge, IslandState  # noqa: E402
import phoenix_store  # noqa: E402
import phoenix_logs  # noqa: E402
from phoenix_control import PhoenixControl  # noqa: E402

FEED_HZ = 8.0

# The bot's mood vocabulary -> the island's emotion engine (M.PY's words).
MOOD_EMOTION = {
    "idle": "idle",
    "listening": "greeting",
    "thinking": "thinking",
    "acting": "thinking",
    "speaking": "loving",
    "happy": "happy",
    "alert": "error",
}

# The bot's mood vocabulary -> the island's window state machine.  "working"
# means the big card (thought line + action box + speech line) stays open, which
# is why speaking/happy/alert stay on "working" first and retract later.
MOOD_STATE = {
    "idle": "waiting",
    "listening": "waiting",
    "thinking": "thinking",
    "acting": "acting",
    "speaking": "working",
    "happy": "working",
    "alert": "working",
}

# after a turn ends the answer lingers this long, then the island retracts
SETTLE_AFTER = {"happy": 6.0, "alert": 8.0}


def island_source(explicit=None):
    """Find M.PY.  Windows is case-insensitive, other systems are not."""
    if explicit:
        return Path(explicit)
    if os.environ.get("PHOENIX_ISLAND_FILE"):
        return Path(os.environ["PHOENIX_ISLAND_FILE"])
    for name in ("M.PY", "M.py", "phoenix_dynamic_island.py"):
        candidate = ROOT / name
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"no island file in {ROOT} (looked for M.PY, M.py)")


def load_island_class(path=None):
    """Import the island from its file (its __main__ block stays asleep).

    The file is M.PY -- an upper-case extension -- and the usual
    spec_from_file_location() refuses those, so the loader is explicit.
    """
    path = island_source(path)
    loader = SourceFileLoader("phoenix_island_source", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    loader.exec_module(module)
    if not hasattr(module, "PhoenixDynamicIslandOverlay"):
        raise AttributeError(f"{path} has no PhoenixDynamicIslandOverlay")
    return module.PhoenixDynamicIslandOverlay


def _one_line(text, limit=90):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "\u2026"


def _tail(text, limit=62):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[-limit:]


def _last_bot_line(snap):
    for entry in reversed(snap.get("transcript") or []):
        if entry.get("role") == "bot" and entry.get("text"):
            return entry["text"]
    return ""


class IslandFeed:
    """IslandState -> island.set_*(), on its own thread.

    The island's public setters are queue-based, so this is the thread-safe way
    to drive it from outside: no attribute poking, no pygame calls off-thread.
    Every push is guarded — a broken island must only lose the picture, never
    the poller.
    """

    def __init__(self, state, island, hz=FEED_HZ):
        self.state = state
        self.island = island
        self.interval = 1.0 / max(1.0, float(hz))
        self.pushes = 0
        self.last_error = ""
        self._stop = threading.Event()
        self._thread = None
        self._mood = None
        self._turn = None
        self._state_sent = None
        self._emotion = None
        self._thought = None
        self._chip = None
        self._speech = None
        self._settle_at = None
        self._retracted = False
        self._emotion_hold_until = 0.0
        self._click_through = False

    def hold_emotion(self, seconds):
        """Keep the emotion someone else just set (the user's own words, from
        the brain) on the face for a while instead of the bot's mood."""
        self._emotion_hold_until = time.time() + max(0.0, float(seconds))

    # -- the mapping, pure enough to call from a test --------------------
    def push(self, snap):
        mood = snap.get("mood") or "idle"
        turn = int(snap.get("turn") or 0)

        if turn != self._turn:                 # a new turn: fresh card
            self._turn = turn
            self._thought = self._chip = self._speech = None

        if mood != self._mood:                 # a new mood: restart the clock
            self._mood = mood
            self._settle_at = None
            self._retracted = False

        want = MOOD_STATE.get(mood, "working")
        if mood in SETTLE_AFTER and not self._retracted:
            if self._settle_at is None:
                self._settle_at = time.time() + SETTLE_AFTER[mood]
            elif time.time() >= self._settle_at:
                self._retracted = True
                want = "done" if mood == "happy" else "waiting"
        if want != self._state_sent:
            self._state_sent = want
            self._push("set_state", want)

        # the capsule gets out of the way while a turn runs: WS_EX_TRANSPARENT,
        # so the bot's own clicks reach what it is aiming at behind the island
        click = bool(snap.get("busy"))
        if click != self._click_through:
            self._click_through = click
            if callable(getattr(self.island, "set_click_through", None)):
                self._push("set_click_through", click)

        emotion = MOOD_EMOTION.get(mood, "idle")
        if emotion != self._emotion and time.time() >= self._emotion_hold_until:
            self._emotion = emotion
            self._push("set_emotion", emotion)

        # the live token tail while it writes, then the answer it produced,
        # then whatever the state says it is doing
        answer = _last_bot_line(snap)
        if snap.get("thinking") and snap.get("stream"):
            thought = _tail(snap["stream"])
        elif not snap.get("thinking") and answer:
            thought = _one_line(answer, 62)
        else:
            thought = _one_line(snap.get("headline"), 62)
        if thought and thought != self._thought:
            self._thought = thought
            self._push("set_thought", thought)

        chips = snap.get("actions") or []
        # the badge counts what the bot has reported THIS turn, not the turn
        # number: IslandState.action_count resets with every user message
        step = max(1, int(snap.get("action_count") or 0))
        if chips:
            head = chips[-1]
            mark = {"done": "+ ", "fail": "! "}.get(head.get("state"), "")
            label = mark + _one_line(head.get("message"), 90)
            key = (label, head.get("state"), step)
            if key != self._chip:
                self._chip = key
                self._push("set_action", label, step)

        # what it is saying, or the answer it just produced
        speech = snap.get("speech") or ""
        if not speech and not snap.get("thinking"):
            speech = _last_bot_line(snap)
        if speech and speech != self._speech:
            self._speech = speech
            # the whole sentence: the island's chat panel wraps and scrolls it
            self._push("set_speech", _one_line(speech, 2000))

    def _push(self, name, *args):
        try:
            getattr(self.island, name)(*args)
            self.pushes += 1
            return True
        except Exception as e:                 # never take the feed down
            self.last_error = f"{name}: {type(e).__name__}: {e}"
            return False

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.push(self.state.snapshot())
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
            self._stop.wait(self.interval)

    def start(self):
        self._thread = threading.Thread(target=self._loop, name="island-feed",
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()


class Live:
    """Owns one island, one bridge and (optionally) the real bot."""

    def __init__(self, state, bridge, island):
        self.state = state
        self.bridge = bridge
        self.island = island
        self.bot = None
        self.ready = False
        self.brain = None
        self.feed = IslandFeed(state, island)
        # The Tk main app window (mascots, models, settings).  Built on the
        # main thread inside run(), because that is the only thread Tk will
        # accept a window on - and it is the thread the island renders on.
        self.control = None
        self.store = phoenix_store.MascotStore()
        island.on_user_input = self.on_user_input
        island.on_interrupt = self.on_interrupt

    # -- island -> bot ---------------------------------------------------
    def on_user_input(self, text):
        text = (text or "").strip()
        if not text:
            return False
        if self.brain is not None:
            self.brain.apps.note_text(text)     # clicked a tab / named an app
        if not self.ready or self.bot is None:
            self.island.set_speech("Still waking up - llama-server is starting.")
            self.island.set_emotion("sleepy")
            return False
        if not self.bridge.submit(text):
            self.island.set_speech("Still working on the last one. Esc cancels it.")
            self.island.set_emotion("thinking")
            return False
        self.island.set_state("working")
        self.island.set_emotion("thinking")
        self.island.set_thought(_one_line(f"You: {text}", 62))
        return True

    def on_interrupt(self):
        """Esc on the island: cancel a running turn, otherwise close the island.

        Four quick pokes call the same callback, but a poke is a nudge -- the
        island flags itself dizzy first, so it never closes on a poke.
        """
        if self.bridge.busy:
            self.bridge.stop()                 # bumps generation_id + stops TTS
            if self.bot is not None:
                try:
                    self.bot.ctm.interrupt_now("user_cancel")
                except Exception:
                    pass
            self.island.set_state("waiting")
            self.island.set_emotion("bored")
            self.island.set_speech("Stopped.")
            return "cancelled"
        if getattr(self.island, "is_dizzy", False):
            self.island.set_speech("Poked. Nothing was running to cancel.")
            return "poked"
        self.quit()
        return "quit"

    def quit(self):
        self.island.running = False

    # -- bot -> island ---------------------------------------------------
    def import_bot(self):
        """Import the production bot module.  MUST run on the MAIN thread.

        bot_mk9 does `import torch` at module level and then pulls in
        OmniParser -> easyocr -> torchvision -> av.  Those are C extensions
        that install their own interpreter state, and importing them from a
        worker thread while the island's render loop is spinning on the main
        thread kills the process outright:

            Fatal Python error: PyEval_RestoreThread: the function must be
            called with the GIL held, after Python initialization and before
            Python finalization, but the GIL is released
            (the current Python thread state is NULL)

        There is no way to catch that - it is fatal, not an exception - so
        the import happens once, on the main thread, before the render loop
        claims it.  Returns the module, or None if every candidate failed.
        """
        if self.bot is not None:
            return self.bot
        # MK9 is the default now: it boots text-only and loads the vision
        # encoder on the first turn that needs it. Set PHOENIX_BOT to
        # bot_mk8_vlm to go back to the always-vision behaviour.
        wanted = os.environ.get("PHOENIX_BOT", "bot_mk9")
        bot = None
        last_err = None
        for name in (wanted, "bot_mk8_vlm"):
            try:
                t0 = time.time()
                bot = importlib.import_module(name)
                print(f"[live] imported {name} in {time.time() - t0:.1f}s")
                if name != wanted:
                    print(f"[live] {wanted} unavailable, fell back to {name}")
                break
            except Exception as e:          # try the next candidate
                last_err = e
        if bot is None:
            print(f"[live] could not import the bot: "
                  f"{type(last_err).__name__}: {last_err}")
            self.state.set(last_error=str(last_err))
            self.state.set_mood("alert", "Could not import the bot",
                                f"{type(last_err).__name__}: {last_err}")
            return None
        self.bot = bot
        return bot

    def boot(self, no_vision=None):
        """Install the hooks and start the brain.  Never raises.

        This is what main_loop() does before its stdin loop, minus the stdin
        loop: the island is the input now.  Safe to call from a daemon
        thread: the heavy import has already happened on the main thread via
        import_bot(), and this returns straight away if it did.
        """
        bot = self.import_bot()
        if bot is None:
            return None
        self.bridge.install(bot)               # the bot file itself is untouched
        print(f"[live] {bot.__name__} imported, hooks installed")
        # the voice toggle: the island flips the bot's TTS and reads its state
        self.island.on_toggle_voice = getattr(bot, "toggle_voice", None)
        self.island.on_voice_state = lambda: bool(getattr(bot, "TTS_ENABLED", True))
        self.brain = island_brain.Brain(island=self.island, feed=self.feed).install(bot)
        print(f"[live] brain installed: {', '.join(self.brain.installed)}"
              + (f" (error: {self.brain.last_error})" if self.brain.last_error else ""))
        self.state.set_mood("thinking", "Waking up", "importing the brain")
        try:
            if no_vision is None:
                no_vision = os.environ.get("PHOENIX_NO_VISION", "0") == "1"
            if not no_vision:
                bot.start_vision()
            bot.server.ensure_started()
            self.ready = True
            print(f"[live] bot ready - llama-server {bot.server.base_url}")
            self.state.set_mood("listening", "PHOENIX is awake",
                                f"llama-server {bot.server.base_url}")
            self.state.begin_stream()
            self.state.end_stream("Ready. Type a task and press Enter.")
            self._smoke()
        except Exception as e:
            print(f"[live] brain error: {type(e).__name__}: {e}")
            self.state.set(last_error=str(e))
            self.state.set_mood("alert", "Brain offline",
                                f"{type(e).__name__}: {e}")
        return bot

    def _smoke(self):
        """ISLAND_ASK="..." : one real turn as soon as the bot is ready, then quit."""
        ask = os.environ.get("ISLAND_ASK", "").strip()
        if not ask:
            return
        print(f"[live] smoke turn: {ask!r}")

        def turn():
            self.on_user_input(ask)
            self.bridge.wait(300)
            snap = self.state.snapshot()
            answers = [m for m in snap["transcript"] if m["role"] == "bot"]
            print(f"[live] smoke done - tokens {snap['tokens']}, "
                  f"chips {len(snap['actions'])}")
            for m in answers[-1:]:
                print(f"[live] answer: {m['text'][:400]}")
            self.quit()

        threading.Timer(1.5, turn).start()

    def attach_control(self) -> bool:
        """Build the main app window on THIS thread and give it to the island.

        Tk insists a window be created and driven from one thread, and the
        island already owns the main thread, so we create it here - before the
        render loop starts - and let the island pump it once per frame through
        on_frame.  Nothing here may stop the island: every failure path just
        carries on without the window.
        """
        isl = self.island
        try:
            self.store.load()
        except Exception as e:
            print(f"[live] mascot store unavailable: {e}")
        if self.store.last_error:
            print(f"[live] {self.store.last_error} - using the defaults")
        try:
            isl.set_mascots(self.store.mascots, self.store.active)
            print(f"[live] mascots: {len(self.store.mascots)} loaded, "
                  f"'{isl.mascot_label()}' on screen")
        except Exception as e:
            print(f"[live] mascots not applied: {e}")

        try:
            self.control = PhoenixControl(island=isl, store=self.store)
            if not self.control.build():
                print("[live] no window could be created - "
                      "the island runs on its own")
                self.control = None
                return False
        except Exception as e:
            print(f"[live] main app window unavailable: "
                  f"{type(e).__name__}: {e}")
            self.control = None
            return False

        isl.on_open_control = self.control.show
        isl.on_frame = self.control.pump
        print("[live] main app window ready - press M on the island to open it, "
              "and closing it leaves the island running")
        if os.environ.get("PHOENIX_CONTROL_OPEN", "0") == "1":
            self.control.show()
        return True

    def run(self):
        """Render on the main thread; the feed and the boot are daemons."""
        self.attach_control()
        self.feed.start()
        try:
            self.island.run()                  # blocks until island.running is False
        except KeyboardInterrupt:
            self.island.running = False
        finally:
            self.feed.stop()
            if self.control is not None:
                self.control.destroy()


def demo_brain(state):
    """ISLAND_UI_ONLY=1: behave like a bot so the island can be watched.

    Nothing here touches bot_mk8_vlm.
    """
    def step():
        state.user_said("apply the tva change and run the tests")
        state.set_mood("thinking", "Reading your request", "NEEDLE")
        state.begin_stream()
        reply = ("I opened invoice.ts, set TVA to 0.20, and the suite is green: "
                 "48 passed. The Stripe payout and yesterday's Claude Code "
                 "sessions are untouched.")
        for i in range(0, len(reply), 6):
            state.token(reply[i:i + 6])
            state.action("NEEDLE", "search_intent('tva rate')")
            time.sleep(0.02)
        state.action("TASK", "Applied the TVA change to invoice.ts")
        state.end_stream(reply)
        state.resolve_actions(True, "48 tests passed")
        state.speaking("Done. TVA is 20% and all 48 tests pass.")
        time.sleep(1.5)
        state.set_mood("happy", "Done", "task complete")
        time.sleep(2.0)
        # a failing beat, so the error face is part of the demo too
        state.set_mood("alert", "That action was rejected", "driver refused the click")

    threading.Timer(1.5, step).start()


def main():
    # Give the bot a log file before anything else prints.  The bot itself is
    # never edited - it prints to stdout, and this tees that into
    # ~/.phoenix/bot.log so the main app's Logs tab can read it.  Installing
    # it first means the banner below is in the log too.
    try:
        phoenix_logs.install_tee()
    except Exception as e:
        print(f"[live] log tee unavailable: {e}")

    source = island_source()
    print("=" * 66)
    print("  PHOENIX MK8 - Dynamic Island, live")
    print(f"  island : {source}")
    print("  Enter on the island to type  ·  Esc cancels, or quits when idle")
    print("  M opens the main app window (mascots, models, settings)")
    print("    - closing it leaves the floating island running")
    print("  Drag any file from Explorer onto it and the bot takes that turn")
    print("=" * 66)

    try:
        island_class = load_island_class(source)
    except Exception as e:
        print(f"[live] FATAL: could not load {source}: {type(e).__name__}: {e}")
        return 1

    state = IslandState()
    bridge = BotBridge(state)
    live = Live(state, bridge, island_class())

    if os.environ.get("ISLAND_LIVE_SECONDS"):
        seconds = float(os.environ["ISLAND_LIVE_SECONDS"])
        threading.Timer(seconds, live.quit).start()
        print(f"[live] auto-quit in {seconds:g}s (ISLAND_LIVE_SECONDS)")

    if os.environ.get("ISLAND_UI_ONLY", "0") == "1":
        print("[live] UI only - no bot, a scripted turn (ISLAND_UI_ONLY=1)")
        demo_brain(state)
        live.run()
    else:
        # The bot import pulls in torch, torchvision and av.  Those must be
        # imported on THIS thread: done from the boot daemon they abort the
        # interpreter with a fatal PyEval_RestoreThread error.  See
        # Live.import_bot.
        print("[live] loading the bot (torch, easyocr, vision stack)...")
        live.import_bot()

        # The bot's own threads (vision capture, TTS, server watchdog, its Tk
        # status dot) must NOT come up while this thread is still creating the
        # window and the OpenGL context.  Both sides initialise C extensions
        # that drop the GIL - PyAV builds a filter graph, pygame/moderngl build
        # a GL context - and doing that at the same moment aborts the
        # interpreter with the same fatal PyEval_RestoreThread error.  So the
        # island gets its window and a few steady GL frames FIRST, and only
        # then is the bot allowed to start.
        delay = float(os.environ.get("PHOENIX_BOOT_DELAY", "3.0"))
        print(f"[live] island first, bot threads in {delay:g}s "
              f"(PHOENIX_BOOT_DELAY)")
        threading.Timer(delay, live.boot).start()
        live.run()
    snap = state.snapshot()
    answers = [m["text"] for m in snap["transcript"] if m["role"] == "bot"]
    print(f"[live] island closed. feed pushes: {live.feed.pushes}"
          f", turns: {snap['turn']}, tokens: {snap['tokens']}"
          f", emotion: {live.feed._emotion or '-'}"
          + (f", answers: {len(answers)}" if answers else "")
          + (f" (last error: {live.feed.last_error})" if live.feed.last_error else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
