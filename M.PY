"""
Phoenix Dynamic Island HUD - Connected to PHOENIX MK4 Desktop Agent
Integrates the ModernGL / OpenGL borderless transparent Dynamic Island
directly with PHOENIX MK4 (Qwen3-VL + OmniParser + Nidle Hands + RAG).

Features:
- Borderless floating Dynamic Island HUD on Windows (no background box).
- Crash-proof dual engine: ModernGL 3.3 Core Profile with automatic Pygame software fallback.
- Top-Right Settings Icon (⚙️): Click to select which 4 app shortcuts appear in the main grid!
- App Shortcuts Grid: WhatsApp, Spotify, YouTube, Browser, Snapchat, Discord, Terminal, VS Code.
- Expressive Avatar Emotions: Greeting, Happy, Thinking, Bored, Loving, Exhausted, Sleepy, Dizzy.
- Dynamic Eye-Tracking: Eyes follow the desktop mouse cursor in real-time.
- Interactive File Pickup Animation:
    1. Island expands.
    2. Bot acts as a letterbox, follows the incoming file, and opens a top slot.
    3. File drops into the bot slot.
    4. Bot jumps to the left side of the island.
    5. Bot shrinks into a runner head and travels with a glowing trail to the right as a progress bar!
- Real Chat input bar & Action Stream HUD.
- Full debug logging to 'phoenix_island.log'.
"""

import sys
import os
import time
import math
import array
import threading
import queue
import io
import json
import random
import re
try:                       # optional: only the alpha path needs it
    import numpy as np
except Exception:
    np = None
from datetime import datetime
import traceback
from dataclasses import dataclass
from typing import Optional, Callable, Tuple, List, Dict

# Ensure debug log exists
LOG_FILE = "phoenix_island.log"

def log_debug(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    formatted = f"[{ts}] {msg}"
    print(formatted, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(formatted + "\n")
    except Exception:
        pass

log_debug("Phoenix Dynamic Island booting...")

try:
    import pygame
    from pygame.locals import *
except ImportError:
    log_debug("FATAL: pygame not installed. Please run: pip install pygame")
    sys.exit(1)

MODERNGL_AVAILABLE = False
try:
    import moderngl
    MODERNGL_AVAILABLE = True
    log_debug("ModernGL loaded successfully.")
except ImportError:
    log_debug("Notice: moderngl not installed. Will use high-fidelity Pygame fallback.")

IS_WINDOWS = sys.platform.startswith("win")


def set_window_pos(hwnd, x: int, y: int, w: int, h: int,
                   flags: int, topmost: bool = True) -> bool:
    """SetWindowPos with its real argtypes, which is the whole point.

    Called straight off ctypes.windll, every argument is marshalled as a
    32-bit int and the call comes back FALSE - silently, because nothing
    checks the return value - so the island kept its full 860x576 window no
    matter how small the panel was.  The leftover transparent area below a
    76px island is what a screenshot tool paints as a black slab.

    Declaring the types once here also keeps this cheap to call per frame.
    """
    if not IS_WINDOWS or not hwnd:
        return False
    try:
        fn = ctypes.windll.user32.SetWindowPos
        fn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
                       ctypes.c_int, ctypes.c_int, ctypes.c_int,
                       ctypes.c_uint]
        fn.restype = ctypes.c_bool
        return bool(fn(hwnd, -1 if topmost else None, x, y, w, h, flags))
    except Exception as e:
        log_debug(f"SetWindowPos notice: {e}")
        return False
if IS_WINDOWS:
    try:
        import ctypes
        from ctypes import wintypes
        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
    except Exception as e:
        log_debug(f"ctypes notice: {e}")

# ---------------------------------------------------------------------------
# GLSL Shaders (gl_VertexID fullscreen quad - zero attribute KeyError)
# ---------------------------------------------------------------------------

VERTEX_SHADER = """
#version 330 core
out vec2 v_uv;
void main() {
    float x = -1.0 + float((gl_VertexID & 1) << 2);
    float y = -1.0 + float((gl_VertexID & 2) << 1);
    v_uv = vec2((x + 1.0) * 0.5, (y + 1.0) * 0.5);
    gl_Position = vec4(x, y, 0.0, 1.0);
}
"""

STARFIELD_FRAG_SHADER = """
#version 330 core
uniform vec2 u_resolution;
uniform float u_time;
uniform vec4 u_island_bounds;
in vec2 v_uv;
out vec4 fragColor;

float hash12(vec2 p) {
    vec3 p3  = fract(vec3(p.xyx) * 0.1031);
    p3 += dot(p3, p3.yzx + 33.33);
    return fract((p3.x + p3.y) * p3.z);
}

void main() {
    vec2 fragCoord = v_uv * u_resolution;
    float left = u_island_bounds.x;
    float bottom = u_island_bounds.y;
    float width = u_island_bounds.z;
    float height = u_island_bounds.w;
    
    if (fragCoord.x < left || fragCoord.x > left + width ||
        fragCoord.y < bottom || fragCoord.y > bottom + height) {
        discard;
    }

    vec2 localUV = (fragCoord - vec2(left, bottom)) / vec2(width, height);
    vec3 color = vec3(0.006, 0.008, 0.015);
    
    vec2 gridUV = localUV * vec2(38.0, 14.0);
    vec2 id = floor(gridUV);
    vec2 gv = fract(gridUV) - 0.5;
    
    for (int y = -1; y <= 1; y++) {
        for (int x = -1; x <= 1; x++) {
            vec2 offset = vec2(float(x), float(y));
            vec2 cellId = id + offset;
            float randVal = hash12(cellId);
            
            if (randVal > 0.62) {
                vec2 starPos = offset + (vec2(hash12(cellId + 0.1), hash12(cellId + 0.2)) - 0.5) * 0.8;
                float dist = length(gv - starPos);
                float twinkle = sin(u_time * 4.0 + randVal * 12.0) * 0.5 + 0.5;
                float brightness = (0.014 / (dist + 0.005)) * twinkle * randVal;
                vec3 starCol = mix(vec3(0.5, 0.8, 1.0), vec3(1.0, 0.9, 0.7), hash12(cellId + 0.3));
                color += starCol * brightness * 0.5;
            }
        }
    }
    float vig = smoothstep(0.0, 0.12, localUV.x) * smoothstep(1.0, 0.88, localUV.x);
    fragColor = vec4(color * vig, 1.0);
}
"""

UI_FRAG_SHADER = """
#version 330 core
uniform sampler2D u_texture;
in vec2 v_uv;
out vec4 fragColor;
void main() {
    vec4 col = texture(u_texture, vec2(v_uv.x, 1.0 - v_uv.y));
    fragColor = col;
}
"""

# ---------------------------------------------------------------------------
# App Shortcuts Registry
# ---------------------------------------------------------------------------
ALL_SHORTCUT_APPS = [
    {"id": "whatsapp", "name": "WhatsApp", "icon": "💬", "color": (37, 211, 102), "action": "open whatsapp desktop"},
    {"id": "spotify", "name": "Spotify", "icon": "🎵", "color": (30, 215, 96), "action": "play favorites on spotify"},
    {"id": "youtube", "name": "YouTube", "icon": "▶", "color": (255, 60, 60), "action": "browse trending youtube"},
    {"id": "browser", "name": "Browser", "icon": "🌐", "color": (66, 133, 244), "action": "launch google chrome"},
    {"id": "snapchat", "name": "Snapchat", "icon": "👻", "color": (255, 235, 59), "action": "open snapchat web"},
    {"id": "discord", "name": "Discord", "icon": "🎮", "color": (114, 137, 218), "action": "open discord app"},
    {"id": "terminal", "name": "Terminal", "icon": ">_", "color": (180, 190, 205), "action": "spawn bash terminal"},
    {"id": "vscode", "name": "VS Code", "icon": "⚡", "color": (0, 122, 204), "action": "open current project in vscode"},
]

# ---------------------------------------------------------------------------
# Phoenix Dynamic Island Overlay Class
# ---------------------------------------------------------------------------

class PhoenixDynamicIslandOverlay:
    """
    Drop-in replacement for StatusOverlay that connects Phoenix MK4 to the
    ModernGL Dynamic Island HUD with full emotion engine, file pickup animation,
    and shortcut settings grid.
    """
    def __init__(self, on_user_input: Optional[Callable[[str], None]] = None,
                 on_interrupt: Optional[Callable[[], None]] = None):
        self.on_user_input = on_user_input
        self.on_interrupt = on_interrupt
        
        # Live state from Phoenix bot
        self.state = "waiting"       # "waiting", "thinking", "acting", "speaking", "stalled", "done"
        self.current_thought = ""    # From <thought> tag
        # Reasoning is folded away by default.  It used to be a flat line in
        # the header, truncated at 65 characters, so a thought either showed
        # up chopped or did not show at all - and it competed with the title
        # for the same strip.  Now it is a dropdown the user opens, and the
        # full text gets its own panel ABOVE the agent's answer.
        self.thoughts_open = False
        self.thought_scroll = 0.0
        self._thought_rect = None        # the open panel, for hit-testing
        self._thoughts_max_scroll = 0
        self.current_action = ""     # Dispatched <ACTION>
        self.current_app = ""        # target_app
        self.current_speech = ""     # Latest text being spoken
        self.current_step = 0        # Task step count
        self.max_steps = 24

        # Chat panel: this turn's conversation, scrollable so nothing is cut
        self.chat_lines = []                         # (role, text): "you" / "agent"
        self.chat_scroll = 0.0                       # px scrolled from the panel top
        self._chat_max_scroll = 0.0                  # clamp, refreshed while drawing
        self._chat_rect = pygame.Rect(0, 0, 0, 0)    # last drawn panel (wheel hit-test)
        self._glyph_cache = {}                        # char -> the emoji font has it?
        
        # Emotions: 'idle', 'greeting', 'happy', 'thinking', 'bored', 'loving', 'exhausted', 'sleepy', 'dizzy', 'error'
        self.emotion = "greeting"
        self.emotion_timer = 4.0     # Stay in greeting for 4 seconds then transition to idle
        
        # Island geometry & modes
        # The window is created once and never resized, so it must be big
        # enough for the LARGEST layout the island can reach, not the idle
        # one.  The expanded view is 860x560; at the old 320 the bottom
        # 240px were drawn off the window and never seen.
        # 576 leaves a little slack for the expanded panel's shadow.
        self.screen_width = 860          # == the expanded view's width
        self.screen_height = 576
        # The GL surface stays 860x576, but the OS window tracks the panel
        # (see _sync_window).  A topmost window carrying 500px of empty
        # transparent area under a 76px island is what every screenshot
        # tool paints as a black slab, and it steals that whole rectangle
        # from whatever is underneath.
        self.win_w = self.screen_width
        self.win_h = self.screen_height
        self.current_width = 540.0
        self.current_height = 76.0
        self.current_radius = 28.0
        
        self.target_width = 540.0
        self.target_height = 76.0
        self.target_radius = 28.0
        
        # Shortcut grid & settings modal
        self.pinned_apps = ["whatsapp", "spotify", "youtube", "browser"]
        self.settings_open = False

        # -- the expanded view ------------------------------------------
        self.on_toggle_voice = None      # set by the launcher
        self.on_voice_state = None
        # The Tk main app window (phoenix_control.py) is a SEPARATE window.
        # The launcher wires this in; pressing M or the "Main app" row in
        # Settings asks for it.  Closing that window must never stop us, so
        # this callback owns nothing and returns immediately.
        self.on_open_control = None
        # Called once per rendered frame, on the same thread as run().  The
        # launcher uses it to pump the Tk event loop without the island ever
        # blocking on it.
        self.on_frame = None
        self.expanded_open = False
        self.expanded_tab = "models"        # models | history | settings
        self.expanded_scroll = 0
        self.expanded_models = []           # scanned GGUF files
        self.expanded_models_scanned = False
        self.active_model_path = ""
        self.active_mmproj_path = ""
        self.downloads = {}                 # name -> {state, pct, error}
        self.history_entries = None         # loaded lazily from disk
        self.history_path = os.path.join(
            os.path.expanduser("~"), ".phoenix", "island_chat.jsonl")
        self.setting_toggles = {
            # Opt-in: per-pixel alpha uses UpdateLayeredWindow, a
            # different path from the colorkey window this island has
            # always used.  Off by default so nothing changes until
            # you switch it on in Settings.
            # The island is meant to be semi-transparent, so this ships
            # ON.  If UpdateLayeredWindow ever fails it falls back to
            # colorkey (verified), so the window can never vanish.
            "translucent": True,
            # How opaque the panel stays. 0.85 is the floor: over a white
            # desktop the darkest panel background reachable at alpha a is
            # (1-a)*255, and below 0.85 the dimmest text (150,158,175) drops
            # under the 4.5:1 WCAG ratio.  check_transparency proves it.
            "translucent_level": 0.86,
            "click_through": True,
            # ("personality_picker" used to live here.  The picker moved to
            # the main app window, so the flag went with it.)
            "speech_wave": True,
            "show_step_badge": True,
            "shortcuts_always": True,
        }
        # per-pixel alpha support; falls back to the colorkey window
        self._per_pixel_alpha = False
        self._alpha_failed = False   # never retry a failing path
        self._alpha_applied = False
        
        # Interactive Chat Input on the Island
        self.input_active = False
        self.input_text = ""
        self.cursor_blink = 0.0
        
        # File Pickup Animation State
        # Stages: 'none', 'incoming', 'slot_open', 'intake', 'jump_left', 'progress_bar', 'completed'
        self.file_anim_state = "none"
        self.file_anim_progress = 0.0
        self.file_anim_name = "document.pdf"
        self.file_anim_size = "2.4 MB"
        self.file_trail_particles: List[Dict] = []
        
        # Internal Threading
        self.msg_queue = queue.Queue()
        self.thread: Optional[threading.Thread] = None
        self.running = False
        
        # Avatar poke physics
        self.is_dizzy = False
        self.dizzy_timer = 0.0
        self.avatar_hits = 0
        self.last_hit_time = 0.0
        self.bounce_offset = 0.0

        # -- the living avatar ------------------------------------------
        # Personalities retune every number the avatar animation uses, so a
        # Butler breathes slowly and still while an Anime Girl is bouncy.
        self.personality = "normal"
        # -- the mascot gallery ----------------------------------------
        # The personality picker used to live here, in this panel.  It now
        # lives in the main app window; the island is told what to show.
        # self.mascots is {key: {name, personality, head, accent, shape,
        # emoji, size}}; self.mascot_key says which one is on screen.
        self.mascots = {}
        self.mascot_key = ""
        self._preset_cache = None
        self._preset_dirty = True
        self.anim_t = 0.0            # animation clock, advanced by _update
        self._blink_t = 1.2          # countdown to the next blink
        self._blink_phase = 1.0      # 1.0 = eyes open, 0.0 = shut
        self._blink_queued = 0       # extra blinks (double / triple)
        self._gaze = [0.0, 0.0]      # current eye offset, eased
        self._gaze_target = [0.0, 0.0]
        self._gaze_hold = 0.0        # countdown until the next saccade
        self._speaking_until = 0.0   # time.time() while the bot talks
        self._speech_env = 0.0       # eased speaking energy 0..1
        self._body_lean = 0.0
        self._arm_wave = 0.0
        self._antenna = (0.0, 0.0)     # (angle, angular velocity)
        self._antenna_target = 0.0
        # (there used to be floating "motes" here - single set_at pixels
        #  scattered around the avatar.  They read as dirt on the panel, so
        #  they are gone; see scratch/remove_motes.py)
        self._energy = 0.0             # 0 idle .. 1 excited

        # -- soft-body motion: the head leans and deforms, it is not a
        # rigid rect that slides around.  Both are real springs, so they
        # overshoot and settle instead of easing to a stop dead flat.
        self._tilt = 0.0               # head lean, -1..1
        self._tilt_v = 0.0
        self._squish = 0.0             # + squashed, - stretched
        self._squish_v = 0.0
        self._breathe = 0.0            # -1..1 breathing phase
        self._fidget = 0.0             # a micro-shrug that decays to nothing
        self._fidget_t = 3.0
        self._wink_eye = -1            # one eye lags the other; -1 = neither
        self._ear_tw = [0.0, 0.0]      # ear twitch phases
        self._nod = 0.0                # nod impulse when a reply lands
        self._nod_v = 0.0
        self._pop = 0.0                # surprise pop when an action starts

        # -- activity: what it is doing, at a glance ---------------------
        #: Each entry is the target weight of a pose while that activity is
        #: current.  They cross-fade, so the face morphs between them
        #: instead of snapping.
        self._ACTIVITY_POSE = {
            "idle":    dict(tilt=0.00, bob=0.0, arm=0.15, ring=0.0),
            "working": dict(tilt=0.32, bob=1.4, arm=0.75, ring=1.0),
            "done":    dict(tilt=-0.20, bob=0.0, arm=0.30, ring=0.0),
        }
        self.activity = "idle"
        self._activity_mix = {"idle": 1.0, "working": 0.0, "done": 0.0}
        self._done_flash = 0.0         # fades out after a job finishes

        # -- startup splash ---------------------------------------------
        self._splash_t = 0.0
        self._splash_seen = 0.0

        # -- expanded view transitions ----------------------------------
        self._expanded_anim = 0.0      # 0 closed .. 1 fully open
        self._tab_anim = 1.0           # 0 just switched .. 1 settled
        self._tab_dir = 1.0            # +1 sliding in from the left
        
        self.window_pos_x = 300
        self.window_pos_y = 0
        self.click_through = False
        self._scan_hidden = 0
        self._scan_is_hidden = False
        self.use_moderngl = MODERNGL_AVAILABLE
        self.start_time = time.time()

    # ---- Bot API Callbacks (Called by bot_mk8_vlm.py) ----
    def set_state(self, state: str):
        """Called by bot: 'waiting', 'working', 'thinking', 'acting', 'done', 'stalled'."""
        if state == "done":
            self._done_flash = 1.0
        self.state = state
        self.msg_queue.put(("STATE", state))

    @staticmethod
    def _clean_ui_text(text: str) -> str:
        """The model writes '<thought>hi</thought>'; the island only ever shows
        'hi'.  Every piece of bot text runs through this."""
        text = re.sub(r"<\s*/?\s*[A-Za-z_][\w.-]*\s*>", " ", str(text or ""))
        text = re.sub(r"^PHOENIX:\s*", "", text, flags=re.I)
        return " ".join(text.split()).strip()

    def set_emotion(self, emotion: str):
        """Called by bot: 'greeting', 'happy', 'thinking', 'bored', 'loving', 'exhausted', 'sleepy', 'dizzy'."""
        self.emotion = emotion
        self.msg_queue.put(("EMOTION", emotion))

    def set_thought(self, thought: str):
        thought = self._clean_ui_text(thought)      # never show the raw markup
        self.current_thought = thought
        self.msg_queue.put(("THOUGHT", thought))

    def set_action(self, action: str, step: int = 1):
        action = self._clean_ui_text(action)         # '<ACTION>click(x)</ACTION>'
        self.current_action = action                 # becomes 'click(x)'
        self.current_step = step
        if action:
            self._pop = 1.0           # it startles, then settles
        self.msg_queue.put(("ACTION", (action, step)))

    def set_speech(self, text: str):
        text = self._clean_ui_text(text)
        self.current_speech = text
        # Keep the mouth moving for as long as the line is worth reading.
        try:
            self._speaking_until = time.time() + min(8.0, 1.2 + len(text) * 0.045)
        except Exception:
            pass
        if text:
            self._nod = 1.0           # it nods as the reply arrives
        self._chat_add("agent", text)
        self.msg_queue.put(("SPEECH", text))

    # -- Chat panel (the AGENT box): keeps the whole turn, scrollable --------
    # -- the whole history, not just the 40 lines the island keeps ------
    def _history_dir(self):
        d = os.path.dirname(self.history_path)
        try:
            os.makedirs(d, exist_ok=True)
        except Exception as e:
            log_debug(f"history dir not writable: {e}")
            return None
        return d

    def _chat_persist(self, role: str, text: str):
        """Append one turn to the on-disk history.  Never raises."""
        if not self.setting_toggles.get("chat_history", True):
            return
        if not self._history_dir():
            return
        try:
            stamp = datetime.now().isoformat(timespec="seconds")
            with io.open(self.history_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": stamp, "role": role,
                                     "text": text}, ensure_ascii=False) + "\n")
        except Exception as e:
            log_debug(f"history write failed: {e}")

    def load_history(self, limit=400):
        """Whole chat history, newest first.  Cached after the first read."""
        if self.history_entries is not None:
            return self.history_entries
        rows = []
        try:
            if os.path.exists(self.history_path):
                with io.open(self.history_path, encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rows.append(json.loads(line))
                        except Exception:
                            continue
        except Exception as e:
            log_debug(f"history read failed: {e}")
        rows = rows[-limit:]
        self.history_entries = list(reversed(rows))
        return self.history_entries

    def clear_history_cache(self):
        self.history_entries = None

    def load_history_turn(self, idx: int):
        """Put a past turn back into the live chat."""
        rows = self.load_history()
        if not (0 <= idx < len(rows)):
            return False
        row = rows[idx]
        self.chat_lines.append((row.get("role", "agent"), row.get("text", "")))
        del self.chat_lines[:-40]
        return True

    def _chat_add(self, role: str, text: str):
        text = self._clean_ui_text(text)
        if not text:
            return
        self.chat_lines.append((role, text))
        if len(self.chat_lines) > 40:
            del self.chat_lines[:-40]
        self._chat_persist(role, text)
        self.chat_scroll = float("inf")          # stick to the newest line

    def _chat_reset(self, user_text: str = ""):
        """A new turn: the panel starts fresh with your own words."""
        self.chat_lines = []
        self.chat_scroll = 0.0
        self._chat_max_scroll = 0.0
        self._chat_add("you", user_text)

    def _scroll_chat(self, delta_px: float):
        """Positive scrolls toward older lines, negative toward newer; clamped."""
        self.chat_scroll = max(0.0, min(self.chat_scroll - delta_px,
                                        float(self._chat_max_scroll)))

    def set_app(self, app_name: str):
        self.current_app = app_name
        self.msg_queue.put(("APP", app_name))

    def trigger_file_pickup(self, filename: str, filesize: str = "2.4 MB"):
        """Triggers the letterbox intake and progress trail animation."""
        self.file_anim_name = filename
        self.file_anim_size = filesize
        self.file_anim_state = "incoming"
        self.file_anim_progress = 0.0
        self.file_trail_particles.clear()
        self.target_width = 720.0
        self.target_height = 190.0

    def start(self):
        """Start the ModernGL Dynamic Island in a background thread."""
        self.running = True
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()

    def run(self):
        """Run directly on the main thread (for standalone execution)."""
        self.running = True
        self._run_loop()

    # -----------------------------------------------------------------------
    # Pygame / ModernGL Window & Render Loop
    # -----------------------------------------------------------------------
    def _run_loop(self):
        try:
            pygame.init()
            log_debug("Pygame initialized.")

            # Position at Top-Center of Screen
            pos_x = 300
            pos_y = 0
            if IS_WINDOWS:
                try:
                    user32 = ctypes.windll.user32
                    screen_w = user32.GetSystemMetrics(0)
                    # Centre the WINDOW, so that a capsule centred inside
                    # it also sits on the screen centre.
                    pos_x = max(0, (screen_w - self.screen_width) // 2)
                    os.environ['SDL_VIDEO_WINDOW_POS'] = f"{pos_x},{pos_y}"
                    log_debug(f"Positioned window at top center: ({pos_x}, {pos_y}) on {screen_w}px screen.")
                except Exception as e:
                    log_debug(f"Position calculation note: {e}")

            self.window_pos_x = pos_x
            self.window_pos_y = pos_y

            # Setup Window
            flags = pygame.NOFRAME
            self.surface = None

            if self.use_moderngl:
                try:
                    pygame.display.gl_set_attribute(GL_CONTEXT_MAJOR_VERSION, 3)
                    pygame.display.gl_set_attribute(GL_CONTEXT_MINOR_VERSION, 3)
                    pygame.display.gl_set_attribute(GL_CONTEXT_PROFILE_MASK, GL_CONTEXT_PROFILE_CORE)
                    pygame.display.set_caption("Phoenix Dynamic Island HUD")
                    self.surface = pygame.display.set_mode((self.screen_width, self.screen_height), pygame.OPENGL | pygame.DOUBLEBUF | flags)
                    self.ctx = moderngl.create_context()
                    log_debug("OpenGL 3.3 Core profile & ModernGL context created successfully.")
                except Exception as gl_err:
                    log_debug(f"ModernGL setup warning: {gl_err}. Falling back to standard Pygame renderer.")
                    self.use_moderngl = False

            if not self.use_moderngl:
                pygame.display.set_caption("Phoenix Dynamic Island HUD")
                self.surface = pygame.display.set_mode((self.screen_width, self.screen_height), flags)
                log_debug("Pygame native 2D display mode initialized.")

            # Windows Transparency (borderless floating island)
            if IS_WINDOWS:
                self._apply_windows_transparency(pos_x, pos_y)

            self.clock = pygame.time.Clock()
            self.start_time = time.time()

            # Shaders if ModernGL is active
            if self.use_moderngl:
                try:
                    self.prog_starfield = self.ctx.program(
                        vertex_shader=VERTEX_SHADER,
                        fragment_shader=STARFIELD_FRAG_SHADER
                    )
                    self.vao_starfield = self.ctx.vertex_array(self.prog_starfield, [])

                    self.prog_ui = self.ctx.program(
                        vertex_shader=VERTEX_SHADER,
                        fragment_shader=UI_FRAG_SHADER
                    )
                    self.vao_ui = self.ctx.vertex_array(self.prog_ui, [])
                    self.ui_texture = self.ctx.texture((self.screen_width, self.screen_height), 4)
                except Exception as sh_err:
                    log_debug(f"Shader compilation note: {sh_err}. Falling back to 2D renderer.")
                    self.use_moderngl = False

            # High Legibility Fonts (Segoe UI on Windows, crisp fallbacks)
            pygame.font.init()
            self.font_title = pygame.font.SysFont("segoeui, arial, sfprodisplay, helvetica", 17, bold=True)
            self.font_bold = pygame.font.SysFont("segoeui, arial, sfprotext, helvetica", 15, bold=True)
            self.font_regular = pygame.font.SysFont("segoeui, arial, sfprotext, helvetica", 14)
            self.font_small = pygame.font.SysFont("segoeui, arial, sfprotext, helvetica", 13)
            self.font_badge = pygame.font.SysFont("segoeui, arial, sfprotext, helvetica", 11, bold=True)
            self.font_mono = pygame.font.SysFont("consolas, cascadia code, menlo, monaco", 14, bold=True)
            # the colour-emoji font FIRST: Segoe UI Symbol has no 💬🎵🌐 and
            # draws a tofu box for every one of them
            self.font_emoji = pygame.font.SysFont("segoeuiemoji, segoeuisymbol, arial", 16)

            self.ui_surface = pygame.Surface((self.screen_width, self.screen_height), pygame.SRCALPHA)

            log_debug("Phoenix Dynamic Island render loop running.")

            # Main Loop
            while self.running:
                dt = self.clock.tick(60) / 1000.0
                self._process_bot_events()
                self._handle_input_events()
                self._update(dt)
                self._render()
                # The main app window lives in Tk, which wants its event loop
                # pumped on the thread that made it - this one.  Pumping here
                # costs a frame tick and can never block the island, and a
                # missing or misbehaving window is swallowed so the loop lives.
                if callable(self.on_frame):
                    try:
                        self.on_frame()
                    except Exception as e:
                        log_debug(f"frame hook: {e}")

            pygame.quit()
            log_debug("Phoenix Dynamic Island stopped cleanly.")

        except Exception as e:
            err_msg = traceback.format_exc()
            log_debug("="*50 + "\nEXCEPTION IN DYNAMIC ISLAND:\n" + err_msg + "\n" + "="*50)
            if IS_WINDOWS:
                try:
                    ctypes.windll.user32.MessageBoxW(0, f"Dynamic Island Error:\n{e}\n\nCheck phoenix_island.log", "Phoenix Island Error", 0x10)
                except Exception:
                    pass

    def _apply_windows_transparency(self, x: int, y: int):
        try:
            hwnd = pygame.display.get_wm_info().get('window')
            if not hwnd:
                return
            user32 = ctypes.windll.user32
            # Always on top
            SWP_SHOWWINDOW = 0x0040
            set_window_pos(hwnd, x, y, self.screen_width, self.screen_height,
                           SWP_SHOWWINDOW)
            
            # Layered colorkey transparency for RGB(1, 1, 1)
            GWL_EXSTYLE = -20
            WS_EX_LAYERED = 0x00080000
            LWA_COLORKEY = 0x00000001
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED)
            COLORKEY = 0x00010101
            user32.SetLayeredWindowAttributes(hwnd, COLORKEY, 0, LWA_COLORKEY)
            log_debug("Applied Windows HWND_TOPMOST and Layered colorkey transparency.")
        except Exception as e:
            log_debug(f"Transparency notice: {e}")

    # -----------------------------------------------------------------------
    # Window behaviour: let clicks through, and get out of the way of scans
    # -----------------------------------------------------------------------
    def _hwnd(self):
        if not IS_WINDOWS:
            return None
        try:
            return pygame.display.get_wm_info().get('window')
        except Exception:
            return None

    def set_click_through(self, enabled: bool) -> bool:
        """WS_EX_TRANSPARENT: the mouse falls through the island to whatever is
        behind it, so the bot can click objects the capsule overlaps (a search
        icon at the top of the screen, for one).  Off again when it is idle, so
        the island stays clickable for you.
        """
        hwnd = self._hwnd()
        if not hwnd:
            return False
        try:
            user32 = ctypes.windll.user32
            GWL_EXSTYLE, WS_EX_TRANSPARENT = -20, 0x00000020
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            new = (style | WS_EX_TRANSPARENT) if enabled else (style & ~WS_EX_TRANSPARENT)
            if new == style:
                return self.click_through == bool(enabled)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, new)
            self.click_through = bool(enabled)
            log_debug(f"Click-through {'ON' if enabled else 'OFF'} - the bot is "
                      f"{'working' if enabled else 'idle'}.")
            return True
        except Exception as e:
            log_debug(f"click-through notice: {e}")
            return False

    def hide_for_scan(self) -> bool:
        """Step aside for one screenshot so the capture shows what the island is
        covering, instead of the island.  Counted, so a grab that raises inside
        a `finally` can never leave the island hidden.  True only for the
        outermost hide (that caller owns the grab).
        """
        hwnd = self._hwnd()
        if not hwnd:
            return False
        nested = self._scan_hidden > 0
        self._scan_hidden += 1
        if nested:
            return False
        try:
            ctypes.windll.user32.ShowWindow(hwnd, 0)       # SW_HIDE
            self._scan_is_hidden = True
            time.sleep(0.015)      # let the compositor drop it before the grab
            return True
        except Exception as e:
            log_debug(f"scan-hide notice: {e}")
            self._scan_hidden -= 1
            return False

    def show_after_scan(self) -> bool:
        """Back on screen after a scan grab (inner calls are no-ops)."""
        if self._scan_hidden <= 0:
            return False
        self._scan_hidden -= 1
        if self._scan_hidden > 0 or not self._scan_is_hidden:
            return False
        self._scan_is_hidden = False
        try:
            ctypes.windll.user32.ShowWindow(self._hwnd(), 4)  # SW_SHOWNOACTIVATE
            return True
        except Exception as e:
            log_debug(f"scan-show notice: {e}")
            return False

    def _process_bot_events(self):
        while not self.msg_queue.empty():
            kind, data = self.msg_queue.get_nowait()
            if kind == "STATE":
                self.state = data
                if data in ("working", "thinking", "acting"):
                    self.target_width = 720.0
                    self.target_height = 294.0   # room for the 120 px chat panel
                    self.target_radius = 30.0
                    if self.emotion != "dizzy":
                        self.emotion = "thinking"
                elif data == "waiting":
                    self.current_action = ""    # the card is over: shortcuts return
                    self.toggle_thoughts(False)  # reasoning folds itself away
                    if not self.input_active and not self.settings_open and self.file_anim_state == "none":
                        self.target_width = 540.0
                        self.target_height = 76.0
                        self.target_radius = 28.0
                elif data == "done":
                    self.current_action = ""
                    self.thoughts_open = False
                    self.emotion = "happy"
                    self.target_width = 540.0
                    self.target_height = 76.0
                elif data == "stalled":
                    self.emotion = "error"
            elif kind == "EMOTION":
                self.emotion = data
            elif kind == "ACTION":
                action, step = data
                self.current_action = self._clean_ui_text(action)
                self.current_step = step
                self.target_width = 720.0
                self.target_height = 294.0   # room for the 120 px chat panel
            elif kind == "THOUGHT":
                self.current_thought = self._clean_ui_text(data)
            elif kind == "SPEECH":
                self.current_speech = self._clean_ui_text(data)
            elif kind == "APP":
                self.current_app = data

    def _handle_input_events(self):
        for event in pygame.event.get():
            if event.type == QUIT:
                self.running = False
            elif event.type == DROPFILE:
                # Real file dropped onto island!
                dropped_path = event.file
                fname = os.path.basename(dropped_path)
                log_debug(f"Real file dropped from Windows: {dropped_path}")
                self.trigger_file_pickup(fname, "Uploaded File")
                self._chat_reset(f"I just dropped the file '{fname}'. Please analyze it.")
                if self.on_user_input:
                    self.on_user_input(f"I just dropped the file '{fname}' from '{dropped_path}'. Please analyze it.")
            elif event.type == KEYDOWN:
                if self.input_active:
                    if event.key == K_RETURN:
                        cmd = self.input_text.strip()
                        if cmd:
                            log_debug(f"User command: {cmd}")
                            self._chat_reset(cmd)    # the panel opens with your words
                            if self.on_user_input:
                                self.on_user_input(cmd)
                            else:
                                self.set_thought(f"Processing '{cmd}'...")
                                self.set_action(f"Execute {cmd}", 1)
                                self.set_speech(f"Running: {cmd}")
                        self.input_text = ""
                        self.input_active = False
                    elif event.key == K_ESCAPE:
                        self.input_active = False
                    elif event.key == K_BACKSPACE:
                        self.input_text = self.input_text[:-1]
                    else:
                        if event.unicode and len(event.unicode) > 0 and ord(event.unicode) >= 32:
                            self.input_text += event.unicode
                else:
                    if event.key == K_RETURN or event.key == K_SLASH:
                        self.input_active = True
                        self.settings_open = False
                        self.target_width = 660.0
                        self.target_height = 120.0
                    elif event.key == K_ESCAPE:
                        if self.settings_open:
                            self.settings_open = False
                            self.target_width = 540.0
                            self.target_height = 76.0
                        elif self.on_interrupt:
                            self.on_interrupt()
                    elif event.key == K_v:
                        self._toggle_voice()
                    elif event.key == K_e:
                        # expanded view: models / history / settings
                        self.toggle_expanded()
                    elif event.key == K_m:
                        # the main app window: mascots, models, settings
                        self.open_control_app()
                    elif event.key == K_t:
                        # Quick test file pickup animation
                        self.trigger_file_pickup("presentation.pptx", "4.8 MB")
                    elif event.key in (K_1, K_2, K_3, K_4, K_5, K_6, K_7, K_8, K_9):
                        emotions_list = ["greeting", "happy", "thinking", "bored", "loving", "exhausted", "sleepy", "dizzy", "error"]
                        idx = event.key - K_1
                        if 0 <= idx < len(emotions_list):
                            self.emotion = emotions_list[idx]
                            log_debug(f"Switched emotion to: {self.emotion}")
                    elif event.key in (K_PAGEUP, K_PAGEDOWN):
                        # the chat panel scrolls even while the island is click-through
                        self._scroll_chat(56 if event.key == K_PAGEUP else -56)
            elif event.type == MOUSEBUTTONDOWN:
                if event.button == 1:
                    self._handle_click(event.pos)
            elif event.type == MOUSEWHEEL:
                if self.expanded_open:
                    # the expanded view scrolls its own lists
                    self.expanded_scroll = max(
                        0, min(self._expanded_scroll_max(),
                               self.expanded_scroll - event.y * 40.0))
                # wheel over the reasoning panel scrolls the reasoning, and
                # only when the reasoning is actually opened
                elif (self.thoughts_open and self._thought_rect is not None
                        and self._thought_rect.collidepoint(self.get_cursor_pos())):
                    self._scroll_thoughts(event.y * 40.0)
                # wheel over the panel scrolls the chat
                elif self._chat_rect.collidepoint(self.get_cursor_pos()):
                    self._scroll_chat(event.y * 40.0)

    def _handle_click(self, pos: Tuple[int, int]):
        mx, my = pos
        ix = int((self.win_w - self.current_width) / 2)
        island_rect = pygame.Rect(ix, 0, int(self.current_width), int(self.current_height))

        if not island_rect.collidepoint(mx, my):
            return

        # 1. Top-Right Settings Gear Icon Click
        gear_rect = pygame.Rect(ix + int(self.current_width) - 34, 14, 24, 24)
        if gear_rect.collidepoint(mx, my):
            self.settings_open = not self.settings_open
            if self.settings_open:
                self.target_width = 720.0
                self.target_height = 250.0
                self.input_active = False
            else:
                self.target_width = 540.0
                self.target_height = 76.0
            return

        # 1b. The expanded view swallows its own clicks first.
        if self.expanded_open:
            self._expanded_hit((mx, my), ix, iw, ih)
            return

        # 2. If Settings Open: Click app items in the picker grid
        if self.settings_open:
            # The Main-app button lives below the picker, so it cannot collide
            if self._main_app_button(ix).collidepoint(mx, my):
                self.open_control_app()
                return
            picker_start_y = 65
            for idx, app in enumerate(ALL_SHORTCUT_APPS):
                col = idx % 4
                row = idx // 4
                bx = ix + 30 + col * 165
                by = picker_start_y + row * 75
                btn_rect = pygame.Rect(bx, by, 150, 60)
                if btn_rect.collidepoint(mx, my):
                    app_id = app["id"]
                    if app_id in self.pinned_apps:
                        if len(self.pinned_apps) > 1:
                            self.pinned_apps.remove(app_id)
                    else:
                        if len(self.pinned_apps) < 4:
                            self.pinned_apps.append(app_id)
                        else:
                            # Replace last
                            self.pinned_apps[-1] = app_id
                    log_debug(f"Updated pinned apps: {self.pinned_apps}")
                    return
            return

        # 2b. The reasoning chip, while the bot is working.  Checked before the
        # avatar because the two sit next to each other in the header.
        if (self.state in ("working", "thinking", "acting") or self.current_action):
            if self.thoughts_open and self._thought_rect is not None \
                    and self._thought_rect.collidepoint(mx, my):
                self.toggle_thoughts(False)
                return
            chip = self._thought_chip_rect(
                ix, int(self.current_width))
            if chip.collidepoint(mx, my):
                self.toggle_thoughts()
                return

        # 3. Avatar click -> poke or cycle emotion
        avatar_rect = pygame.Rect(ix + 16, 14, 52, 48)
        if avatar_rect.collidepoint(mx, my):
            now = time.time()
            if now - self.last_hit_time < 0.5:
                self.avatar_hits += 1
            else:
                self.avatar_hits = 1
            self.last_hit_time = now

            if self.avatar_hits >= 4:
                self.is_dizzy = True
                self.emotion = "dizzy"
                self.dizzy_timer = 3.5
                if self.on_interrupt:
                    self.on_interrupt()
            else:
                # Cycle through emotions playfully
                emotions_cycle = ["happy", "loving", "thinking", "greeting", "bored", "exhausted", "sleepy"]
                cur_idx = emotions_cycle.index(self.emotion) if self.emotion in emotions_cycle else 0
                self.emotion = emotions_cycle[(cur_idx + 1) % len(emotions_cycle)]
                self.bounce_offset = -6.0
            return

        # 4. Shortcut Grid Click (header row when idle, body column when working)
        origin = self._grid_origin()
        if origin:
            grid_start_x, grid_start_y = origin
            for i, app_id in enumerate(self.pinned_apps[:4]):
                app = next((a for a in ALL_SHORTCUT_APPS if a["id"] == app_id), None)
                if not app:
                    continue
                sx = grid_start_x + i * 42
                sy = grid_start_y
                srect = pygame.Rect(sx, sy, 36, 36)
                if srect.collidepoint(mx, my):
                    log_debug(f"Shortcut clicked: {app['name']}")
                    act = app["action"]
                    self._chat_reset(act)
                    if self.on_user_input:
                        self.on_user_input(act)
                    else:
                        self.set_state("acting")
                        self.set_thought(f"Opening {app['name']}...")
                        self.set_action(f"Launch {app['name']}", step=1)
                        self.set_speech(f"Launching {app['name']} for you!")
                    return

        # 5. Background click on idle island -> toggle input bar
        if self.file_anim_state == "none":
            self.input_active = not self.input_active
            if self.input_active:
                self.target_width = 660.0
                self.target_height = 120.0
            else:
                self.target_width = 540.0
                self.target_height = 76.0

    def _sync_window(self):
        """Keep the OS window exactly as large as the panel.

        The GL surface stays 860x576 and the panel is drawn centred inside
        it, so the window is just the top-left slice the panel needs.  The
        two things this buys are the whole point:

          * there is no empty transparent area left for the colour key to
            punch out, so nothing can show up as a black slab - not a
            screenshot tool reading the window surface, not a compositor
            that skips layered windows, not a VM screen grab;
          * a topmost window only covers the island itself instead of a
            1075x720 rectangle hanging over the desktop.

        The panel is always centred in the window (both ease together), so
        the slice never clips it, and get_cursor_pos stays correct because
        it is window-relative and window_pos_x moves with the window.
        """
        hwnd = self._hwnd()
        if not hwnd:
            return
        # Same three terms the capsule background uses.  current and target
        # cover the easing; MODE_HEIGHT covers the panel that is on screen
        # right now - without it a live action card whose capsule has already
        # eased back to idle height would get its chat panel clipped away.
        mode = self._active_mode()
        need_h = self.MODE_HEIGHT[mode]
        w = max(1, int(max(self.current_width, self.target_width)))
        h = max(1, int(max(self.current_height, self.target_height, need_h)))
        if (w, h) == getattr(self, "_last_win", None):
            return
        self._last_win = (w, h)
        self.win_w, self.win_h = w, h
        try:
            screen_w = ctypes.windll.user32.GetSystemMetrics(0)
            x = max(0, (screen_w - w) // 2)
            self.window_pos_x, self.window_pos_y = x, 0
            SWP_SHOWWINDOW, SWP_NOACTIVATE = 0x0040, 0x0010
            if not set_window_pos(hwnd, x, 0, w, h,
                                  SWP_SHOWWINDOW | SWP_NOACTIVATE):
                log_debug("window sync: SetWindowPos refused")
        except Exception as e:
            log_debug(f"window sync notice: {e}")

    def get_cursor_pos(self) -> Tuple[int, int]:
        """Get cursor position relative to the island window, tracking across the entire desktop."""
        if IS_WINDOWS:
            try:
                pt = POINT()
                ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
                return pt.x - self.window_pos_x, pt.y - self.window_pos_y
            except Exception:
                pass
        return pygame.mouse.get_pos()

    def _update(self, dt: float):
        self._update_avatar(dt)
        speed = 14.0 * dt
        self.current_width += (self.target_width - self.current_width) * min(1.0, speed)
        self.current_height += (self.target_height - self.current_height) * min(1.0, speed)
        self.current_radius += (self.target_radius - self.current_radius) * min(1.0, speed)
        # the window follows the capsule, never the other way round
        self._sync_window()
        
        self.cursor_blink = (self.cursor_blink + dt * 2.0) % 1.0
        
        # Soft bounce recovery
        self.bounce_offset += (0.0 - self.bounce_offset) * min(1.0, 10.0 * dt)

        # Emotion greeting timer
        if self.emotion == "greeting":
            self.emotion_timer -= dt
            if self.emotion_timer <= 0.0:
                self.emotion = "idle"

        # Dizzy timer
        if self.is_dizzy:
            self.dizzy_timer -= dt
            if self.dizzy_timer <= 0.0:
                self.is_dizzy = False
                self.emotion = "happy"
                self.avatar_hits = 0

        # File Pickup Animation State Machine
        if self.file_anim_state != "none":
            self._update_file_pickup_animation(dt)

        # -- the startup splash owns the capsule while it plays ----------
        if self._splash_t < self.SPLASH_TIME and not self.expanded_open:
            self._splash_t += dt
            self.target_width = 540.0
            self.target_height = 180.0
            self.target_radius = 30.0
        elif self._splash_t >= self.SPLASH_TIME and self._splash_seen < self.SPLASH_TIME:
            # the splash grew the capsule on its way in - shrink it back,
            # or the island is left stranded at splash height for good.
            self._splash_seen = self._splash_t
            if not self.expanded_open:
                if self.settings_open:
                    self.target_width, self.target_height = 720.0, 250.0
                    self.target_radius = 24.0
                elif self.state in ("working", "thinking", "acting") or self.current_action:
                    # a turn started while the splash was still playing.
                    # Collapsing to idle height here drew the action card -
                    # which needs the tall capsule - inside a short one.
                    self.target_width = 720.0
                    self.target_height = self.MODE_HEIGHT["action"]
                    self.target_radius = 30.0
                else:
                    self.target_width = 540.0
                    self.target_height = 76.0
                    self.target_radius = 28.0

        # -- expanded view: eased open/close and the tab cross-slide -----
        want_ea = 1.0 if self.expanded_open else 0.0
        self._expanded_anim += (want_ea - self._expanded_anim) * min(1.0, 9.0 * dt)
        self._tab_anim += (1.0 - self._tab_anim) * min(1.0, 11.0 * dt)

    def _update_file_pickup_animation(self, dt: float):
        self.file_anim_progress += dt
        t = self.file_anim_progress

        if self.file_anim_state == "incoming":
            # File is floating downward toward letterbox slot
            if t > 0.8:
                self.file_anim_state = "slot_open"
        elif self.file_anim_state == "slot_open":
            # Bot opens slot on top of head, file drops inside
            if t > 1.6:
                self.file_anim_state = "intake"
        elif self.file_anim_state == "intake":
            # Slot snaps shut, bot prepares to leap
            if t > 2.2:
                self.file_anim_state = "jump_left"
        elif self.file_anim_state == "jump_left":
            # Bot jumps to far left
            if t > 3.0:
                self.file_anim_state = "progress_bar"
        elif self.file_anim_state == "progress_bar":
            # Bot glides left to right as progress bar
            # Add particles to trail
            if len(self.file_trail_particles) < 60:
                self.file_trail_particles.append({
                    "x": 0, "y": 0, "alpha": 1.0, "size": 3.0, "color": (80, 210, 255)
                })
            if t > 5.5:
                self.file_anim_state = "completed"
        elif self.file_anim_state == "completed":
            if t > 6.8:
                self.file_anim_state = "none"
                self.target_width = 540.0
                self.target_height = 76.0
                self.emotion = "happy"
                self.set_thought(f"Extracted and ingested {self.file_anim_name}")
                self.set_action(f"Visual parse {self.file_anim_name}", step=1)
                self.set_speech(f"I've read {self.file_anim_name}. Ready for questions!")

    def _render(self):
        current_time = time.time() - self.start_time

        # 1. Clear background
        if IS_WINDOWS:
            if self.use_moderngl:
                self.ctx.clear(1.0/255.0, 1.0/255.0, 1.0/255.0, 0.0)
            # (1,1,1) is the colorkey, so it is a hole either way.  It must
            # be alpha 0 when per-pixel alpha is live: an opaque backdrop
            # is not punched out by a colorkey, it would turn the whole
            # window into one translucent slab instead of a capsule.
            self.ui_surface.fill((1, 1, 1, 0) if self._per_pixel_alpha
                                 else (1, 1, 1, 255))
        else:
            if self.use_moderngl:
                self.ctx.clear(0.0, 0.0, 0.0, 0.0)
            self.ui_surface.fill((0, 0, 0, 0))

        # 2. ModernGL Starfield Shader inside island capsule
        if self.use_moderngl:
            island_left = (self.win_w - self.current_width) / 2.0
            # The capsule is drawn at y=0, so its bottom edge is its own
            # height - not screen_height - height, which describes a capsule
            # pinned to the bottom of the window.
            island_bottom = float(self.current_height)
            try:
                self.prog_starfield['u_resolution'].value = (self.screen_width, self.screen_height)
                self.prog_starfield['u_time'].value = float(current_time)
                self.prog_starfield['u_island_bounds'].value = (
                    float(island_left), float(island_bottom),
                    float(self.current_width), float(self.current_height)
                )
                self.ctx.enable(moderngl.BLEND)
                self.vao_starfield.render(moderngl.TRIANGLES, vertices=3)
            except Exception as e:
                pass

        # 3. Draw Island Graphics onto UI surface
        self._draw_island()

        # 4. Composite to Screen
        if self.use_moderngl:
            raw_data = pygame.image.tostring(self.ui_surface, 'RGBA')
            self.ui_texture.write(raw_data)
            self.ui_texture.use(location=0)
            self.prog_ui['u_texture'].value = 0
            
            self.ctx.enable(moderngl.BLEND)
            self.ctx.blend_func = (moderngl.SRC_ALPHA, moderngl.ONE_MINUS_SRC_ALPHA)
            self.vao_ui.render(moderngl.TRIANGLES, vertices=3)
            pygame.display.flip()
        else:
            self.surface.blit(self.ui_surface, (0, 0))
            pygame.display.flip()

        # per-pixel alpha has no automatic repaint: re-upload the frame
        if self._per_pixel_alpha and not self._alpha_failed:
            try:
                hwnd = self._hwnd()
                if hwnd and not self._blit_per_pixel_alpha(hwnd):
                    self._alpha_failed = True
                    self._use_colorkey(hwnd)
            except Exception:
                self._alpha_failed = True

    def _draw_island(self):
        # Centre the capsule horizontally inside the window; the window is
        # itself centred on the screen, so the capsule lands on the screen
        # centre in every mode.  iy stays 0 - a floating island hangs from
        # the top of the screen.
        ix = int((self.win_w - self.current_width) / 2)
        iy = 0
        iw = int(self.current_width)
        ih = int(self.current_height)
        ir = int(self.current_radius)
        
        # The panel on screen decides how tall the capsule must be.  Sizing
        # the background from current_height alone is what let the action
        # card - which lays itself out for a 294 px capsule - spill its
        # action box and chat panel past the capsule onto the transparent
        # part of the window, as opaque dark rectangles.
        mode = self._active_mode()
        island_rect = pygame.Rect(ix, iy, iw,
                                  int(max(self.current_height,
                                          self.MODE_HEIGHT[mode])))

        # Dynamic State Border Glow
        border_col = (50, 215, 120, 240) if self.state == "waiting" else (70, 150, 255, 240)
        if self.emotion in ("dizzy", "exhausted", "error") or self.state == "stalled":
            border_col = (255, 95, 85, 240)
        elif self.emotion == "loving":
            border_col = (255, 110, 180, 240)
        elif self.file_anim_state != "none":
            border_col = (60, 220, 255, 240)

        # Draw Capsule Background
        pygame.draw.rect(self.ui_surface, (12, 13, 17, 255), island_rect, border_radius=ir)
        pygame.draw.rect(self.ui_surface, border_col, island_rect, 1, border_radius=ir)

        # Hardware Camera Bezel Notch
        cam_x = self.win_w // 2
        pygame.draw.rect(self.ui_surface, (5, 5, 8), (cam_x - 24, 2, 48, 8), border_radius=4)
        pygame.draw.circle(self.ui_surface, (25, 30, 48), (cam_x, 6), 3)

        # Top-Right Settings Gear Icon (⚙️)
        gear_x = ix + iw - 30
        gear_y = 16
        gear_col = (110, 120, 140) if not self.settings_open else (70, 160, 255)
        # Draw clean vector gear icon
        pygame.draw.circle(self.ui_surface, gear_col, (gear_x, gear_y), 7, 2)
        for angle in range(0, 360, 45):
            rad = math.radians(angle)
            gx = gear_x + math.cos(rad) * 8.5
            gy = gear_y + math.sin(rad) * 8.5
            pygame.draw.circle(self.ui_surface, gear_col, (int(gx), int(gy)), 2)

        # Mode Routing
        if mode == "splash":
            self._draw_splash(ix, iw, ih)
        elif mode == "expanded":
            # Keep drawing the panel on the way OUT too.  Routing on
            # expanded_open alone would blank the content on the frame you
            # pressed E, and the eased _expanded_anim would never be seen.
            self._draw_expanded_mode(ix, iw, ih)
        elif mode == "file":
            self._draw_file_pickup_mode(ix, iw, ih)
        elif mode == "settings":
            self._draw_settings_mode(ix, iw, ih)
        elif mode == "input":
            self._draw_input_mode(ix, iw, ih)
        elif mode in ("action", "action_thoughts"):
            self._draw_action_mode(ix, iw, ih)
        else:
            self._draw_idle_mode(ix, iw, ih)

    def _active_mode(self) -> str:
        """Which MODE_HEIGHT entry describes what is on screen right now.

        One place, so the capsule background and the panel that gets drawn
        inside it can never disagree about how tall the island is.
        """
        if self._splash_t < self.SPLASH_TIME and not self.expanded_open:
            return "splash"
        if self.expanded_open or self._expanded_anim > 0.002:
            return "expanded"
        if self.file_anim_state != "none":
            return "file"
        if self.settings_open:
            return "settings"
        if self.input_active:
            return "input"
        if self.state in ("working", "thinking", "acting") or self.current_action:
            return "action_thoughts" if self.thoughts_open else "action"
        return "idle"

    # -----------------------------------------------------------------------
    # Avatar Rendering & Emotional Expressions
    # -----------------------------------------------------------------------
    # -----------------------------------------------------------------------
    # The avatar theatre: everything that makes the bot look alive
    # -----------------------------------------------------------------------
    #: Personality presets.  Each one retunes the whole animation, the way a
    #: Taby personality preset changes how the face moves while it idles.
    _PERSONALITIES = {
        "normal":     dict(blink=4.2, blink_jitter=2.2, breath=1.5, sway=0.30,
                           lean=0.55, arm=0.35, eye=1.00, bounce=1.0,
                           head=(245, 248, 252), accent=(90, 170, 255)),
        "butler":     dict(blink=3.0, blink_jitter=1.0, breath=1.0, sway=0.16,
                           lean=0.30, arm=0.22, eye=0.94, bounce=0.6,
                           head=(242, 244, 248), accent=(200, 170, 110)),
        "brother":    dict(blink=4.8, blink_jitter=2.6, breath=1.9, sway=0.42,
                           lean=0.70, arm=0.45, eye=1.04, bounce=1.25,
                           head=(243, 248, 246), accent=(90, 205, 170)),
        "anime_girl": dict(blink=6.0, blink_jitter=3.4, breath=2.4, sway=0.55,
                           lean=0.85, arm=0.60, eye=1.18, bounce=1.5,
                           head=(255, 240, 246), accent=(255, 120, 175)),
        "italian":    dict(blink=4.6, blink_jitter=2.4, breath=1.7, sway=0.50,
                           lean=0.75, arm=0.55, eye=1.06, bounce=1.3,
                           head=(250, 244, 236), accent=(225, 120, 60)),
        "russian":    dict(blink=3.4, blink_jitter=1.4, breath=1.2, sway=0.22,
                           lean=0.35, arm=0.28, eye=0.92, bounce=0.7,
                           head=(238, 242, 248), accent=(120, 150, 215)),
    }

    def set_personality(self, name: str):
        """Switch the avatar personality.  Unknown names are ignored.

        Only the main app window calls this now.  The island itself no longer
        offers a picker: it renders whatever mascot it was last handed.
        """
        key = str(name or "").strip().lower().replace(" ", "_")
        if key in self._PERSONALITIES:
            self.personality = key
            self._preset_dirty = True
            return True
        return False

    # -----------------------------------------------------------------------
    # Mascots
    # -----------------------------------------------------------------------
    #: Outlines _draw_avatar can draw.  "blob" is the original soft rounded
    #: capsule; "emoji" hands the whole head over to a glyph.
    _MASCOT_SHAPES = ("blob", "circle", "hex", "diamond", "triangle",
                      "star", "square", "emoji")

    @staticmethod
    def _coerce_rgb(value, fallback):
        try:
            seq = list(value)
            r, g, b = int(seq[0]), int(seq[1]), int(seq[2])
        except Exception:
            return fallback
        cl = lambda c: max(0, min(255, c))          # noqa: E731
        return (cl(r), cl(g), cl(b))

    def _clean_mascot(self, raw, index=0):
        """One mascot dict, every field present and in range.

        The store (phoenix_store) already normalises; this is the island's
        own copy of that contract, so a caller that does not go through the
        store still cannot hand us a mascot that breaks _draw_avatar.
        """
        raw = raw if isinstance(raw, dict) else {}
        key = str(raw.get("key") or "").strip()[:32] or ("mascot%d" % (index + 1))
        personality = str(raw.get("personality") or "normal").strip().lower()
        if personality not in self._PERSONALITIES:
            personality = "normal"
        shape = str(raw.get("shape") or "blob").strip().lower()
        if shape not in self._MASCOT_SHAPES:
            shape = "blob"
        try:
            size = float(raw.get("size", 1.0))
        except Exception:
            size = 1.0
        emoji = raw.get("emoji")
        return {
            "key": key,
            "name": str(raw.get("name") or key)[:24],
            "personality": personality,
            "head": self._coerce_rgb(raw.get("head"), (245, 248, 252)),
            "accent": self._coerce_rgb(raw.get("accent"), (90, 170, 255)),
            "shape": shape,
            "emoji": emoji if isinstance(emoji, str) and emoji else "",
            "size": max(0.6, min(1.8, size)),
        }

    def set_mascots(self, mascots, active=None):
        """Take the whole gallery from the main app window.

        Returns the key that ended up on screen.  An empty or unusable list
        leaves whatever was there before rather than blanking the island.
        """
        built = {}
        for i, raw in enumerate(list(mascots or [])):
            m = self._clean_mascot(raw, i)
            if m["key"] not in built:
                built[m["key"]] = m
        if built:
            self.mascots = built
        want = str(active or "").strip()
        if want in self.mascots:
            self.mascot_key = want
        elif not self.mascot_key or self.mascot_key not in self.mascots:
            self.mascot_key = next(iter(self.mascots), self.mascot_key)
        if self.mascot_key:
            self.set_personality(self.mascots[self.mascot_key]["personality"])
        self._preset_dirty = True
        return self.mascot_key

    def set_mascot(self, key):
        """Put a different mascot on screen, if we know it."""
        key = str(key or "").strip()
        if key in self.mascots:
            self.mascot_key = key
            self.set_personality(self.mascots[key]["personality"])
            self._preset_dirty = True
            return True
        return False

    def set_mascot_appearance(self, **fields):
        """Live-tweak the on-screen mascot: head=, accent=, shape=, size=,
        emoji=, name=.  Only the fields handed in are touched, so the window
        can push one slider without clobbering the rest."""
        m = self.mascots.get(self.mascot_key)
        if not m:
            return False
        for name in ("head", "accent"):
            if name in fields:
                m[name] = self._coerce_rgb(fields[name], m[name])
        if "shape" in fields:
            shape = str(fields["shape"] or "blob").strip().lower()
            m["shape"] = shape if shape in self._MASCOT_SHAPES else m["shape"]
        if "size" in fields:
            try:
                m["size"] = max(0.6, min(1.8, float(fields["size"])))
            except Exception:
                pass
        if "emoji" in fields:
            emoji = fields["emoji"]
            m["emoji"] = emoji if isinstance(emoji, str) else ""
        if "name" in fields:
            m["name"] = str(fields["name"])[:24]
        if "personality" in fields:
            # A mascot carries its own personality; changing it switches the
            # animation preset unless the user pinned a different one.
            self.set_personality(fields["personality"])
        self._preset_dirty = True
        return True

    def mascot(self):
        """The mascot currently on screen, or an empty dict."""
        return self.mascots.get(self.mascot_key) or {}

    def mascot_size(self) -> float:
        """The on-screen scale multiplier, so _draw_avatar can fold it in."""
        try:
            return float(self.mascot().get("size", 1.0))
        except Exception:
            return 1.0

    def mascot_label(self) -> str:
        return str(self.mascot().get("name") or self._PERSONALITY_LABELS.get(
            self.personality, "Normal"))

    def open_control_app(self):
        """Ask the launcher for the main app window.  A no-op when the island
        is running on its own, so nothing here can ever crash the loop."""
        try:
            if callable(self.on_open_control):
                self.on_open_control()
                return True
        except Exception as e:
            log_debug(f"control app unavailable: {e}")
        return False

    def skip_splash(self):
        """Jump past the startup splash.

        The splash owns the capsule for its first few seconds, so anything
        that wants to draw a mode straight away (renders, tests) has to say
        it has already played.
        """
        self._splash_t = self.SPLASH_TIME
        self._splash_seen = self.SPLASH_TIME

    def _p(self):
        """The live preset: personality animation + the mascot's colours.

        _p() is read a dozen times a frame, so the merge is cached and only
        rebuilt when the personality or the mascot actually changes.
        """
        if getattr(self, "_preset_cache", None) is not None and not getattr(
                self, "_preset_dirty", False):
            return self._preset_cache
        base = self._PERSONALITIES.get(self.personality,
                                       self._PERSONALITIES["normal"])
        m = self.mascot()
        merged = dict(base)
        if m:
            merged["head"] = m["head"]
            merged["accent"] = m["accent"]
        self._preset_cache = merged
        self._preset_dirty = False
        return merged

    @staticmethod
    def _lerp_col(a, b, k):
        """Blend two RGB colours: k=0 gives a, k=1 gives b.

        The island surface has no alpha channel, so anything that ought to
        look transparent is drawn as a colour mixed toward the panel
        background rather than as an alpha-blended shape.
        """
        k = max(0.0, min(1.0, float(k)))
        return (int(a[0] + (b[0] - a[0]) * k),
                int(a[1] + (b[1] - a[1]) * k),
                int(a[2] + (b[2] - a[2]) * k))

    #: How long the startup splash plays before the island takes over.
    SPLASH_TIME = 2.6

    #: How tall the capsule has to be for each panel, in one place.  The
    #: state handlers ease the capsule toward these numbers and
    #: _draw_island uses them to make the capsule background tall enough to
    #: sit behind everything the panel draws.  When the two disagree the
    #: panel paints opaque boxes onto the transparent part of the window -
    #: that is the black band behind the island.
    MODE_HEIGHT = {
        "idle": 76.0,
        "splash": 180.0,
        "expanded": 560.0,
        "file": 190.0,
        "settings": 250.0,
        "input": 120.0,
        "action": 294.0,
        # the reasoning panel, unfolded above the chat panel
        "action_thoughts": 470.0,
    }

    def is_speaking(self) -> bool:
        return time.time() < self._speaking_until

    def _update_avatar(self, dt: float):
        """Advance blinking, gaze, breathing and speech energy.

        Called every frame from _update, so the avatar is frame-rate
        independent and keeps moving even when nothing else changes.
        """
        p = self._p()
        self.anim_t += dt

        # -- blink: a real cycle, with the occasional double blink ---------
        self._blink_phase = min(1.0, self._blink_phase + dt * 14.0)
        self._blink_t -= dt
        if self._blink_t <= 0.0:
            if self._blink_phase < 1.0:          # finishing the closed part
                self._blink_phase = 0.0
                if self._blink_queued > 0:
                    self._blink_queued -= 1
                    self._blink_t = 0.09           # straight back down
                else:
                    self._blink_t = p["blink"] + self._blink_jitter()
                self._wink_eye = -1
            else:
                self._blink_phase = 0.0            # start closing
                self._blink_t = 0.11
                # every so often one eye stays open while the other shuts.
                # -1 is the common case: most blinks are just blinks.
                self._wink_eye = -1
                if random.random() < 0.30:
                    self._wink_eye = random.choice((0, 1))

        # -- gaze: hold, then flick to a new spot (a micro-saccade) ---------
        self._gaze_hold -= dt
        if self._gaze_hold <= 0.0:
            self._gaze_hold = 1.4 + (self.anim_t % 1.7)
            if self._blink_queued and self.anim_t % 5 < 2:
                self._blink_queued = 2             # a double blink now and then
            self._gaze_target = [random.uniform(-2.2, 2.2),
                                 random.uniform(-1.4, 1.6)]
        for i in (0, 1):
            self._gaze[i] += (self._gaze_target[i] - self._gaze[i]) * min(1.0, 7.0 * dt)

        # -- speech energy: eases in when it talks, out when it stops -----
        want = 1.0 if self.is_speaking() else 0.0
        self._speech_env += (want - self._speech_env) * min(1.0, 6.0 * dt)

        # -- energy: thinking, speaking and happiness all raise it -------
        if self.is_speaking():
            want_e = 1.0
        elif self.emotion in ("thinking", "loving", "greeting", "happy"):
            want_e = 0.75
        elif self.emotion in ("error", "dizzy"):
            want_e = 0.85
        else:
            want_e = 0.25
        self._energy += (want_e - self._energy) * min(1.0, 3.5 * dt)

        # -- the antenna is a real spring, so it overshoots ---------------
        wobble = math.sin(self.anim_t * 1.7) * 0.5
        if self.emotion in ("thinking", "loving", "greeting", "happy"):
            wobble += math.sin(self.anim_t * 3.3) * 0.4
        self._antenna_target = wobble
        angle, vel = self._antenna
        vel += ((self._antenna_target - angle) * 34.0 - vel * 5.2) * dt
        angle += vel * dt
        self._antenna = (angle, vel)

        # -- body: breathing and a slow lean, so it is never statue-still --
        # a slow figure-eight, so the head drifts instead of sitting still
        drift = math.sin(self.anim_t * 0.45) * 2.2 * p["bounce"]
        self._body_lean = (math.sin(self.anim_t * 0.7) * p["lean"]
                           + drift * 0.35)
        self._arm_wave = (math.sin(self.anim_t * 3.1) * 0.5 + 0.5) * p["arm"]

        # -- breathing: the head scales very slightly, in and out ---------
        self._breathe = math.sin(self.anim_t * p["breath"] * 2.0)

        # -- activity states: idle / working / done, cross-faded ---------
        self.activity = ("working" if self.state in
                         ("working", "thinking", "acting", "stalled")
                         else ("done" if self.state == "done" else "idle"))
        for key in self._activity_mix:
            target = 1.0 if key == self.activity else 0.0
            self._activity_mix[key] += ((target - self._activity_mix[key])
                                        * min(1.0, 2.4 * dt))
        self._done_flash = max(0.0, self._done_flash - dt * 0.9)

        # -- a shrug now and then, so idle is never statue-still ----------
        self._fidget_t -= dt
        if self._fidget_t <= 0.0:
            self._fidget_t = random.uniform(4.0, 11.0)
            self._fidget = 1.0
            for i in (0, 1):
                self._ear_tw[i] = 1.0        # the ears flick with it
        self._fidget *= max(0.0, 1.0 - 3.0 * dt)
        for i in (0, 1):
            self._ear_tw[i] *= max(0.0, 1.0 - 2.6 * dt)

        # -- tilt and squish: real springs, so they overshoot ------------
        pose = self._ACTIVITY_POSE["idle"]
        for key, weight in self._activity_mix.items():
            p2 = self._ACTIVITY_POSE[key]
            pose = {k: pose[k] + (p2[k] - pose[k]) * weight for k in pose}

        want_tilt = (math.sin(self.anim_t * 0.55) * 0.20
                     + pose["tilt"]
                     + self._fidget * 1.5
                     - self._nod * 0.9
                     + self._nod * 0.9 * math.sin(self.anim_t * 9.0))
        if self.emotion == "thinking":
            want_tilt += 0.22
        elif self.emotion == "loving":
            want_tilt += math.sin(self.anim_t * 2.1) * 0.28
        elif self.emotion in ("error", "dizzy"):
            want_tilt += math.sin(self.anim_t * 26.0) * 0.11    # a shiver
        want_tilt *= 1.0 + self._pop * 0.5

        want_squish = (self._breathe * 0.055
                       + self._speech_env * 0.10
                       + self._activity_mix["working"] * 0.045
                       + self._pop * 0.16
                       - self._fidget * 0.10)

        self._tilt_v += ((want_tilt - self._tilt) * 30.0 - self._tilt_v * 6.0) * dt
        self._tilt += self._tilt_v * dt
        self._squish_v += ((want_squish - self._squish) * 26.0
                           - self._squish_v * 5.5) * dt
        self._squish += self._squish_v * dt

        # -- nod and surprise pop decay back to rest ---------------------
        self._nod_v += ((0.0 - self._nod) * 26.0 - self._nod_v * 7.0) * dt
        self._nod += self._nod_v * dt
        self._pop = max(0.0, self._pop - dt * 1.8)

    def _blink_jitter(self) -> float:
        p = self._p()
        return random.uniform(0.4, 1.6) * p["blink_jitter"]

    def _eye_openness(self, eye: int = 0) -> float:
        """1.0 open, 0.0 shut.  Squints to 0.55 when it is thinking.

        A winking eye re-opens before the other, which is what turns an
        ordinary blink into a wink.
        """
        if self.emotion in ("sleepy", "exhausted"):
            return 0.0
        base = self._blink_phase
        if eye == self._wink_eye:
            base = min(1.0, base * 2.6)
        if self.emotion == "bored":
            base = min(base, 0.45)
        if self.emotion == "thinking":
            base = min(base, 0.75)
        return max(0.0, min(1.0, base))

    def _deform_head(self, cx: int, cy: int, w: int, h: int):
        """The head's corners after tilt and squish.

        Tilt shears the top of the head sideways, so it leans; squish
        widens and shortens it while holding the area roughly constant,
        which is what makes it read as a soft body being deformed rather
        than an image being resized.
        """
        sq = max(-0.55, min(0.55, self._squish))
        ww = w * (1.0 + sq * 0.55)
        hh = max(4.0, h * (1.0 - sq * 0.55))
        lean = self._tilt * max(w, h) * 0.45
        pts = []
        for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
            px = cx + sx * ww / 2.0
            py = cy + sy * hh / 2.0
            px += lean * (-sy)
            pts.append((int(px), int(py)))
        return pts, int(ww), int(hh)

    def _draw_activity_ring(self, cx: int, cy: int, s: float):
        """A ring around the head that says what it is doing.

        Working sweeps a rotating arc; done leaves a green ring that fades.
        """
        work = self._activity_mix["working"]
        done = self._activity_mix["done"]
        if work < 0.03 and done < 0.03 and self._done_flash < 0.03:
            return
        bg = (12, 13, 17)
        accent = self._p()["accent"]
        r = max(6, int(25 * s))
        t = self.anim_t
        if done > 0.03 or self._done_flash > 0.03:
            pulse = 0.55 + 0.45 * math.sin(t * 4.0)
            k = max(done * pulse, self._done_flash * pulse)
            pygame.draw.circle(self.ui_surface,
                               self._lerp_col(bg, (70, 225, 140), k),
                               (cx, cy), r, max(1, int(1.5 * s)))
        if work > 0.03:
            speed = 2.4 + 2.2 * work
            for k in (work * 0.95, work * 0.45):
                pygame.draw.arc(
                    self.ui_surface,
                    self._lerp_col(bg, accent, k),
                    pygame.Rect(cx - r, cy - r, r * 2, r * 2),
                    (t * speed) % (2 * math.pi),
                    (t * speed) % (2 * math.pi) + 1.3,
                    max(1, int(1.7 * s)))

    def _gaze_offset(self, cx: int, cy: int):
        """Eye offset: micro-saccades, plus cursor tracking while idle."""
        gx, gy = self._gaze
        if self.emotion in ("idle", "greeting", "happy", "bored"):
            try:
                mx, my = self.get_cursor_pos()
                dx, dy = mx - cx, my - cy
                dist = math.hypot(dx, dy)
                if dist > 0.001:
                    reach = min(3.2, dist * 0.02)
                    gx += (dx / dist) * reach
                    gy += (dy / dist) * reach
            except Exception:
                pass
        return gx, gy

    def _mouth_openness(self) -> float:
        """0 closed.  While speaking it opens and closes like syllables."""
        if self._speech_env <= 0.02:
            return 0.0
        wobble = (math.sin(self.anim_t * 17.0) * 0.5 +
                  math.sin(self.anim_t * 7.3) * 0.5) * 0.5 + 0.5
        return min(1.0, self._speech_env * (0.45 + wobble * 0.55))

    # -- the body ---------------------------------------------------------
    def _draw_avatar_body(self, cx: int, cy: int, s: float, stage: pygame.Rect):
        """A small body that moves inside a FIXED area.

        The head never leaves the stage - it sways, breathes and leans within
        it, which is what stops the avatar looking pinned to the panel.
        """
        p = self._p()
        t = self.anim_t
        breath = math.sin(t * p["breath"] * 2.0) * 1.6 * s
        sway = math.sin(t * 0.9) * p["sway"] * 3.0 * s
        lean = self._body_lean * s

        head_w = int(36 * s)
        head_h = int(32 * s)
        body_top = cy + head_h // 2 - 2
        body_cx = int(cx + sway)
        shoulder = int(body_cx + lean * 0.4)

        # torso
        torso = pygame.Rect(body_cx - int(13 * s), body_top,
                            int(26 * s), int(20 * s + breath))
        pygame.draw.rect(self.ui_surface, p["head"], torso,
                         border_radius=max(2, int(7 * s)))
        pygame.draw.rect(self.ui_surface, p["accent"], torso, 1,
                         border_radius=max(2, int(7 * s)))
        # a collar so the body reads as a body, not a block
        pygame.draw.line(self.ui_surface, p["accent"],
                         (body_cx - int(5 * s), body_top + 1),
                         (body_cx + int(5 * s), body_top + 1),
                         max(1, int(2 * s)))

        # arms: one idles, one lifts when greeting or speaking
        arm_col = p["head"]
        lift = self._arm_wave
        if self.emotion == "greeting":
            lift = 1.0
        if self._speech_env > 0.2:
            lift = max(lift, self._speech_env)
        for side in (-1, 1):
            ax = body_cx + side * int(14 * s)
            ay = body_top + int(6 * s)
            swing = lift if side > 0 else 0.15
            tip_y = int(ay - swing * 9 * s)
            tip_x = int(ax + side * swing * 4 * s)
            pygame.draw.line(self.ui_surface, arm_col, (ax, ay),
                             (tip_x, tip_y), max(2, int(4 * s)))
            pygame.draw.circle(self.ui_surface, arm_col,
                               (tip_x, tip_y), max(2, int(3 * s)))

        # A spring antenna on top of the head - it overshoots and wobbles.
        ang, _vel = self._antenna
        base_x = int(cx + sway)
        base_y = cy - int(head_h * 0.5)
        tip_x = int(base_x + math.sin(ang) * 11 * s)
        tip_y = int(base_y - (10 + math.cos(ang) * 3) * s)
        pygame.draw.line(self.ui_surface, p["accent"],
                         (base_x, base_y), (tip_x, tip_y),
                         max(1, int(2 * s)))
        pygame.draw.circle(self.ui_surface, p["accent"], (tip_x, tip_y),
                           max(1, int(2.5 * s)))

        # the stage floor keeps the motion visibly inside a fixed area
        if stage is not None:
            floor = pygame.Rect(stage.x + 4, stage.bottom - 2,
                                stage.width - 8, 2)
            pygame.draw.rect(self.ui_surface, (200, 208, 220), floor,
                             border_radius=1)

    # -- speech reaction: a small waveform beside the head ----------------
    def _draw_speech_wave(self, cx: int, cy: int, s: float):
        if self._speech_env <= 0.05:
            return
        p = self._p()
        base_x = cx + int(22 * s)
        base_y = cy + int(6 * s)
        for i in range(4):
            amp = (math.sin(self.anim_t * 13.0 + i * 1.1) * 0.5 + 0.5)
            hgt = max(1, int(amp * 6 * s * self._speech_env))
            bx = base_x + i * int(3 * s)
            pygame.draw.rect(self.ui_surface, p["accent"],
                             (bx, base_y - hgt, max(1, int(2 * s)), hgt * 2),
                             border_radius=1)

    def _emoji_font(self, size):
        """Segoe UI Emoji, cached per size.

        A TTF load is not free and the avatar asks every frame, so the fonts
        are kept.  Verified to actually rasterise colour-emoji code points
        through SDL_ttf on this machine; if the font is missing anywhere else
        we fall back to the island's own font and the glyph still shows.
        """
        size = max(6, int(size))
        cache = getattr(self, "_emoji_fonts", None)
        if cache is None:
            cache = {}
            self._emoji_fonts = cache
        if size not in cache:
            font = None
            try:
                path = os.path.join(
                    os.environ.get("WINDIR") or "C:\\Windows",
                    "Fonts", "seguiemj.ttf")
                if os.path.isfile(path):
                    font = pygame.font.Font(path, size)
            except Exception:
                font = None
            if font is None:
                try:
                    font = pygame.font.SysFont("segoeuiemoji", size)
                except Exception:
                    font = self.font_regular
            cache[size] = font
        return cache[size]

    def _draw_emoji_head(self, cx: int, cy: int, glyph: str, colour, scale: float):
        """A mascot whose shape is "emoji": the glyph IS the head.

        No capsule, no sheen, no ears and no eye expressions - they would all
        fight with a face that is already drawn.
        """
        try:
            font = self._emoji_font(int(34 * scale))
            surf = font.render(glyph, True, tuple(colour))
            surf = pygame.transform.smoothscale(
                surf, (max(6, int(surf.get_width() * scale * 0.9)),
                       max(6, int(surf.get_height() * scale * 0.9))))
            rect = surf.get_rect(center=(int(cx), int(cy)))
            self.ui_surface.blit(surf, rect)
        except Exception as exc:
            log_debug(f"emoji head failed ({glyph!r}): {exc}")

    #: The head is rendered this many times oversize and smoothed back down.
    #: pygame offers no anti-aliased polygon, and at 1:1 a 36px head shows
    #: every step along its edge - which is most of why the face read as a
    #: cut-out sticker rather than something soft.
    HEAD_AA = 4

    def _corner_radius(self, shape: str, hw: int, hh: int) -> float:
        """How much to round the head's corners, per shape.

        The blob is the soft one and gets the most.  The geometric mascots
        keep their identity and only lose the razor edge.
        """
        m = min(float(hw), float(hh))
        shape = str(shape or "blob").strip().lower()
        if shape in ("blob", "emoji"):
            k = 0.30
        elif shape == "circle":
            k = 0.16
        else:
            k = 0.13
        return max(1.0, m * k)

    def _round_poly(self, pts, radius: float, segs: int = 5):
        """Turn a polygon with sharp corners into one with rounded ones.

        Each vertex becomes a short quadratic Bezier running from partway
        down the incoming edge to partway along the outgoing one.  Rounding
        the points themselves - rather than redrawing a rounded box - is
        what keeps the tilt and the squish, so the head still leans exactly
        the way it was deformed to.
        """
        pts = [(float(x), float(y)) for x, y in pts]
        if len(pts) < 3 or radius <= 0.5:
            return [(int(x), int(y)) for x, y in pts]
        out = []
        n = len(pts)
        for i in range(n):
            px, py = pts[i - 1]
            cx, cy = pts[i]
            nx, ny = pts[(i + 1) % n]
            l1 = math.hypot(cx - px, cy - py) or 1.0
            l2 = math.hypot(nx - cx, ny - cy) or 1.0
            # never eat more than half of either edge, or the corner collapses
            r = min(radius, l1 * 0.45, l2 * 0.45)
            ax, ay = cx + (px - cx) / l1 * r, cy + (py - cy) / l1 * r
            bx, by = cx + (nx - cx) / l2 * r, cy + (ny - cy) / l2 * r
            out.append((ax, ay))
            for s in range(1, segs):
                t = s / segs
                u = 1.0 - t
                out.append((u * u * ax + 2.0 * u * t * cx + t * t * bx,
                            u * u * ay + 2.0 * u * t * cy + t * t * by))
            out.append((bx, by))
        return [(int(x), int(y)) for x, y in out]

    def _draw_head_aa(self, poly, fill_col=None, line_col=None):
        """Fill, or outline, the head outline - with anti-aliased edges.

        Fill and outline stay two separate calls so the outline can still be
        laid over the ears, exactly where it was drawn before.
        """
        if fill_col is None and line_col is None:
            return
        xs = [p[0] for p in poly]
        ys = [p[1] for p in poly]
        pad = 2
        x0, y0 = int(min(xs)) - pad, int(min(ys)) - pad
        w = int(max(xs)) - x0 + pad + 1
        h = int(max(ys)) - y0 + pad + 1
        if w <= 0 or h <= 0:
            return
        k = self.HEAD_AA
        big = pygame.Surface((w * k, h * k), pygame.SRCALPHA)
        big.fill((0, 0, 0, 0))
        sp = [((p[0] - x0) * k, (p[1] - y0) * k) for p in poly]
        if fill_col is not None:
            pygame.draw.polygon(big, fill_col, sp)
        if line_col is not None:
            pygame.draw.polygon(big, line_col, sp,
                                max(1, int(round(k * 0.75))))
        self.ui_surface.blit(pygame.transform.smoothscale(big, (w, h)),
                             (x0, y0))

    def _mascot_shape_points(self, shape, cx, cy, hw, hh):
        """Outline for a mascot shape, or None when it is the soft blob.

        The blob keeps its four deformed corners and is rounded by
        _round_poly; every other shape is a plain polygon here.
        """
        shape = str(shape or "blob").strip().lower()
        if shape in ("blob", "emoji"):
            return None
        rx = max(2.0, float(hw) / 2.0)
        ry = max(2.0, float(hh) / 2.0)
        cx, cy = float(cx), float(cy)
        if shape == "circle":
            n = 26
            return [(int(cx + math.cos(i * 2 * math.pi / n) * rx * 0.96),
                     int(cy + math.sin(i * 2 * math.pi / n) * ry * 0.96))
                    for i in range(n)]
        if shape == "square":
            kx, ky = rx * 0.94, ry * 0.94
            return [(int(cx - kx), int(cy - ky)), (int(cx + kx), int(cy - ky)),
                    (int(cx + kx), int(cy + ky)), (int(cx - kx), int(cy + ky))]
        if shape == "hex":
            pts = [(math.cos(math.radians(60 * i - 90)) * rx * 1.02,
                    math.sin(math.radians(60 * i - 90)) * ry * 1.02)
                   for i in range(6)]
        elif shape == "diamond":
            pts = [(0.0, -ry * 1.14), (rx * 1.14, 0.0),
                   (0.0, ry * 1.14), (-rx * 1.14, 0.0)]
        elif shape == "triangle":
            pts = [(0.0, -ry * 1.18), (rx * 1.12, ry * 0.84),
                   (-rx * 1.12, ry * 0.84)]
        elif shape == "star":
            pts = []
            for i in range(10):
                ang = math.radians(36 * i - 90)
                rad = 1.16 if i % 2 == 0 else 0.52
                pts.append((math.cos(ang) * rx * rad,
                            math.sin(ang) * ry * rad))
        else:
            return None
        return [(int(cx + px), int(cy + py)) for px, py in pts]

    def _draw_avatar(self, cx: int, cy: int, custom_scale: float = 1.0, slot_open_height: float = 0.0):
        # the mascot's size slider rides on top of whatever scale the caller
        # asked for, so a big mascot stays big in the idle view, in the
        # expanded view and in the file-pickup animation alike
        custom_scale = custom_scale * self.mascot_size()
        cy += int(self.bounce_offset)
        w = int(36 * custom_scale)
        h = int(32 * custom_scale)

        # The head leans and deforms before anything else is drawn, so the
        # eyes, the body and the antenna all travel with it.
        cx = int(cx + self._tilt * 8.0 * custom_scale)
        cy = int(cy + self._squish * 3.0 * custom_scale
                 + self._activity_mix["working"] * self._breathe * 1.2)
        avatar_rect = pygame.Rect(cx - w//2, cy - h//2, w, h)

        _self = self
        # The body is drawn FIRST so the head capsule sits on top of it.
        _stage = pygame.Rect(avatar_rect.x - 4, avatar_rect.y - 2,
                             avatar_rect.width + 8, avatar_rect.height + 26)
        self._draw_avatar_body(cx, cy, custom_scale, _stage)
        _p = self._p()
        
        # Soft metallic head capsule
        head_col = _p["head"]
        if self.emotion == "exhausted":
            head_col = (225, 230, 238)
        elif self.emotion == "loving":
            head_col = (255, 244, 248)
        elif self.emotion == "error":
            head_col = (255, 224, 224)

        head_pts, hw, hh = self._deform_head(cx, cy, w, h)
        _m = self.mascot()
        _shape = _m.get("shape", "blob")
        _glyph_only = bool(_shape == "emoji" and _m.get("emoji"))
        if _glyph_only:
            self._draw_emoji_head(cx, cy, _m["emoji"],
                                  _m.get("accent", head_col), custom_scale)
            _poly = None
        else:
            _poly = self._mascot_shape_points(_shape, cx, cy, hw, hh)
            if _poly is None:
                # the soft blob: the four deformed corners, rounded and
                # anti-aliased below.  There are deliberately NO circles at
                # the corners - they bulged out past the quad and read as
                # four round lumps stuck on the face.
                _poly = head_pts
            _poly = self._round_poly(_poly,
                                     self._corner_radius(_shape, hw, hh))
            self._draw_head_aa(_poly, head_col)
        # a moving sheen: the highlight slides as the head tilts, which is
        # what stops it reading as a flat sticker
        if not _glyph_only:
            sheen_x = cx - int(hw * 0.22) + int(self._tilt * hw * 0.18)
            sheen_y = cy - int(hh * 0.26) - int(self._breathe * custom_scale)
            pygame.draw.circle(self.ui_surface, self._lerp_col(head_col, (255, 255, 255), 0.55),
                               (sheen_x, sheen_y), max(1, int(3.5 * custom_scale)))

            # ears that flick when it shrugs - only on shapes they read on
            if _shape in ("blob", "circle", "square"):
                for i, sx in enumerate((-1, 1)):
                    ex = cx + sx * int(hw * 0.40) - int(self._tilt * custom_scale * 4)
                    ey = cy - int(hh * 0.42) - int(self._ear_tw[i] * 3 * custom_scale)
                    pygame.draw.circle(self.ui_surface, head_col,
                                       (ex, ey), max(2, int(3.5 * custom_scale)))

            self._draw_head_aa(_poly, None, (200, 208, 220))

            # A mascot with an emoji badge but a geometric head wears the glyph
            # just off its top-right corner, so two red hexagons are still
            # told apart at a glance.
            if _m.get("emoji"):
                try:
                    _bf = self._emoji_font(max(9, int(11 * custom_scale)))
                    _bs = _bf.render(_m["emoji"], True, tuple(_m.get("accent", head_col)))
                    self.ui_surface.blit(
                        _bs, _bs.get_rect(center=(cx + int(hw * 0.62),
                                                  cy - int(hh * 0.62))))
                except Exception:
                    pass
        self._draw_activity_ring(cx, cy, custom_scale)

        # Letterbox slot on top of head if opened
        if slot_open_height > 0.0:
            slot_w = int(24 * custom_scale)
            slot_h = max(2, int(slot_open_height * custom_scale))
            slot_rect = pygame.Rect(cx - slot_w//2, cy - h//2 - slot_h//2, slot_w, slot_h)
            pygame.draw.rect(self.ui_surface, (20, 22, 28), slot_rect, border_radius=2)
            pygame.draw.rect(self.ui_surface, (80, 210, 255), slot_rect, 1, border_radius=2)

        # Emotional Expressions
        t = time.time()
        
        if _glyph_only:
            # The glyph already carries a face; drawing eyes on top of it just
            # makes a smudge, so the whole expression chain is skipped.
            pass
        elif self.emotion == "dizzy" or self.is_dizzy:
            # Dizzy Spiral Eyes
            spin = t * 14.0
            for eye_x in (cx - int(7 * custom_scale), cx + int(7 * custom_scale)):
                ex = eye_x + math.cos(spin) * 2.0
                ey = cy + math.sin(spin) * 2.0
                pygame.draw.circle(self.ui_surface, (30, 30, 42), (int(ex), int(ey)), int(2 * custom_scale))
                pygame.draw.circle(self.ui_surface, (30, 30, 42), (eye_x, cy), int(3.5 * custom_scale), 1)

        elif self.emotion == "loving":
            # Pulsing Cute Pink Heart Pupils
            pulse = 1.0 + math.sin(t * 8.0) * 0.15
            for eye_x in (cx - int(7 * custom_scale), cx + int(7 * custom_scale)):
                # Draw heart
                hr = int(3 * custom_scale * pulse)
                pygame.draw.circle(self.ui_surface, (255, 80, 140), (eye_x - hr//2, cy - 1), hr)
                pygame.draw.circle(self.ui_surface, (255, 80, 140), (eye_x + hr//2, cy - 1), hr)
                points = [(eye_x - hr, cy), (eye_x + hr, cy), (eye_x, cy + hr + 1)]
                pygame.draw.polygon(self.ui_surface, (255, 80, 140), points)
            # Rosy Blushing Cheeks
            pygame.draw.circle(self.ui_surface, (255, 170, 190, 180), (cx - int(12 * custom_scale), cy + int(6 * custom_scale)), int(3 * custom_scale))
            pygame.draw.circle(self.ui_surface, (255, 170, 190, 180), (cx + int(12 * custom_scale), cy + int(6 * custom_scale)), int(3 * custom_scale))

        elif self.emotion == "happy" or self.emotion == "greeting":
            # Joyful Inverted Arch Smiling Eyes ( ^ _ ^ )
            for eye_x in (cx - int(7 * custom_scale), cx + int(7 * custom_scale)):
                arc_rect = pygame.Rect(eye_x - int(5 * custom_scale), cy - int(4 * custom_scale), int(10 * custom_scale), int(8 * custom_scale))
                pygame.draw.arc(self.ui_surface, (30, 32, 45), arc_rect, 0.2, math.pi - 0.2, int(2 * custom_scale))
            # Blushing Cheeks
            pygame.draw.circle(self.ui_surface, (255, 180, 190, 150), (cx - int(11 * custom_scale), cy + int(5 * custom_scale)), int(2.5 * custom_scale))
            pygame.draw.circle(self.ui_surface, (255, 180, 190, 150), (cx + int(11 * custom_scale), cy + int(5 * custom_scale)), int(2.5 * custom_scale))
            # If greeting, draw waving hand
            if self.emotion == "greeting":
                wave_angle = math.sin(t * 12.0) * 0.4
                hx = cx + int(18 * custom_scale) + math.cos(wave_angle) * 4
                hy = cy - int(6 * custom_scale) + math.sin(wave_angle) * 4
                pygame.draw.circle(self.ui_surface, (245, 248, 252), (int(hx), int(hy)), int(4 * custom_scale))

        elif self.emotion == "thinking":
            # Pondering Eyes Looking Up-Left + Floating orbital ring
            look_x = -2.5 * custom_scale
            look_y = -2.5 * custom_scale
            for eye_x in (cx - int(7 * custom_scale), cx + int(7 * custom_scale)):
                pygame.draw.circle(self.ui_surface, (225, 230, 240), (eye_x, cy), int(4 * custom_scale))
                pygame.draw.circle(self.ui_surface, (40, 90, 220), (int(eye_x + look_x), int(cy + look_y)), int(2.5 * custom_scale))
            # Orbital thinking halo dots
            for i in range(3):
                ang = t * 4.0 + (i * 2.09)
                ox = cx + math.cos(ang) * (20 * custom_scale)
                oy = cy - int(10 * custom_scale) + math.sin(ang) * (5 * custom_scale)
                pygame.draw.circle(self.ui_surface, (90, 180, 255), (int(ox), int(oy)), int(2 * custom_scale))

        elif self.emotion == "sleepy":
            # Closed Relaxed Eyelids
            for eye_x in (cx - int(7 * custom_scale), cx + int(7 * custom_scale)):
                pygame.draw.line(self.ui_surface, (40, 42, 55), (eye_x - int(4 * custom_scale), cy), (eye_x + int(4 * custom_scale), cy), int(2 * custom_scale))
            # Floating "z Z Z" particles
            zx = cx + int(14 * custom_scale)
            zy = cy - int(12 * custom_scale) - int((t * 15) % 18)
            ztxt = self.font_small.render("z", True, (130, 150, 190))
            self.ui_surface.blit(ztxt, (zx, zy))

        elif self.emotion == "exhausted":
            # Drooping Eyelids + Sweat Drop 💧
            for eye_x in (cx - int(7 * custom_scale), cx + int(7 * custom_scale)):
                pygame.draw.line(self.ui_surface, (50, 52, 65), (eye_x - int(4 * custom_scale), cy + 1), (eye_x + int(4 * custom_scale), cy + 3), int(2 * custom_scale))
            # Sweat bead
            sw_x = cx + int(14 * custom_scale)
            sw_y = cy - int(8 * custom_scale)
            pygame.draw.circle(self.ui_surface, (70, 180, 255), (sw_x, sw_y), int(3 * custom_scale))
            pygame.draw.polygon(self.ui_surface, (70, 180, 255), [(sw_x - 2, sw_y), (sw_x + 2, sw_y), (sw_x, sw_y - 4)])

        elif self.emotion == "bored":
            # Half-closed flat eyelids looking sideways
            look_x = math.sin(t * 1.5) * (2.5 * custom_scale)
            for eye_x in (cx - int(7 * custom_scale), cx + int(7 * custom_scale)):
                pygame.draw.circle(self.ui_surface, (230, 235, 242), (eye_x, cy), int(4 * custom_scale))
                pygame.draw.circle(self.ui_surface, (30, 32, 45), (int(eye_x + look_x), cy), int(2.5 * custom_scale))
                # Flat upper lid
                pygame.draw.line(self.ui_surface, head_col, (eye_x - int(5 * custom_scale), cy - int(2 * custom_scale)), (eye_x + int(5 * custom_scale), cy - int(2 * custom_scale)), int(3 * custom_scale))

        elif self.emotion == "error":
            # Alarmed: shivering head, wide eyes with tiny pupils, red ! badge
            sh = cx + int(math.sin(t * 42.0) * 2.0)
            for eye_x in (sh - int(7 * custom_scale), sh + int(7 * custom_scale)):
                pygame.draw.circle(self.ui_surface, (255, 236, 236), (eye_x, cy), int(4.5 * custom_scale))
                pygame.draw.circle(self.ui_surface, (200, 40, 40), (eye_x, cy), int(2.4 * custom_scale))
            bx, by = sh, cy - int(17 * custom_scale)
            tip = (bx, by - int(6 * custom_scale))
            pygame.draw.polygon(self.ui_surface, (255, 95, 85),
                                [tip, (bx - int(6 * custom_scale), by + int(5 * custom_scale)),
                                 (bx + int(6 * custom_scale), by + int(5 * custom_scale))])
            bang = self.font_small.render("!", True, (255, 255, 255))
            self.ui_surface.blit(bang, (bx - 2, by - int(2 * custom_scale)))

        else:
            # IDLE: Real-time Cursor Tracking Eyes
            look_x, look_y = self._gaze_offset(cx, cy)
            look_x *= custom_scale
            look_y *= custom_scale

            # pupils dilate with energy, so a thinking look really reads
            pupil_r = (2.2 + self._energy * 1.1) * custom_scale
            eye_xs = (cx - int(7 * custom_scale), cx + int(7 * custom_scale))
            for idx, eye_x in enumerate(eye_xs):
                if self._eye_openness(idx) < 0.55:
                    pygame.draw.line(self.ui_surface, (30, 30, 42),
                                     (eye_x - int(4 * custom_scale), cy),
                                     (eye_x + int(4 * custom_scale), cy),
                                     int(2 * custom_scale))
                    continue
                pygame.draw.circle(self.ui_surface, (230, 235, 242), (eye_x, cy), int(4 * custom_scale))
                px = int(eye_x + look_x)
                py = int(cy + look_y)
                pygame.draw.circle(self.ui_surface, (20, 22, 30), (px, py), int(pupil_r))
                pygame.draw.circle(self.ui_surface, (255, 255, 255), (px - 1, py - 1), 1)

        # Voice reaction: the waveform only appears while it is talking.
        self._draw_speech_wave(cx, cy, custom_scale)

    # -----------------------------------------------------------------------
    # Pinned shortcut grid: one layout, drawn and hit-tested the same way
    # -----------------------------------------------------------------------
    # -- model management ----------------------------------------------
    def scan_models(self, force=False):
        """Find every GGUF on disk so the models tab has something real.

        Reuses the same folder llama_server.py searches, plus a Phoenix
        models folder, so what you see here is what the brain can load.
        """
        if self.expanded_models_scanned and not force:
            return self.expanded_models
        found = []
        bases = [os.path.join(os.path.expanduser("~"), "Downloads"),
                 os.path.join(os.path.expanduser("~"), "Downloads", "qwen3vl-4b"),
                 os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "models"),
                 os.path.join(os.path.expanduser("~"), ".phoenix", "models")]
        seen = set()
        for base in bases:
            if not os.path.isdir(base):
                continue
            try:
                names = sorted(os.listdir(base))
            except Exception:
                continue
            for name in names:
                if not name.lower().endswith(".gguf"):
                    continue
                full = os.path.join(base, name)
                if full in seen or not os.path.isfile(full):
                    continue
                seen.add(full)
                try:
                    size_mb = os.path.getsize(full) / (1024 * 1024)
                except Exception:
                    size_mb = 0.0
                low = name.lower()
                found.append({
                    "path": full, "name": name, "size_mb": size_mb,
                    "kind": "projector" if low.startswith("mmproj") else "model",
                    "enabled": True,
                })
        # the projector first: it reads as the vision half of the pair
        found.sort(key=lambda m: (m["kind"] != "projector", m["name"].lower()))
        self.expanded_models = found
        self.expanded_models_scanned = True
        return found

    def set_active_model(self, path: str):
        """Point the brain at a different GGUF on disk."""
        if not path or not os.path.isfile(path):
            return False
        low = os.path.basename(path).lower()
        if low.startswith("mmproj"):
            self.active_mmproj_path = path
        else:
            self.active_model_path = path
        log_debug(f"Active model -> {os.path.basename(path)}")
        return True

    def start_model_download(self, repo: str, filename: str):
        """Fetch a GGUF from Hugging Face on a worker thread.

        The UI thread never blocks: progress lands in self.downloads and the
        tab reads it every frame.
        """
        if not filename:
            return False
        state = {"state": "queued", "pct": 0.0, "error": None,
                 "name": filename, "path": None}
        self.downloads[filename] = state
        url = f"https://huggingface.co/{repo}/resolve/main/{filename}"

        def worker():
            import requests
            state["state"] = "downloading"
            dest_dir = os.path.join(os.path.expanduser("~"), ".phoenix", "models")
            try:
                os.makedirs(dest_dir, exist_ok=True)
                dest = os.path.join(dest_dir, filename)
                with requests.get(url, stream=True, timeout=60) as resp:
                    resp.raise_for_status()
                    total = int(resp.headers.get("Content-Length") or 0)
                    done = 0
                    with open(dest, "wb") as out:
                        for chunk in resp.iter_content(chunk_size=1 << 20):
                            if not chunk:
                                continue
                            out.write(chunk)
                            done += len(chunk)
                            if total:
                                state["pct"] = round(done * 100.0 / total, 1)
                state["pct"] = 100.0
                state["state"] = "done"
                state["path"] = dest
                # it is on disk now, so show it straight away
                self.expanded_models_scanned = False
                self.scan_models(force=True)
            except Exception as e:
                state["state"] = "error"
                state["error"] = f"{type(e).__name__}: {e}"

        try:
            threading.Thread(target=worker, daemon=True).start()
            return True
        except Exception as e:
            state["state"] = "error"
            state["error"] = str(e)
            return False

    def _glyph_renders(self, ch: str) -> bool:
        """Does the emoji font really have this glyph?

        A font without it draws the SAME tofu box for every missing character,
        so compare against a private-use codepoint no font can have.  Cached.
        """
        if ch in self._glyph_cache:
            return self._glyph_cache[ch]
        ok = False
        font = getattr(self, "font_emoji", None)
        try:
            if font is not None:
                glyph = font.render(ch, True, (255, 255, 255))
                tofu = font.render("\ue123", True, (255, 255, 255))
                ok = (glyph.get_size() != (0, 0) and
                      pygame.image.tostring(glyph, "RGBA") !=
                      pygame.image.tostring(tofu, "RGBA"))
        except Exception:
            ok = False
        self._glyph_cache[ch] = ok
        return ok

    def _draw_app_icon(self, ch: str, rect: pygame.Rect, color, hover: bool = False,
                       height: int = 20):
        """One shortcut cell: light plate, app-colour border, and the app's own
        glyph - or its initial when the font has none.

        The plate is what makes it legible: colour emoji come in their own
        (sometimes near-black) colours, and a dark 🎵 on a dark cell was
        invisible.
        """
        plate = (255, 255, 255) if hover else (248, 250, 253)
        pygame.draw.rect(self.ui_surface, plate, rect, border_radius=9)
        pygame.draw.rect(self.ui_surface, color, rect, 2, border_radius=9)
        if self._glyph_renders(ch):
            surf = self.font_emoji.render(ch, True, (255, 255, 255))
        else:
            ink = tuple(int(c * 0.45) for c in color)     # readable on the plate
            surf = self.font_bold.render((ch or "?")[0].upper(), True, ink)
        if surf.get_height() > 0 and abs(surf.get_height() - height) > 2:
            scale = height / surf.get_height()
            surf = pygame.transform.smoothscale(
                surf, (max(1, int(surf.get_width() * scale)), height))
        self.ui_surface.blit(surf, surf.get_rect(center=rect.center))

    def _grid_origin(self):
        """Top-left of the 4 shortcut cells in the current mode, or None.

        Idle keeps them in the header row; while a task runs (or the composer is
        open) they move into a right-hand column of the body, so the shortcuts
        never disappear just because the bot is working.
        """
        if self.file_anim_state != "none" or self.settings_open:
            return None
        ix = int((self.win_w - self.current_width) / 2)
        iw = int(self.current_width)
        working = (self.input_active or
                   self.state in ("working", "thinking", "acting") or
                   bool(self.current_action))
        if working:
            return (ix + iw - 176, 70) if iw >= 420 else None
        return (ix + iw - 215, 20)

    def _draw_shortcut_grid(self):
        origin = self._grid_origin()
        if origin is None:
            return
        ox, oy = origin
        mx, my = self.get_cursor_pos()
        for i, app_id in enumerate(self.pinned_apps[:4]):
            app = next((a for a in ALL_SHORTCUT_APPS if a["id"] == app_id), None)
            if not app:
                continue
            sx, sy = ox + i * 42, oy
            srect = pygame.Rect(sx, sy, 36, 36)
            self._draw_app_icon(app["icon"], srect, app["color"],
                                hover=srect.collidepoint(mx, my), height=20)

    # -----------------------------------------------------------------------
    # Main Idle Mode with 4 Shortcut Bots
    # -----------------------------------------------------------------------
    def _draw_idle_mode(self, ix: int, iw: int, ih: int):
        # Draw Avatar on Left
        avatar_cx = ix + 36
        avatar_cy = 38
        self._draw_avatar(avatar_cx, avatar_cy)

        # Bot Name & Status
        name_txt = self.font_bold.render("PHOENIX MK4", True, (240, 245, 255))
        self.ui_surface.blit(name_txt, (ix + 64, 18))

        # Emotion badge / state
        status_color = (50, 215, 120)
        status_label = "VLM Ready · Click to command"
        if self.emotion == "greeting":
            status_label = "Hello there! Ready to assist!"
            status_color = (255, 215, 60)
        elif self.emotion == "loving":
            status_label = "At your service with love ❤️"
            status_color = (255, 110, 180)
        elif self.emotion == "exhausted":
            status_label = "Phew... heavy tasks completed"
            status_color = (255, 130, 80)
        elif self.emotion == "sleepy":
            status_label = "Resting in standby mode... zZ"
            status_color = (140, 160, 220)
        elif self.emotion == "error":
            status_label = "Something went wrong - checking it"
            status_color = (255, 95, 85)

        pygame.draw.circle(self.ui_surface, status_color, (ix + 68, 44), 3)
        st_txt = self.font_small.render(status_label, True, (160, 168, 185))
        self.ui_surface.blit(st_txt, (ix + 78, 38))

        # 4 Main App Shortcut Grid (header while idle, body column while working)
        self._draw_shortcut_grid()

    # -----------------------------------------------------------------------
    # Settings App Picker Modal (Customizing the 4 shortcuts)
    # -----------------------------------------------------------------------
    _PERSONALITY_LABELS = {
        "normal": "Normal", "butler": "Butler", "brother": "Brother",
        "anime_girl": "Anime", "italian": "Italian", "russian": "Russian",
    }

    def _main_app_button(self, ix: int):
        """The one button that replaced the six personality chips.

        Returns the rect and is used for BOTH drawing and hit-testing, so the
        button you see is always the button you click.  Choosing a mascot,
        a colour, a shape, a size or a personality now happens in the main
        app window; the island only opens it.
        """
        return pygame.Rect(ix + 30, 206, 660, 36)

    def _draw_main_app_row(self, ix: int):
        mx, my = self.get_cursor_pos()
        head = self.font_small.render("Mascot & personality", True, (150, 158, 175))
        self.ui_surface.blit(head, (ix + 30, 190))

        rect = self._main_app_button(ix)
        accent = self.mascot().get("accent", (90, 170, 255))
        hover = rect.collidepoint(mx, my)
        pygame.draw.rect(self.ui_surface,
                         (26, 32, 48) if hover else (21, 25, 36),
                         rect, border_radius=9)
        pygame.draw.rect(self.ui_surface, accent, rect,
                         2 if hover else 1, border_radius=9)

        who = self.mascot_label()
        label = f"Open Phoenix main app  -  mascot: {who}  (M)"
        txt = self.font_bold.render(label, True, (240, 245, 255))
        self.ui_surface.blit(txt, txt.get_rect(center=rect.center))
        hint = self.font_small.render("press M", True, accent)
        self.ui_surface.blit(hint, (rect.right - hint.get_width() - 10,
                                    rect.centery - hint.get_height() // 2))

    # -----------------------------------------------------------------------
    # The expanded view: models, history, settings
    # -----------------------------------------------------------------------
    _EXPANDED_TABS = (("models", "Models"), ("history", "History"),
                      ("settings", "Settings"))

    def toggle_expanded(self, tab=None):
        self.expanded_open = not self.expanded_open
        if self.expanded_open:
            if tab:
                self._set_expanded_tab(tab)
            self.expanded_scroll = 0
            self.expanded_models_scanned = False
            self.target_width = 860.0
            self.target_height = 560.0
            self.target_radius = 26.0
            if self.expanded_tab == "history":
                self.clear_history_cache()
        else:
            self.expanded_scroll = 0
            if self.settings_open:
                self.target_width, self.target_height = 720.0, 250.0
                self.target_radius = 24.0
            else:
                self.target_width, self.target_height = 540.0, 76.0
                self.target_radius = 30.0
        return self.expanded_open

    def _expanded_scroll_max(self):
        """How far the active tab can scroll, so the wheel stops at the end."""
        iw = int(self.current_width)
        ih = int(self.current_height)
        body_h = ih - 120
        if self.expanded_tab == "models":
            rows = len(self.scan_models()) * 52 + 40 + len(self.downloads) * 40
        elif self.expanded_tab == "history":
            rows = len(self.load_history()) * 44 + 40
        else:
            # derived from the layout table, not typed out again
            rows = self._settings_content_height() + 16
        return max(0, rows - body_h + 16)

    def _expanded_tab_rects(self, ix: int):
        out = []
        for i, (key, label) in enumerate(self._EXPANDED_TABS):
            out.append((key, label, pygame.Rect(ix + 24 + i * 116, 62, 108, 32)))
        return out

    def _draw_splash(self, ix: int, iw: int, ih: int):
        """Startup splash: the mascot orbits in on a shrinking spiral,
        overshoots as it lands, and the panel settles around it."""
        t = self._splash_t
        bg = (12, 13, 17)
        p = self._p()
        ccx = ix + iw // 2
        hub_y = ih // 2 - 10

        # the orbit collapses over the first 1.6s
        orbit = max(0.0, 1.0 - t / 1.6)
        ang = -1.3 + t * 4.6
        rad = 30.0 * orbit

        # a fading trail of where it has been
        for i in range(5):
            k = (1.0 - i / 5.0) * orbit
            a2 = ang - 0.30 * (i + 1)
            pygame.draw.circle(
                self.ui_surface, self._lerp_col(bg, p["accent"], 0.34 * k),
                (int(ccx + math.cos(a2) * rad * 2.4),
                 int(hub_y + math.sin(a2) * rad * 0.55)),
                max(1, int(4.0 * k)))
        if rad > 5.0:
            pygame.draw.circle(
                self.ui_surface, self._lerp_col(bg, p["accent"], 0.22 * orbit),
                (ccx, hub_y), int(rad), 1)

        # springy landing: overshoots the final size, then settles
        land = min(1.0, t / 1.6)
        bounce = 1.0 + 0.22 * math.sin(land * math.pi) * (1.0 - land)
        scale = (0.5 + 0.5 * land) * bounce
        mx = int(ccx + math.cos(ang) * rad * 2.4)
        my = int(hub_y + math.sin(ang) * rad * 0.55)

        fade = max(0.0, min(1.0, (t - 0.85) / 0.45)) * \
            max(0.0, min(1.0, (self.SPLASH_TIME - t) / 0.5))
        if fade > 0.02:
            name = self.font_title.render(
                "PHOENIX", True, self._lerp_col(bg, (240, 245, 255), fade))
            self.ui_surface.blit(name, name.get_rect(center=(ccx, ih - 34)))
        self._draw_avatar(mx, my, custom_scale=scale)

    def _expanded_body(self, ix: int, iw: int, ih: int):
        """The scrolling body rect.

        Draw AND hit-test MUST share this, or while the panel is sliding
        in the row you see is not the row you click.
        """
        rise = int((1.0 - self._expanded_anim) * 26)
        slide = int((1.0 - self._tab_anim) * 34 * self._tab_dir)
        return pygame.Rect(ix + 16 + slide, 104 + rise, iw - 32, ih - 120)

    def _set_expanded_tab(self, key: str):
        """Switch tab and restart the cross-slide from the old tab."""
        order = [k for k, _label in self._EXPANDED_TABS]
        if key not in order:
            return False
        if key == self.expanded_tab:
            return True
        self._tab_dir = 1.0 if order.index(key) > order.index(self.expanded_tab) else -1.0
        self.expanded_tab = key
        self.expanded_scroll = 0
        self._tab_anim = 0.0
        if key == "history":
            self.clear_history_cache()
        return True

    def _draw_expanded_mode(self, ix: int, iw: int, ih: int):
        a = self._expanded_anim
        bg = (12, 13, 17)
        body = self._expanded_body(ix, iw, ih)

        # While the panel is sliding in its body deliberately overshoots the
        # capsule, so clip to it - otherwise rows paint outside the island.
        prev_clip = self.ui_surface.get_clip()
        self.ui_surface.set_clip((prev_clip or self.ui_surface.get_rect())
                                 .clip(pygame.Rect(ix, 0, iw, ih)))
        try:
            self._draw_expanded_body(ix, iw, ih, a, bg, body)
        finally:
            self.ui_surface.set_clip(prev_clip)

    def _draw_expanded_body(self, ix: int, iw: int, ih: int, a: float,
                            bg, body):
        title = self.font_title.render(
            "PHOENIX MK4", True, self._lerp_col(bg, (245, 248, 255), a))
        self.ui_surface.blit(title, (ix + 24, 18 - int((1.0 - a) * 10)))
        sub = self.font_small.render(
            "Expanded view - E toggles, Esc closes", True,
            self._lerp_col(bg, (150, 158, 175), a))
        self.ui_surface.blit(sub, (ix + 24, 40 - int((1.0 - a) * 8)))

        # tab bar drops into place
        mx, my = self.get_cursor_pos()
        drop = int((1.0 - a) * 14)
        for key, label, rect in self._expanded_tab_rects(ix):
            rect = rect.move(0, drop)
            active = (self.expanded_tab == key)
            hover = rect.collidepoint(mx, my)
            body_bg = ((32, 40, 60) if active
                       else ((24, 29, 44) if hover else (20, 24, 34)))
            pygame.draw.rect(self.ui_surface, self._lerp_col(bg, body_bg, a),
                             rect, border_radius=9)
            col = self._lerp_col(bg, (110, 180, 255) if active else (150, 158, 175), a)
            pygame.draw.rect(self.ui_surface, col, rect, 2 if active else 1,
                             border_radius=9)
            txt = self.font_bold.render(
                label, True,
                self._lerp_col(bg,
                               (235, 242, 255) if active else (160, 168, 185), a))
            self.ui_surface.blit(txt, txt.get_rect(center=rect.center))

        pygame.draw.rect(self.ui_surface, self._lerp_col(bg, (18, 21, 30), a),
                         body, border_radius=12)
        pygame.draw.rect(self.ui_surface, self._lerp_col(bg, (45, 52, 70), a),
                         body, 1, border_radius=12)

        if self.expanded_tab == "models":
            self._draw_models_tab(body, mx, my)
        elif self.expanded_tab == "history":
            self._draw_history_tab(body, mx, my)
        else:
            self._draw_settings_tab(body, mx, my)

    # -- models ----------------------------------------------------------
    def _draw_models_tab(self, box, mx, my):
        models = self.scan_models()
        y = box.y + 14 - self.expanded_scroll

        head = self.font_small.render(
            f"{len(models)} GGUF file(s) found - click one to make it the brain",
            True, (150, 158, 175))
        self.ui_surface.blit(head, (box.x + 16, y))
        y += 26

        for m in models:
            if y > box.bottom or y + 46 < box.y:
                y += 52
                continue
            rect = pygame.Rect(box.x + 16, y, box.width - 32, 46)
            is_active = (m["path"] == self.active_model_path or
                         m["path"] == self.active_mmproj_path)
            hover = rect.collidepoint(mx, my)
            bg = (30, 38, 58) if is_active else ((24, 29, 44) if hover else (22, 26, 38))
            pygame.draw.rect(self.ui_surface, bg, rect, border_radius=9)
            accent = (150, 120, 230) if m["kind"] == "projector" else (90, 170, 255)
            pygame.draw.rect(self.ui_surface, accent if is_active else (48, 55, 74),
                             rect, 2 if is_active else 1, border_radius=9)

            tag = "vision" if m["kind"] == "projector" else "brain"
            pill = self.font_small.render(tag, True, accent)
            self.ui_surface.blit(pill, (rect.x + 12, rect.centery - 6))

            name = self.font_regular.render(m["name"][:46], True, (230, 236, 248))
            self.ui_surface.blit(name, (rect.x + 62, rect.centery - 16))
            meta = self.font_small.render(
                f"{m['size_mb']:.0f} MB", True, (130, 140, 160))
            self.ui_surface.blit(meta, (rect.x + 62, rect.centery + 2))
            if is_active:
                act = self.font_small.render("ACTIVE", True, (120, 235, 150))
                self.ui_surface.blit(act, (rect.right - 62, rect.centery - 6))
            y += 52

        if not models:
            none = self.font_regular.render(
                "No GGUF files found in Downloads/ or ~/.phoenix/models",
                True, (140, 150, 170))
            self.ui_surface.blit(none, (box.x + 16, y))

        # downloads, with a live progress bar
        for name, d in self.downloads.items():
            if d["state"] == "error":
                continue
            if y > box.bottom:
                break
            rect = pygame.Rect(box.x + 16, y, box.width - 32, 34)
            pygame.draw.rect(self.ui_surface, (22, 26, 38), rect, border_radius=8)
            lbl = self.font_small.render(
                f"{d['name'][:40]}  {d['pct']:.0f}%", True, (150, 190, 240))
            self.ui_surface.blit(lbl, (rect.x + 12, rect.y + 4))
            bar = pygame.Rect(rect.x + 12, rect.y + 20, rect.width - 24, 7)
            pygame.draw.rect(self.ui_surface, (40, 46, 62), bar, border_radius=3)
            fill = bar.copy()
            fill.width = max(2, int(bar.width * (d["pct"] / 100.0)))
            pygame.draw.rect(self.ui_surface, (90, 200, 140), fill, border_radius=3)
            y += 40

    # -- history ---------------------------------------------------------
    def _draw_history_tab(self, box, mx, my):
        rows = self.load_history()
        y = box.y + 14 - self.expanded_scroll
        head = self.font_small.render(
            f"{len(rows)} saved message(s) - newest first, click to restore",
            True, (150, 158, 175))
        self.ui_surface.blit(head, (box.x + 16, y))
        y += 26

        if not rows:
            none = self.font_regular.render("No history saved yet.",
                                           True, (140, 150, 170))
            self.ui_surface.blit(none, (box.x + 16, y))
            return

        for idx, row in enumerate(rows):
            if y > box.bottom:
                break
            if y + 40 < box.y:
                y += 44
                continue
            rect = pygame.Rect(box.x + 16, y, box.width - 32, 38)
            hover = rect.collidepoint(mx, my)
            pygame.draw.rect(self.ui_surface,
                             (26, 32, 48) if hover else (21, 25, 36),
                             rect, border_radius=8)
            role = str(row.get("role", "agent"))
            accent = (120, 200, 255) if role == "you" else (130, 230, 170)
            txt = self.font_regular.render(
                f"{role}: {str(row.get('text', ''))[:62]}", True, accent)
            self.ui_surface.blit(txt, (rect.x + 12, rect.centery - 7))
            ts = self.font_small.render(str(row.get("ts", ""))[11:16],
                                        True, (110, 120, 140))
            self.ui_surface.blit(ts, (rect.right - 46, rect.centery - 6))
            y += 44

    # -- settings --------------------------------------------------------
    _SETTING_ROWS = (
        ("translucent", "Semi-transparent window",
         "per-pixel alpha, falls back to colorkey"),
        ("click_through", "Click-through while working",
         "clicks pass to the app underneath"),
        ("speech_wave", "Speech waveform", "the voice bars beside the face"),
        ("show_step_badge", "Step badge", "Step n/24 in the corner"),
        ("shortcuts_always", "Shortcuts never hide",
         "the app grid stays put while working"),
        ("chat_history", "Save chat history",
         "writes every turn to ~/.phoenix"),
    )

    # The one row that used to be six personality chips.  Picking a mascot,
    # a colour, a shape, a size and a personality is now the main app
    # window's job; the island just points at it.
    _MAIN_APP_ROW = ("main_app", "Phoenix main app",
                     "Mascots, models and settings - press M")

    def _settings_layout(self, box):
        """[(kind, key, rect)] for the whole settings tab, top to bottom.

        The draw path, the hit-test and _expanded_scroll_max all read this,
        so the chip you see is always the chip you click and the scroll
        range always matches the content.
        """
        out = [("voice", "voice",
                pygame.Rect(box.x + 16, box.y + 16, box.width - 32, 40))]
        y = box.y + 68
        out.append(("label", "", pygame.Rect(box.x + 16, y, 0, 22)))
        y += 24
        out.append(("main_app", "main_app",
                    pygame.Rect(box.x + 16, y, box.width - 32, 40)))
        y += 46
        out.append(("label", "Features", pygame.Rect(box.x + 16, y, 0, 22)))
        y += 26
        for key, _label, _why in self._SETTING_ROWS:
            out.append(("toggle", key,
                        pygame.Rect(box.x + 16, y, box.width - 32, 40)))
            y += 44
        return out

    def _setting_row_spec(self, key):
        for spec in self._SETTING_ROWS:
            if spec[0] == key:
                return spec
        return (key, key, "")

    def _settings_content_height(self):
        """How tall the settings tab really is, measured off the same table."""
        rows = self._settings_layout(pygame.Rect(0, 0, 200, 200))
        return (rows[-1][2].bottom + 8) if rows else 0

    def _voice_on(self):
        try:
            return bool(self.on_voice_state()) if self.on_voice_state else True
        except Exception:
            return True

    def _toggle_voice(self):
        """Ask the bot to flip the voice.  Quietly does nothing when the bot
        did not wire itself up (the island running on its own)."""
        try:
            if callable(self.on_toggle_voice):
                self.on_toggle_voice()
                return True
        except Exception:
            pass
        return False

    def _draw_voice_row(self, box, mx, my, y):
        rect = pygame.Rect(box.x + 16, y, box.width - 32, 40)
        hover = rect.collidepoint(mx, my)
        pygame.draw.rect(self.ui_surface,
                         (26, 32, 48) if hover else (21, 25, 36), rect,
                         border_radius=8)
        name = self.font_regular.render("Voice (speech)", True, (225, 232, 245))
        self.ui_surface.blit(name, (rect.x + 12, rect.y + 6))
        on = self._voice_on()
        sub = self.font_small.render(
            "ON - press V to mute" if on else "OFF - press V to speak",
            True, (120, 200, 150) if on else (200, 140, 140))
        self.ui_surface.blit(sub, (rect.x + 12, rect.y + 22))
        sw = pygame.Rect(rect.right - 62, rect.centery - 10, 44, 20)
        pygame.draw.rect(self.ui_surface, (50, 200, 130) if on else (60, 66, 84),
                         sw, border_radius=10)
        kx = sw.right - 17 if on else sw.x + 3
        pygame.draw.circle(self.ui_surface, (240, 245, 255), (kx + 7, sw.centery), 7)
        return rect

    def _draw_settings_tab(self, box, mx, my):
        # One layout table feeds the draw path, the hit-test AND the scroll
        # height.  The tab used to hard-code its offsets three times over and
        # the chips silently missed clicks when they drifted apart.
        sy = self.expanded_scroll
        for kind, key, rect in self._settings_layout(box):
            if kind == "label":
                text = "Main app" if not key else key
                head = self.font_small.render(text, True, (150, 158, 175))
                self.ui_surface.blit(head, (rect.x, rect.y - sy))
                continue

            rect = rect.move(0, -sy)
            if kind == "voice":
                self._draw_voice_row(box, mx, my, rect.y)
                continue

            if kind == "main_app":
                hover = rect.collidepoint(mx, my)
                pygame.draw.rect(self.ui_surface,
                                 (26, 32, 48) if hover else (21, 25, 36),
                                 rect, border_radius=8)
                accent = self.mascot().get("accent", (90, 170, 255))
                pygame.draw.rect(self.ui_surface, accent, rect,
                                 2 if hover else 1, border_radius=8)
                name = self.font_regular.render(self._MAIN_APP_ROW[1], True,
                                                (225, 232, 245))
                self.ui_surface.blit(name, (rect.x + 12, rect.y + 4))
                sub = self.font_small.render(self._MAIN_APP_ROW[2], True,
                                             (120, 130, 150))
                self.ui_surface.blit(sub, (rect.x + 12, rect.y + 20))
                badge = self.font_small.render(self.mascot_label(), True, accent)
                self.ui_surface.blit(badge,
                                     (rect.right - 12 - badge.get_width(),
                                      rect.centery - badge.get_height() // 2))
                continue

            key, label, why = self._setting_row_spec(key)
            hover = rect.collidepoint(mx, my)
            pygame.draw.rect(self.ui_surface,
                             (26, 32, 48) if hover else (21, 25, 36),
                             rect, border_radius=8)
            name = self.font_regular.render(label, True, (225, 232, 245))
            self.ui_surface.blit(name, (rect.x + 12, rect.y + 6))
            sub = self.font_small.render(why, True, (120, 130, 150))
            self.ui_surface.blit(sub, (rect.x + 12, rect.y + 22))

            sw = pygame.Rect(rect.right - 62, rect.centery - 10, 44, 20)
            on = bool(self.setting_toggles.get(key, False))
            pygame.draw.rect(self.ui_surface,
                             (50, 200, 130) if on else (60, 66, 84), sw,
                             border_radius=10)
            knob = 14
            kx = sw.right - knob - 3 if on else sw.x + 3
            pygame.draw.circle(self.ui_surface, (240, 245, 255),
                               (kx + knob // 2, sw.centery), knob // 2)

    # -- hit testing -----------------------------------------------------
    def _expanded_hit(self, pos, ix, iw, ih):
        """Returns True when the click was consumed by the panel."""
        # the same gate the draw path uses, so a click cannot slip through
        # to the island underneath while the panel is still animating out
        if not (self.expanded_open or self._expanded_anim > 0.002):
            return False
        mx, my = pos
        for key, _label, rect in self._expanded_tab_rects(ix):
            if rect.collidepoint(mx, my):
                self._set_expanded_tab(key)
                return True

        body = self._expanded_body(ix, iw, ih)
        # rows the animation has clipped off the panel are not clickable
        body = body.clip(pygame.Rect(ix, 104, iw, max(0, ih - 104)))
        if not body.collidepoint(mx, my):
            return True          # swallow stray clicks inside the panel

        # models tab: click a file to select it
        if self.expanded_tab == "models":
            y = body.y + 40 - self.expanded_scroll
            for m in self.scan_models():
                rect = pygame.Rect(body.x + 16, y, body.width - 32, 46)
                if rect.collidepoint(mx, my):
                    self.set_active_model(m["path"])
                    return True
                y += 52

        # history tab: click a row to restore that turn
        elif self.expanded_tab == "history":
            y = body.y + 40 - self.expanded_scroll
            for idx, _row in enumerate(self.load_history()):
                rect = pygame.Rect(body.x + 16, y, body.width - 32, 38)
                if rect.collidepoint(mx, my):
                    self.load_history_turn(idx)
                    return True
                y += 44

        # settings tab: the main-app row and the toggles, from one table
        else:
            for kind, key, rect in self._settings_layout(body):
                if kind == "label":
                    continue
                if not rect.move(0, -self.expanded_scroll).collidepoint(mx, my):
                    continue
                if kind == "voice":
                    self._toggle_voice()
                    return True
                if kind == "main_app":
                    self.open_control_app()
                    return True
                self.setting_toggles[key] = not self.setting_toggles.get(key)
                self._apply_transparency()
                return True
        return True

    # ---- colorkey: the path that is known to work here -----------------
    def _use_colorkey(self, hwnd=None):
        """Binary transparency: RGB(1,1,1) becomes a hole in the window."""
        try:
            hwnd = hwnd or self._hwnd()
            if not hwnd:
                return False
            user32 = ctypes.windll.user32
            GWL_EXSTYLE, WS_EX_LAYED, LWA_COLORKEY = -20, 0x00080000, 0x1
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYED)
            user32.SetLayeredWindowAttributes(hwnd, 0x00010101, 0, LWA_COLORKEY)
            self._per_pixel_alpha = False
            self._alpha_applied = True
            return True
        except Exception as e:
            log_debug(f"colorkey failed: {e}")
            return False

    # ---- per-pixel alpha: a genuinely translucent panel -----------------
    def _apply_transparency(self):
        """Switch between colorkey and real per-pixel alpha.

        Per-pixel alpha needs a 32-bit premultiplied BGRA DIB uploaded with
        UpdateLayeredWindow.  If any step fails we drop back to colorkey for
        good, so the island can never end up invisible.
        """
        try:
            hwnd = self._hwnd()
            if not hwnd:
                return
            if not self.setting_toggles.get("translucent", False):
                self._use_colorkey(hwnd)
                return
            if self._alpha_failed:
                self._use_colorkey(hwnd)
                return
            if self._blit_per_pixel_alpha(hwnd):
                self._per_pixel_alpha = True
                self._alpha_applied = True
            else:
                self._alpha_failed = True
                log_debug("per-pixel alpha unavailable; staying on colorkey")
                self._use_colorkey(hwnd)
        except Exception as e:
            self._alpha_failed = True
            log_debug(f"per-pixel alpha failed ({e}); staying on colorkey")
            try:
                self._use_colorkey(self._hwnd())
            except Exception:
                pass

    def _blit_per_pixel_alpha(self, hwnd):
        """Upload the current frame, with its own alpha, to the window.

        Returns False on any failure; the caller falls back to colorkey.
        """
        if not IS_WINDOWS:
            return False
        mem_dc = hbitmap = old = screen_dc = None
        try:
            # use_last_error is what makes ctypes.get_last_error() real;
            # without it every failure reports 0 and tells you nothing.
            try:
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
            except Exception:
                user32 = ctypes.windll.user32
                gdi32 = ctypes.windll.gdi32

            surf = pygame.display.get_surface()
            if surf is None:
                return False
            w, h = surf.get_size()

            # The window must be layered BEFORE going per-pixel; mixing
            # SetLayeredWindowAttributes and UpdateLayeredWindow is what
            # makes this path go wrong.
            GWL_EXSTYLE, WS_EX_LAYED = -20, 0x00080000
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYED)

            ULONG, LONG = ctypes.c_ulong, ctypes.c_long

            class BITMAPINFOHEADER(ctypes.Structure):
                # biPlanes and biBitCount are WORD in the real Win32 struct.
                # Declaring them as 32-bit ULONG makes this 44 bytes when
                # Windows requires EXACTLY 40, and CreateDIBSection rejects
                # the bad biSize - returning NULL and setting no last-error,
                # which is why this path failed silently twice before.
                _fields_ = [("biSize", ULONG),
                            ("biWidth", LONG), ("biHeight", LONG),
                            ("biPlanes", ctypes.c_ushort),
                            ("biBitCount", ctypes.c_ushort),
                            ("biCompression", ULONG), ("biSizeImage", ULONG),
                            ("biXPelsPerMeter", LONG), ("biYPelsPerMeter", LONG),
                            ("biClrUsed", ULONG), ("biClrImportant", ULONG)]

            class BITMAPINFO(ctypes.Structure):
                _fields_ = [("bmiHeader", BITMAPINFOHEADER),
                            ("bmiColors", ULONG * 3)]

            if ctypes.sizeof(BITMAPINFOHEADER) != 40:
                # If this ever trips, GDI will fail with no error code and
                # the island will silently fall back to colorkey.
                log_debug(f"alpha: BITMAPINFOHEADER is "
                          f"{ctypes.sizeof(BITMAPINFOHEADER)} bytes, need 40")
                return False

            bi = BITMAPINFO()
            bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bi.bmiHeader.biWidth = w
            bi.bmiHeader.biHeight = -h          # negative = top-down
            bi.bmiHeader.biPlanes = 1
            bi.bmiHeader.biBitCount = 32
            bi.bmiHeader.biCompression = 0      # BI_RGB

            # ctypes defaults every return to a 32-bit int, which TRUNCATES
            # 64-bit handles (DC, HBITMAP) and makes CreateDIBSection fail.
            # Declare the real signatures once per call site.
            voidp = ctypes.c_void_p
            user32.GetDC.restype = voidp
            user32.ReleaseDC.argtypes = [ctypes.c_void_p, voidp]
            gdi32.CreateCompatibleDC.restype = voidp
            gdi32.CreateCompatibleDC.argtypes = [voidp]
            gdi32.DeleteDC.argtypes = [voidp]
            gdi32.CreateDIBSection.restype = voidp
            gdi32.CreateDIBSection.argtypes = [voidp,
                                              ctypes.POINTER(BITMAPINFO),
                                              wintypes.UINT,
                                              ctypes.POINTER(voidp),
                                              voidp, wintypes.DWORD]
            gdi32.SelectObject.restype = voidp
            gdi32.SelectObject.argtypes = [voidp, voidp]
            gdi32.DeleteObject.argtypes = [voidp]
            gdi32.SetDIBits.argtypes = [voidp, voidp, wintypes.UINT,
                                        wintypes.UINT, voidp,
                                        ctypes.POINTER(BITMAPINFO), wintypes.UINT]
            gdi32.SetDIBits.restype = ctypes.c_int


            screen_dc = user32.GetDC(hwnd)
            if not screen_dc:
                log_debug("alpha: GetDC failed")
                return False
            mem_dc = gdi32.CreateCompatibleDC(screen_dc)
            if not mem_dc:
                log_debug("alpha: CreateCompatibleDC failed")
                return False
            hbitmap = gdi32.CreateDIBSection(mem_dc, ctypes.byref(bi), 0,
                                             None, None, 0)
            if not hbitmap:
                log_debug("alpha: CreateDIBSection failed (err %d)"
                          % ctypes.get_last_error())
                return False
            old = gdi32.SelectObject(mem_dc, hbitmap)
            if not old:
                return False

            raw = pygame.image.tobytes(surf, "RGBA")
            # How opaque the panel is allowed to be.  Anything that was
            # fully opaque stays fully opaque in shape (the island's rounded
            # corners stay rounded) but drops to `level`, so the desktop
            # shows through the panel without eating the corners.
            level = float(self.setting_toggles.get("translucent_level", 0.86))
            level = max(0.0, min(1.0, level))

            if np is not None:
                # premultiplied BGRA, which is what UpdateLayeredWindow eats.
                # This runs EVERY FRAME, so it has to be vectorised (the
                # nested-Python version cost ~190ms/frame on the expanded
                # panel) AND restricted: the window is 860x576 but the idle
                # capsule is 76px tall at the top, so converting the whole
                # window wasted ~85% of the work on background that is
                # already fully transparent.  Only the capsule band, plus a
                # margin for the avatar's antenna and the border glow.
                margin = 34
                band = min(h, int(self.current_height) + margin)
                src = np.frombuffer(raw, dtype=np.uint8).reshape(h, w, 4)
                dst = np.zeros_like(src)
                sub = src[:band, :]
                out = dst[:band, :]
                al = np.rint(sub[:, :, 3].astype(np.float32) * level)
                al = np.clip(al, 0, 255).astype(np.uint16)
                # One multiply for all three colour channels at once, then
                # swap R and B for BGRA.  Doing the three separately meant
                # three temporaries and cost ~18ms on the expanded panel.
                premul = (sub[:, :, :3].astype(np.uint16)
                          * al[:, :, None] // 255).astype(np.uint8)
                out[:, :, :3] = premul[:, :, ::-1]
                out[:, :, 3] = al.astype(np.uint8)
                dib = dst.tobytes()
            else:
                row_bytes = w * 4
                buf = bytearray(row_bytes * h)
                lut = [min(255, int(a * level + 0.5)) for a in range(256)]
                for y in range(h):
                    base = y * row_bytes
                    row = raw[base:base + row_bytes]
                    for x in range(w):
                        i = base + x * 4
                        a = row[x * 4 + 3]
                        if a == 0:
                            continue
                        na = lut[a]
                        if na == 0:
                            continue
                        buf[i] = int(row[x * 4 + 2] * na / 255)
                        buf[i + 1] = int(row[x * 4 + 1] * na / 255)
                        buf[i + 2] = int(row[x * 4] * na / 255)
                        buf[i + 3] = na
                dib = bytes(buf)

            wrote = gdi32.SetDIBits(mem_dc, hbitmap, 0, h,
                                     (ctypes.c_ubyte * len(dib)).from_buffer_copy(dib),
                                     ctypes.byref(bi), 0)
            if wrote == 0:
                log_debug(f"alpha: SetDIBits wrote 0 rows (w={w} h={h})")
                return False

            ULW_ALPHA = 0x02
            AC_SRC_OVER, AC_SRC_ALPHA = 0x00, 0x01
            size = (ctypes.c_long * 2)(w, h)
            dest = (ctypes.c_long * 2)(0, 0)
            # ctypes.wintypes has NO BLENDFUNCTION, so declaring one there
            # raises AttributeError and the whole per-pixel path dies before
            # it is ever called.  Define it here.
            class BLENDFUNCTION(ctypes.Structure):
                _fields_ = [("BlendOp", ctypes.c_ubyte),
                            ("BlendFlags", ctypes.c_ubyte),
                            ("SourceConstantAlpha", ctypes.c_ubyte),
                            ("AlphaFormat", ctypes.c_ubyte)]

            user32.UpdateLayeredWindow.argtypes = [
                voidp, voidp, ctypes.c_void_p, ctypes.c_void_p,
                voidp, ctypes.c_void_p, wintypes.COLORREF,
                ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD]
            user32.UpdateLayeredWindow.restype = wintypes.BOOL
            blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
            ok = user32.UpdateLayeredWindow(
                voidp(int(hwnd)), voidp(screen_dc), None, size,
                voidp(mem_dc), dest, 0, ctypes.byref(blend), ULW_ALPHA)
            if not ok:
                log_debug("alpha: UpdateLayeredWindow returned 0")
            return bool(ok)
        except Exception as e:
            # The full traceback matters: a bare str(e) from ctypes says
            # "argument 1: TypeError: wrong type", which does not say WHICH
            # call or WHICH argument.  Two earlier attempts failed here for
            # exactly this reason.
            log_debug("UpdateLayeredWindow path: %s\n%s"
                      % (e, traceback.format_exc()))
            return False
        finally:
            try:
                if mem_dc and old:
                    gdi32.SelectObject(mem_dc, old)
                if hbitmap:
                    gdi32.DeleteObject(hbitmap)
                if mem_dc:
                    gdi32.DeleteDC(mem_dc)
                if screen_dc:
                    user32.ReleaseDC(hwnd, screen_dc)
            except Exception:
                pass

    def _draw_settings_mode(self, ix: int, iw: int, ih: int):
        title = self.font_title.render("Configure Main Grid Shortcuts (Pick 4)", True, (245, 248, 255))
        self.ui_surface.blit(title, (ix + 24, 18))

        sub = self.font_small.render("Click any app to pin/unpin from your 4-slot Dynamic Island grid. Click ⚙️ to finish.", True, (150, 158, 175))
        self.ui_surface.blit(sub, (ix + 24, 40))

        mx, my = self.get_cursor_pos()
        picker_start_y = 65

        for idx, app in enumerate(ALL_SHORTCUT_APPS):
            col = idx % 4
            row = idx // 4
            bx = ix + 30 + col * 165
            by = picker_start_y + row * 75
            btn_rect = pygame.Rect(bx, by, 150, 62)

            is_pinned = app["id"] in self.pinned_apps
            is_hover = btn_rect.collidepoint(mx, my)

            bg_col = (20, 24, 34)
            if is_pinned:
                bg_col = (28, 35, 52)
            if is_hover:
                bg_col = (36, 44, 65)

            border_col = app["color"] if is_pinned else (50, 55, 72)
            pygame.draw.rect(self.ui_surface, bg_col, btn_rect, border_radius=10)
            pygame.draw.rect(self.ui_surface, border_col, btn_rect, 1, border_radius=10)

            # Icon
            icn_rect = pygame.Rect(bx + 10, by + 12, 36, 36)
            self._draw_app_icon(app["icon"], icn_rect, app["color"], height=22)

            # Name
            name_txt = self.font_bold.render(app["name"], True, (240, 245, 255))
            self.ui_surface.blit(name_txt, (bx + 54, by + 14))

            # Status pill: Slot # or Unpinned
            if is_pinned:
                slot_num = self.pinned_apps.index(app["id"]) + 1
                pill_txt = self.font_small.render(f"✓ Slot {slot_num}", True, (50, 215, 120))
            else:
                pill_txt = self.font_small.render("+ Click to add", True, (120, 128, 145))
            self.ui_surface.blit(pill_txt, (bx + 54, by + 34))

        self._draw_main_app_row(ix)

    # -----------------------------------------------------------------------
    # Interactive File Pickup Animation Sequence (Exact user spec)
    # -----------------------------------------------------------------------
    def _draw_file_pickup_mode(self, ix: int, iw: int, ih: int):
        t = self.file_anim_progress
        title_txt = self.font_bold.render("FILE INTAKE & VISUAL PROCESSING", True, (70, 210, 255))
        self.ui_surface.blit(title_txt, (ix + 24, 16))

        info_str = f"File: {self.file_anim_name}  ({self.file_anim_size})"
        itxt = self.font_small.render(info_str, True, (180, 188, 205))
        self.ui_surface.blit(itxt, (ix + 24, 36))

        center_x = ix + iw // 2

        if self.file_anim_state in ("incoming", "slot_open", "intake"):
            # Bot stands in center, acting as letterbox
            bot_x = center_x
            bot_y = 100

            # File position drops down
            file_y = 10 + int(t * 70)
            slot_open_height = 0.0

            if self.file_anim_state == "incoming":
                # Bot looks up at the file!
                self.emotion = "thinking"
            elif self.file_anim_state in ("slot_open", "intake"):
                # Top slot opens on head
                slot_open_height = min(12.0, (t - 0.8) * 20.0)
                if self.file_anim_state == "intake":
                    slot_open_height = max(0.0, 12.0 - (t - 1.6) * 20.0)

            # Draw the Letterbox Bot
            self._draw_avatar(bot_x, bot_y, custom_scale=1.2, slot_open_height=slot_open_height)

            # Draw falling file icon
            if self.file_anim_state != "intake" or t < 1.9:
                f_rect = pygame.Rect(center_x - 18, min(file_y, bot_y - 25), 36, 42)
                pygame.draw.rect(self.ui_surface, (250, 252, 255), f_rect, border_radius=4)
                pygame.draw.rect(self.ui_surface, (70, 140, 255), f_rect, 1, border_radius=4)
                ficn = self.font_small.render("📄", True, (50, 50, 70))
                self.ui_surface.blit(ficn, (center_x - 8, min(file_y, bot_y - 25) + 8))

        elif self.file_anim_state == "jump_left":
            # Bot leaps parabolically from center to far left!
            jump_t = (t - 2.2) / 0.8  # 0.0 to 1.0
            start_x = center_x
            end_x = ix + 45
            current_x = int(start_x + (end_x - start_x) * jump_t)
            # Parabolic height curve
            current_y = int(100 - math.sin(jump_t * math.pi) * 35)
            scale = 1.2 - jump_t * 0.4  # shrinks from 1.2 to 0.8
            self._draw_avatar(current_x, current_y, custom_scale=scale)

        elif self.file_anim_state in ("progress_bar", "completed"):
            # Bot is on progress track moving from left to right with particle trail!
            track_left = ix + 45
            track_right = ix + iw - 60
            track_w = track_right - track_left
            track_y = 110

            pct = min(1.0, (t - 3.0) / 2.5) if self.file_anim_state == "progress_bar" else 1.0
            bot_x = int(track_left + track_w * pct)
            bot_y = track_y

            # 1. Background progress bar track
            track_rect = pygame.Rect(track_left, track_y - 4, track_w, 8)
            pygame.draw.rect(self.ui_surface, (25, 28, 38), track_rect, border_radius=4)

            # 2. Glowing filled progress trail
            fill_rect = pygame.Rect(track_left, track_y - 4, int(track_w * pct), 8)
            pygame.draw.rect(self.ui_surface, (50, 215, 255), fill_rect, border_radius=4)

            # 3. Particle sparks streaming from behind the bot runner
            for p in self.file_trail_particles:
                p["alpha"] -= 0.03
                px = bot_x - int((1.0 - p["alpha"]) * 40)
                py = track_y + int(math.sin(time.time() * 20.0 + p["alpha"] * 10) * 8)
                if p["alpha"] > 0:
                    pygame.draw.circle(self.ui_surface, (80, 220, 255), (px, py), 2)

            self.file_trail_particles = [p for p in self.file_trail_particles if p["alpha"] > 0]

            # 4. Draw shrunk runner bot head moving across
            self._draw_avatar(bot_x, bot_y, custom_scale=0.85)

            # 5. Percentage readout
            pct_str = f"Parsing Document... {int(pct * 100)}%"
            ptxt = self.font_mono.render(pct_str, True, (80, 220, 255))
            self.ui_surface.blit(ptxt, (ix + 45, track_y - 28))

    # -----------------------------------------------------------------------
    # Action Stream HUD Mode
    # -----------------------------------------------------------------------
    def _draw_action_mode(self, ix: int, iw: int, ih: int):
        avatar_cx = ix + 36
        avatar_cy = 38
        self._draw_avatar(avatar_cx, avatar_cy)

        # Title
        name_txt = self.font_title.render("PHOENIX MK4", True, (255, 255, 255))
        self.ui_surface.blit(name_txt, (ix + 64, 15))

        # Step Badge
        step_rect = pygame.Rect(ix + iw - 130, 16, 92, 24)
        pygame.draw.rect(self.ui_surface, (25, 42, 70), step_rect, border_radius=6)
        pygame.draw.rect(self.ui_surface, (70, 130, 240), step_rect, 1, border_radius=6)
        step_txt = self.font_bold.render(f"Step {self.current_step}/{self.max_steps}", True, (100, 200, 255))
        self.ui_surface.blit(step_txt, (step_rect.x + 8, step_rect.y + 3))

        # Reasoning: a collapsed dropdown in the header.  The full text lives in
        # its own panel above the agent's answer, and only when it is opened.
        self._draw_thought_chip(ix, iw)

        # Action Box (Live Needle Command - Solid dark container with high contrast)
        # it stops short of the shortcut column, so the grid stays visible
        grid_origin = self._grid_origin()
        body_w = iw - 40 - (190 if grid_origin else 0)
        act_box = pygame.Rect(ix + 20, 68, body_w, 58)
        pygame.draw.rect(self.ui_surface, (20, 24, 34), act_box, border_radius=8)
        pygame.draw.rect(self.ui_surface, (60, 72, 98), act_box, 1, border_radius=8)

        lbl = self.font_badge.render("EXECUTING ACTION", True, (50, 255, 140))
        self.ui_surface.blit(lbl, (act_box.x + 14, act_box.y + 8))

        action_str = self.current_action or "Inspecting visual DOM elements..."
        if len(action_str) > 55:
            action_str = action_str[:55] + "..."
        at = self.font_mono.render(action_str, True, (255, 255, 255))
        self.ui_surface.blit(at, (act_box.x + 14, act_box.y + 28))

        # the shortcuts never disappear while the bot is working
        if grid_origin:
            self._draw_shortcut_grid()

        # Chat panel below the action box: 120 px tall so the whole turn fits,
        # wrapped and scrollable (mouse wheel over it, or PageUp / PageDown)
        entries = self.chat_lines or ([("agent", self.current_speech)]
                                      if self.current_speech else [])
        top = act_box.bottom + 8
        if self.thoughts_open:
            thought_box = self._draw_thought_panel(
                pygame.Rect(ix + 20, top, iw - 40, 190))
            top = thought_box.bottom + 8
        if entries:
            chat_box = pygame.Rect(ix + 20, top, iw - 40, 120)
            self._chat_rect = chat_box
            pygame.draw.rect(self.ui_surface, (22, 28, 44), chat_box, border_radius=8)
            pygame.draw.rect(self.ui_surface, (65, 110, 190), chat_box, 1, border_radius=8)

            badge = self.font_badge.render("AGENT", True, (120, 190, 255))
            self.ui_surface.blit(badge, (chat_box.x + 14, chat_box.y + 6))

            self._draw_chat_lines(chat_box, entries)

    def _ellipsize(self, text: str, font, max_w: int):
        """Cut text to fit max_w, ending with an ellipsis.

        The old reasoning line cut at 65 CHARACTERS, which chopped a word in
        half and then still ran past the edge of the header when the bot
        thought in long words.  Cutting to the pixel width instead means the
        chip can never overflow, whatever the text.
        """
        text = (text or "").strip()
        if not text:
            return ""
        if font.size(text)[0] <= max_w:
            return text
        ell = "..."
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if font.size(text[:mid] + ell)[0] <= max_w:
                lo = mid
            else:
                hi = mid - 1
        return text[:lo].rstrip() + ell

    def _thought_chip_rect(self, ix: int, iw: int) -> pygame.Rect:
        """The collapsed reasoning chip.  One place, so the chip you see is
        the chip you click.

        It starts at ix+76 to clear the avatar hit box (ix+16 .. ix+68) and
        stops short of the step badge, so neither can swallow the click.
        """
        x = ix + 76
        limit = ix + iw - 138                      # step badge starts at iw-130
        return pygame.Rect(x, 36, max(120, limit - x), 20)

    def _draw_thought_chip(self, ix: int, iw: int):
        """The closed reasoning chip: a label, a preview, and a chevron.

        It says how long the thought is even when folded, so the user knows
        there is something behind it - which is the whole point of a
        dropdown rather than a line that silently appears and disappears.
        """
        rect = self._thought_chip_rect(ix, iw)
        self._thought_rect = None
        mx, my = self.get_cursor_pos()
        hover = rect.collidepoint(mx, my)
        thought = (self.current_thought or "").strip()

        pygame.draw.rect(self.ui_surface,
                         (26, 34, 52) if hover else (20, 26, 40),
                         rect, border_radius=6)
        pygame.draw.rect(self.ui_surface,
                         (120, 180, 255) if hover else (58, 76, 110),
                         rect, 1, border_radius=6)

        chev = "v" if self.thoughts_open else ">"
        tag = self.font_badge.render(f"REASONING {chev}", True, (120, 180, 255))
        self.ui_surface.blit(tag, (rect.x + 8, rect.centery - tag.get_height() // 2))
        # reserve room for the character count FIRST, or the preview runs
        # underneath it and the two overlap into an unreadable smear
        count = None
        right_edge = rect.right - 6
        if thought:
            count = self.font_small.render(f"{len(thought)}c", True, (110, 124, 148))
            self.ui_surface.blit(count, (right_edge - count.get_width(),
                                         rect.centery - count.get_height() // 2))
            right_edge -= count.get_width() + 10
        left_edge = rect.x + 12 + tag.get_width()
        room = max(20, right_edge - left_edge)
        body = self._ellipsize(thought or "nothing yet", self.font_small, room)
        body_s = self.font_small.render(body, True, (215, 228, 248))
        self.ui_surface.blit(body_s, (left_edge,
                                      rect.centery - body_s.get_height() // 2))

    def _draw_thought_panel(self, box: pygame.Rect) -> pygame.Rect:
        """The OPEN reasoning panel: the whole thought, wrapped and scrollable.

        It sits directly ABOVE the agent's answer, which is where the eye
        already goes for "what is it doing" - the answer stays below it,
        never behind it.
        """
        self._thought_rect = box
        thought = (self.current_thought or "").strip()
        pygame.draw.rect(self.ui_surface, (19, 25, 38), box, border_radius=8)
        pygame.draw.rect(self.ui_surface, (90, 130, 200), box, 1, border_radius=8)

        badge = self.font_badge.render("REASONING", True, (140, 195, 255))
        self.ui_surface.blit(badge, (box.x + 14, box.y + 6))
        hint = self.font_small.render("click the chip to fold it away", True,
                                      (110, 124, 148))
        self.ui_surface.blit(hint, (box.right - hint.get_width() - 14,
                                    box.y + 8))

        inner_w = box.width - 28
        line_h = self.font_regular.get_linesize()
        flow = self._wrap_text(thought or "(no reasoning to show)", self.font_regular,
                               inner_w)
        clip = pygame.Rect(box.x + 2, box.y + 24, box.width - 4, box.height - 26)
        total_h = len(flow) * line_h
        self._thoughts_max_scroll = max(0, total_h - clip.height)
        if self.thought_scroll > self._thoughts_max_scroll:
            self.thought_scroll = float(self._thoughts_max_scroll)
        self.ui_surface.set_clip(clip)
        y = box.y + 26 - self.thought_scroll
        for ln in flow:
            if y + line_h >= clip.top and y <= clip.bottom:
                self.ui_surface.blit(
                    self.font_regular.render(ln, True, (206, 222, 244)),
                    (box.x + 14, int(y)))
            y += line_h
        self.ui_surface.set_clip(None)
        if self._thoughts_max_scroll > 0:
            track = pygame.Rect(box.right - 10, box.y + 26, 4, clip.height)
            pygame.draw.rect(self.ui_surface, (40, 52, 78), track, border_radius=2)
            thumb_h = max(16, int(track.h * clip.height / max(1, total_h)))
            t = self.thought_scroll / self._thoughts_max_scroll
            ty = track.y + int((track.h - thumb_h) * t)
            pygame.draw.rect(self.ui_surface, (110, 170, 255),
                             pygame.Rect(track.x, ty, 4, thumb_h), border_radius=2)
        return box

    def toggle_thoughts(self, open_=None):
        """Fold the reasoning away, or unfold it.

        Opening grows the capsule so the panel gets real room instead of
        stealing it from the answer underneath - the island already animates
        its own height, so this costs nothing extra.
        """
        self.thoughts_open = (not self.thoughts_open) if open_ is None \
            else bool(open_)
        if self.thoughts_open:
            self.thought_scroll = 0.0
            if not self.expanded_open and not self.file_anim_progress:
                self.target_width = 720.0
                self.target_height = 470.0
                self.target_radius = 30.0
        else:
            if not self.expanded_open and not self.file_anim_progress:
                self.target_height = 294.0
        return self.thoughts_open

    def _scroll_thoughts(self, delta: float):
        self.thought_scroll = max(
            0.0, min(float(self._thoughts_max_scroll),
                     self.thought_scroll + delta))

    def _wrap_text(self, text: str, font, max_w: int):
        """Word-wrap into lines that fit max_w pixels."""
        words = (text or "").split()
        if not words:
            return [""]
        lines, cur = [], words[0]
        for w in words[1:]:
            trial = cur + " " + w
            if font.size(trial)[0] <= max_w:
                cur = trial
            else:
                lines.append(cur)
                cur = w
        lines.append(cur)
        return lines

    def _draw_chat_lines(self, box: pygame.Rect, entries):
        """Wrap the turn's chat into the panel, scroll clamped to the text."""
        pad = 14
        inner_w = box.width - pad * 2 - 10          # right gap for the scrollbar
        line_h = self.font_regular.get_linesize()
        flow = []
        for role, text in entries:
            col = (140, 200, 255) if role == "you" else (232, 238, 250)
            for ln in self._wrap_text(text, self.font_regular, inner_w):
                flow.append((col, ln))
        if not flow:
            return
        view = box.height - 26                       # below the AGENT badge
        total_h = len(flow) * line_h
        self._chat_max_scroll = max(0, total_h - view)
        if self.chat_scroll > self._chat_max_scroll:
            self.chat_scroll = float(self._chat_max_scroll)
        clip = pygame.Rect(box.x + 2, box.y + 22, box.width - 4, box.height - 24)
        self.ui_surface.set_clip(clip)
        y = box.y + 26 - self.chat_scroll
        for col, ln in flow:
            if y + line_h >= clip.top and y <= clip.bottom:
                self.ui_surface.blit(self.font_regular.render(ln, True, col),
                                     (box.x + pad, int(y)))
            y += line_h
        self.ui_surface.set_clip(None)
        if self._chat_max_scroll > 0:                # scrollbar thumb
            track = pygame.Rect(box.right - 9, box.y + 26, 4, view)
            pygame.draw.rect(self.ui_surface, (40, 52, 78), track, border_radius=2)
            thumb_h = max(16, int(track.h * view / max(1, total_h)))
            t = self.chat_scroll / self._chat_max_scroll
            ty = track.y + int((track.h - thumb_h) * t)
            pygame.draw.rect(self.ui_surface, (90, 145, 230),
                             pygame.Rect(track.x, ty, 4, thumb_h), border_radius=2)

    # -----------------------------------------------------------------------
    # Interactive Chat Input Mode
    # -----------------------------------------------------------------------
    def _draw_input_mode(self, ix: int, iw: int, ih: int):
        avatar_cx = ix + 36
        avatar_cy = 38
        self._draw_avatar(avatar_cx, avatar_cy)

        title = self.font_bold.render("Command Phoenix MK4", True, (240, 245, 255))
        self.ui_surface.blit(title, (ix + 64, 18))

        sub = self.font_small.render("Type natural language instruction. Press Enter to dispatch.", True, (150, 158, 175))
        self.ui_surface.blit(sub, (ix + 64, 38))

        grid_origin = self._grid_origin()
        box = pygame.Rect(ix + 20, 64, iw - 40 - (190 if grid_origin else 0), 42)
        pygame.draw.rect(self.ui_surface, (20, 23, 32), box, border_radius=8)
        pygame.draw.rect(self.ui_surface, (70, 150, 255), box, 1, border_radius=8)

        disp_text = self.input_text or "Ask me anything (e.g., 'summarize invoice.pdf', 'open spotify and play jazz')..."
        col = (245, 248, 255) if self.input_text else (110, 118, 135)
        txt = self.font_regular.render(disp_text, True, col)
        self.ui_surface.blit(txt, (box.x + 12, box.y + 12))

        if self.input_active and self.cursor_blink < 0.5:
            cx = box.x + 12 + self.font_regular.size(self.input_text)[0] + 2
            pygame.draw.line(self.ui_surface, (70, 150, 255), (cx, box.y + 10), (cx, box.y + 32), 2)

        if grid_origin:
            self._draw_shortcut_grid()

# ---------------------------------------------------------------------------
# Standalone Execution Entry Point with Windows Crash-Shield
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("\n" + "="*58)
    print("  PHOENIX MK4 DYNAMIC ISLAND HUD (ModernGL + Pygame)")
    print("="*58)
    print("• Borderless floating Dynamic Island HUD at top-center of screen")
    print("• Top-Right ⚙️ Icon: Click to choose which 4 app shortcuts appear in grid")
    print("• App Grid: WhatsApp, Spotify, YouTube, Chrome, Snapchat, Discord, etc.")
    print("• Avatar: Real-time mouse eye-tracking & 9 expressive emotion states")
    print("• File Pickup: Drag & drop real files to watch the Letterbox intake!")
    print("• Press Enter to type commands, or press 'T' to test file pickup")
    print("• Press keys 1-9 to toggle emotions: Happy, Loving, Thinking, Error, etc.")
    print("• Press ESC to exit")
    print("="*58 + "\n")

    def mock_on_user_input(text):
        print(f"\n[USER COMMAND RECEIVED] -> {text}")
        island_app.set_state("acting")
        island_app.set_thought(f"Processing command: '{text}'")
        island_app.set_action(f"Orchestrating tools for '{text}'", step=1)
        island_app.set_speech(f"On it! Executing: {text}")

    try:
        island_app = PhoenixDynamicIslandOverlay(
            on_user_input=mock_on_user_input,
            on_interrupt=lambda: print("[INTERRUPT TRIGGERED]")
        )
        island_app.run()
    except Exception as e:
        err = traceback.format_exc()
        print("\nFATAL ERROR DURING RUN:")
        print(err)
        log_debug(f"FATAL: {err}")
        if IS_WINDOWS:
            try:
                ctypes.windll.user32.MessageBoxW(0, f"Phoenix Island crashed:\n{e}\n\nSee phoenix_island.log", "Phoenix Island Error", 0x10)
            except Exception:
                pass
        input("\nPress Enter to exit...")
