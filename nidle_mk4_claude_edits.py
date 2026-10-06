"""
Needle 4 - Mark IV Desktop Automation Agent (Powered by Cactus Needle)
Executes atomic real-time actions from the PHOENIX brain via PyAutoGUI and Needle 3 routing.
Optimized for Phoenix Dynamic Island (M.PY) & Desktop Assistant.
"""

import os
import re
import sys
import time
import ctypes
try:
    import pyautogui
except Exception:
    class _MockPyAutoGUI:
        FAILSAFE = False
        PAUSE = 0.15
        @staticmethod
        def size():
            return (1920, 1080)
        @staticmethod
        def moveTo(*a, **kw):
            pass
        @staticmethod
        def click(*a, **kw):
            pass
        @staticmethod
        def write(*a, **kw):
            pass
        @staticmethod
        def press(*a, **kw):
            pass
        @staticmethod
        def hotkey(*a, **kw):
            pass
        @staticmethod
        def scroll(*a, **kw):
            pass
    pyautogui = _MockPyAutoGUI()

try:
    import needle
except Exception:
    class _MockNeedleModule:
        @staticmethod
        def tool(*a, **kw):
            if len(a) == 1 and callable(a[0]) and not kw:
                return a[0]
            def dec(f):
                return f
            return dec
        class Needle:
            def __init__(self, *a, **kw):
                pass
            def reset(self):
                pass
            def run(self, *a, **kw):
                return {"results": []}
            def extract(self, *a, **kw):
                return {}
    needle = _MockNeedleModule()

import webbrowser
import subprocess

# pyautogui hard-depends on pyperclip, so clipboard paste is always available.
# It is the only way to type non-ASCII / newlines reliably (pyautogui.write
# silently drops any character not on the US layout).
try:
    import pyperclip
except ImportError:
    pyperclip = None

try:
    import wikipedia
except ImportError:
    wikipedia = None

try:
    import winrt.windows.ui.notifications as notifications
except ImportError:
    notifications = None

pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0.15

def _enable_dpi_awareness():
    try:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

_enable_dpi_awareness()
SCREEN_WIDTH, SCREEN_HEIGHT = pyautogui.size()

_active_dom = []

def set_active_dom(dom: list):
    global _active_dom
    _active_dom = dom

# ---------------------------------------------------------------------------
# Core PyAutoGUI Tools
# ---------------------------------------------------------------------------

@needle.tool
def click_coordinate(x: int, y: int):
    """Click the mouse at the specified (x, y) coordinates on the screen."""
    x = max(0, min(x, SCREEN_WIDTH - 1))
    y = max(0, min(y, SCREEN_HEIGHT - 1))
    
    try:
        pyautogui.moveTo(x, y, duration=0.4)
        time.sleep(0.1)
        pyautogui.click()
        return f"Mouse clicked at ({x}, {y})"
    except Exception as e:
        return f"Failed to click: {e}"

@needle.tool
def click_element_id(element_id: int):
    """Click the mouse on an element by its ID (as provided by the vision parser)."""
    if not _active_dom or element_id < 0 or element_id >= len(_active_dom):
        return f"Failed to click: element id {element_id} not found in current screen."
        
    el = _active_dom[element_id]
    bb = el.get("bbox")
    if not bb or len(bb) < 4:
        return f"Failed to click: element id {element_id} has no valid bounds."
        
    x = int((float(bb[0]) + float(bb[2])) / 2.0 * SCREEN_WIDTH)
    y = int((float(bb[1]) + float(bb[3])) / 2.0 * SCREEN_HEIGHT)
    return click_coordinate(x, y)

@needle.tool
def move_mouse(x: int, y: int):
    """Move the mouse cursor to the specified (x, y) coordinates on the screen without clicking."""
    x = max(0, min(x, SCREEN_WIDTH - 1))
    y = max(0, min(y, SCREEN_HEIGHT - 1))
    
    try:
        pyautogui.moveTo(x, y, duration=0.4)
        return f"Cursor moved to ({x}, {y})"
    except Exception as e:
        return f"Failed to move cursor: {e}"

# pyautogui.write() silently drops anything outside the US printable-ASCII
# layout and cannot type newlines/tabs at all. Those strings go through the
# clipboard instead, so a message with an apostrophe, an emoji, or a line break
# is delivered verbatim instead of truncated or garbled.
_NEEDS_CLIPBOARD = re.compile(r"[^\x20-\x7e]")

def _type_verbatim(text: str):
    if pyperclip is None:
        pyautogui.write(text, interval=0.02)
        return
    saved = None
    try:
        saved = pyperclip.paste()
    except Exception:
        pass
    pyperclip.copy(text)
    time.sleep(0.05)
    pyautogui.hotkey("ctrl", "v")
    if saved is not None:
        try:
            pyperclip.copy(saved)
        except Exception:
            pass

@needle.tool
def type_text(text: str, press_enter: bool = False):
    """Type the provided text string using the keyboard. If press_enter is true, press the enter key afterwards."""
    try:
        if _NEEDS_CLIPBOARD.search(text or ""):
            _type_verbatim(text)
        else:
            pyautogui.write(text, interval=0.02)
        if press_enter:
            time.sleep(0.08)
            pyautogui.press("enter")
            return f"Typed '{text}' and pressed enter"
        return f"Typed '{text}'"
    except Exception as e:
        return f"Failed to type: {e}"

# Model output says "enter"/"esc"/"page down"; pyautogui wants its own names.
_KEY_ALIASES = {
    "return": "enter", "esc": "escape", "escape": "escape", "spacebar": "space",
    "pgup": "pgup", "pgdn": "pgdn", "pageup": "pgup", "pagedown": "pgdn",
    "win": "win", "windows": "win", "cmd": "win", "super": "win",
    "ctrl": "ctrl", "control": "ctrl", "alt": "alt", "shift": "shift",
    "del": "delete", "ins": "insert", "backspace": "backspace", "tab": "tab",
}

@needle.tool
def press_key(key: str):
    """Press a keyboard key or hotkey combination (e.g. 'enter', 'win', 'ctrl+c')."""
    # Simple normalization for modifiers
    key = key.lower().replace("windows", "win").replace("return", "enter")
    parts = [_KEY_ALIASES.get(k.strip(), k.strip()) for k in key.split("+")]
    parts = [k for k in parts if k]

    try:
        if not parts:
            return "Failed to press key: empty key spec"
        if len(parts) > 1:
            pyautogui.hotkey(*parts)
        else:
            pyautogui.press(parts[0])
        return f"Pressed key: {key}"
    except Exception as e:
        return f"Failed to press key {key}: {e}"

@needle.tool
def scroll(amount: int):
    """Scroll the mouse wheel up (positive amount) or down (negative amount)."""
    try:
        pyautogui.scroll(amount)
        return f"Scrolled by {amount}"
    except Exception as e:
        return f"Failed to scroll: {e}"

@needle.tool
def write_file(filename: str, content: str):
    """Write text content to a local file."""
    try:
        with open(filename, "w", encoding="utf-8") as f:
            f.write(content.replace('\\n', '\n'))
        return f"Successfully wrote to {filename}"
    except Exception as e:
        return f"Failed to write file {filename}: {e}"

# ---------------------------------------------------------------------------
# Advanced Tools (Apps, OS, Internet)
# ---------------------------------------------------------------------------

try:
    import win32gui
except ImportError:
    win32gui = None

def _foreground_title() -> str:
    """Title of the current foreground window ('' if unknown)."""
    if not win32gui:
        return ""
    try:
        return win32gui.GetWindowText(win32gui.GetForegroundWindow()) or ""
    except Exception:
        return ""

def _wait_for_foreground_change(before: str, timeout: float = 2.5, poll: float = 0.2):
    """Block until the foreground window title differs from `before` or timeout."""
    if not win32gui:
        return "", False
    deadline = time.time() + timeout
    title = before
    while time.time() < deadline:
        time.sleep(poll)
        title = _foreground_title()
        if title and title != before:
            return title, True
    return title, False


@needle.tool
def launch_app(app_name: str):
    """Launch an application or shortcut manually via Windows Start menu.
    
    Workflow:
      1. Press Windows / Start button to open Start Menu and focus Search.
      2. Type the application name or shortcut name into the search bar.
      3. Wait for Windows 11 search indexer to highlight the shortcut or web search.
      4. Press Enter to launch the app/shortcut (or trigger Windows 11 web search).
    """
    clean = (app_name or "").strip()
    if not clean:
        return "Failed to launch app: empty name."

    # Manual Windows Start menu search and launch workflow
    try:
        before = _foreground_title()

        # Step 1: Press the Start button (Windows key)
        pyautogui.press('win')
        time.sleep(0.45)

        # Step 2: Search for the app name or shortcut in the Start menu
        if _NEEDS_CLIPBOARD.search(clean):
            _type_verbatim(clean)
        else:
            pyautogui.write(clean, interval=0.035)

        # Give Windows 11 search indexer time to locate the shortcut or app
        time.sleep(0.55)

        # Step 3: Press Enter to open the shortcut/app or trigger Windows web search
        pyautogui.press('enter')

        title, ok = _wait_for_foreground_change(before, timeout=3.0)
        if ok and title:
            return f"Opened '{clean}' via Start menu search (foreground: '{title}')."
        return f"Searched and opened '{clean}' via Windows Start menu."
    except Exception as e:
        return f"Failed to launch app '{app_name}' via Start menu: {e}"


@needle.tool
def surf_website(url: str):
    """Open a website URL in the default web browser."""
    if not url.startswith("http"):
        url = "https://" + url
    try:
        webbrowser.open(url)
        return f"Opened browser to {url}"
    except Exception as e:
        return f"Failed to open website: {e}"

@needle.tool
def wikipedia_search(query: str):
    """Search Wikipedia for a topic and return a short summary."""
    if not wikipedia:
        return "Wikipedia module not installed."
    try:
        summary = wikipedia.summary(query, sentences=3)
        return f"Wikipedia summary for '{query}': {summary}"
    except wikipedia.exceptions.DisambiguationError as e:
        return f"Disambiguation error: {e.options[:5]}"
    except wikipedia.exceptions.PageError:
        return "Page not found."
    except Exception as e:
        return f"Failed to search Wikipedia: {e}"

def _toast_text(xml: str) -> str:
    """Extract human-readable <text> nodes from toast XML payload."""
    texts = re.findall(r"<text[^>]*>(.*?)</text>", xml or "", re.S | re.I)
    parts = []
    for t in texts:
        t = re.sub(r"<[^>]+>", " ", t)
        t = t.strip()
        t = re.sub(r"\s+", " ", t)
        if t:
            parts.append(t)
    return " | ".join(parts[:3]) if parts else ""

@needle.tool
def read_notifications():
    """Read recent Windows toast notifications from the action center."""
    if not notifications:
        return "winrt.windows.ui.notifications module not installed."

    manager = notifications.ToastNotificationManager
    toasts = None
    last_err = None

    try:
        toasts = manager.history.get_history()
    except Exception:
        toasts = None

    if toasts is None or not toasts:
        common_aumids = []
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Notifications\Settings") as key:
                count = winreg.QueryInfoKey(key)[0]
                for i in range(count):
                    common_aumids.append(winreg.EnumKey(key, i))
        except Exception as e:
            last_err = e

        for aumid in common_aumids:
            try:
                per = manager.history.get_history_with_id(aumid)
                if per:
                    toasts = list(toasts or []) + [(aumid, p) for p in per]
            except Exception:
                pass

    if toasts is None or not toasts:
        if last_err is not None:
            return f"Toast notification history unavailable ({last_err}). No notifications were readable."
        return "No recent notifications found."
        
    notif_list = []
    for item in toasts:
        if isinstance(item, tuple):
            raw_app, t = item
        else:
            raw_app = 'unknown'
            t = item
            
        app = raw_app
        if "!" in app:
            app = app.split("!")[-1]
        app = app.replace("https://www.", "").replace("http://www.", "").replace("https://", "").replace("http://", "")
        if app.endswith("/web"):
            app = app[:-4]
        if app.startswith("YourPhoneNotifications_com."):
            app = app.replace("YourPhoneNotifications_com.", "")
        
        if not app:
            app = "unknown"

        try:
            xml = t.content.get_xml() if hasattr(t.content, 'get_xml') else str(t)
        except Exception:
            xml = ""
        summary = _toast_text(xml)
        notif_list.append(f"[{app}] {summary}" if summary else f"[{app}] (toast, no text)")

    seen = set()
    unique = []
    for line in notif_list:
        if line in seen:
            continue
        seen.add(line)
        unique.append(line)
    return "\n".join(unique) if unique else "No recent notifications found."

# ---------------------------------------------------------------------------
# Needle Router Initialization
# ---------------------------------------------------------------------------

needle_router = needle.Needle(tools=[
    click_coordinate,
    click_element_id,
    move_mouse,
    type_text,
    press_key,
    scroll,
    launch_app,
    surf_website,
    wikipedia_search,
    read_notifications
])

# ---------------------------------------------------------------------------
# Intent Router Initialization
# ---------------------------------------------------------------------------

SYSTEM_INTENT = (
    "Use the single intent tool that matches the user request. "
    "To open or launch an app, use extract_launch_intent. "
    "To play, watch, or find a named song, video, or content, use "
    "extract_search_intent. "
    "For jokes, greetings, and small talk there is no tool: emit no tool call."
)

@needle.tool(triggers=["send", "message", "text", "introduce yourself", "reply"])
def extract_messaging_intent(contact: str, message: str = "Hello from Phoenix MK3", app: str = "whatsapp"):
    """Send a written message to a named person or business contact on a messaging platform (WhatsApp, Snapchat, etc.)."""
    return {"driver": "messaging", "contact": contact, "payload": message, "app": app}

@needle.tool(triggers=["open", "launch", "start"])
def extract_launch_intent(app_name: str):
    """Open a desktop application when opening the app is the entire request."""
    return {"driver": "launch", "app": app_name}

@needle.tool(triggers=["play", "search for", "find", "watch", "listen to"])
def extract_search_intent(query: str):
    """The user wants to play, watch, or find specific content by name in an app: a song, video, playlist, or title."""
    return {"driver": "search", "query": query}

@needle.tool(triggers=["notifications"])
def extract_notification_intent():
    """Extract intent to read, check, or list system notifications."""
    return {"driver": "notifications"}

@needle.tool(triggers=["pause", "mute", "unmute", "volume", "next track", "previous track"])
def extract_media_intent(command: str):
    """Control playback that is already running: pause, resume, mute, volume, next or previous track."""
    return {"driver": "media", "command": command}

@needle.tool(triggers=["wikipedia"])
def extract_wikipedia_intent(topic: str):
    """Extract intent to search for a topic on Wikipedia or ask what Wikipedia says about a topic."""
    return {"driver": "wikipedia", "query": topic}

@needle.tool(triggers=["google", "weather", "news", "search the web"])
def extract_web_intent(query: str):
    """Extract intent to search the web or google for a query."""
    return {"driver": "google", "query": query}

intent_parser = needle.Needle(tools=[
    extract_messaging_intent, 
    extract_launch_intent, 
    extract_search_intent,
    extract_notification_intent,
    extract_media_intent,
    extract_wikipedia_intent,
    extract_web_intent
], system=SYSTEM_INTENT)

# ---------------------------------------------------------------------------
# Deterministic dispatcher
# ---------------------------------------------------------------------------

def _normalize_key(key: str) -> str:
    k = (key or "").lower().strip()
    k = re.sub(r"\bpress\s+", "", k)
    k = re.sub(r"\s+and\s+", "+", k)
    if " " in k and "+" not in k:
        parts = k.split()
        if len(parts) > 1:
            k = "+".join(parts)
    k = re.sub(r"\s+", "", k)
    return k

_FAILURE_PREFIX = ("failed", "blocked", "rejected", "not found", "unknown",
                   "error", "disambig")

def _ok(results: str) -> bool:
    return not str(results or "").lower().startswith(_FAILURE_PREFIX)

_TYPE_RE = re.compile(
    r"type\s+([\"'])(.+?)\1\s*(?:and\s+)?(?:press\s+(?:enter|return)\b|and\s+enter\b)?\s*$",
    re.IGNORECASE | re.DOTALL)

def _parse_type_command(task_clean: str):
    """Return (text, press_enter) for a canonical `type "..."` command, else None."""
    m = _TYPE_RE.search(task_clean)
    if not m:
        m2 = re.search(r"^\s*type\s+(.+)$", task_clean, re.IGNORECASE | re.DOTALL)
        if not m2:
            return None
        body = m2.group(1).strip()
        body = re.sub(r"^(?:\"|')", "", body)
        body = re.sub(r"(?:\"|')\s*(?:and\s+)?(?:press\s+)?(?:enter|return)\s*$", "", body,
                      flags=re.IGNORECASE)
        body = body.strip().strip("\"'")
        if not body:
            return None
        press = bool(re.search(r"press\s+(?:enter|return)\b|and\s+enter\b",
                               task_clean, re.IGNORECASE))
        return body, press
    text = m.group(2).strip()
    press = bool(re.search(r"press\s+(?:enter|return)\b|and\s+enter\b",
                           task_clean, re.IGNORECASE))
    return text, press

def execute_task(task_description: str):
    """Deterministic regex dispatch for atomic actions."""
    task_clean = (task_description or "").strip()
    if not task_clean:
        return {"confidence": 0.0, "success": False, "results": "Empty action."}

    # type "..." (checked before click so text containing 'click' isn't hijacked)
    typed = _parse_type_command(task_clean)
    if typed:
        text, press_spec = typed
        r = type_text(text, press_enter=press_spec)
        return {"confidence": 1.0, "success": _ok(r), "results": r}

    # media control (standalone: play/pause/next/prev/volume/mute)
    media_match = re.fullmatch(
        r"(?:press\s+)?(?:play|pause|playpause|play/pause|resume|toggle play|"
        r"next(?:\s+song|\s+track)?|prev(?:ious)?(?:\s+song|\s+track)?|"
        r"volume\s?up|volume\s?down|raise\s+volume|lower\s+volume|"
        r"mute|unmute)",
        task_clean, re.I)
    if media_match:
        verb = media_match.group(0).strip().lower()
        verb = re.sub(r"^press\s+", "", verb)
        verb = re.sub(r"\s+", "", verb)
        verb = verb.replace("toggle", "").replace("/", " ")
        verb_key = ""
        if verb.startswith("play") or verb in ("pause", "resume"):
            verb_key = "playpause"
        elif "next" in verb or "nexttrack" in verb:
            verb_key = "nexttrack"
        elif "prev" in verb:
            verb_key = "prevtrack"
        elif "volume" in verb or "raisevolume" in verb:
            verb_key = "volumeup" if ("up" in verb or "raise" in verb) else "volumedown"
        elif verb in ("mute", "unmute"):
            verb_key = "volumemute"
        if not verb_key:
            return {"confidence": 0.0, "success": False,
                    "results": f"UNKNOWN_MEDIA_COMMAND: {task_clean}"}
        r = press_key(verb_key)
        return {"confidence": 1.0, "success": _ok(r), "results": r}

    # wikipedia lookup
    wiki_match = re.search(
        r"\b(?:search|look up|lookup|check|ask)\s+(?:the\s+)?(?:on\s+)?wikipedia\s+"
        r"(?:for\s+)?[\"']?([^\"']+?)[\"']?\s*$", task_clean, re.I)
    if not wiki_match:
        wiki_match = re.search(
            r"\bwikipedia\s+(?:search\s+)?[\"']?([^\"']+?)[\"']?\s*$", task_clean, re.I)
    if wiki_match:
        query = wiki_match.group(1).strip()
        if query:
            r = wikipedia_search(query)
            return {"confidence": 1.0, "success": _ok(r), "results": r}

    # youtube search / play (deterministic URL, opens browser instantly)
    yt_q = None
    m = re.search(r"(?:play|watch|listen to)\s+[\"']?(.+?)[\"']?\s+on\s+youtube\s*$", task_clean, re.I) or \
        re.search(r"search\s+(?:for\s+)?[\"']?(.+?)[\"']?\s+on\s+youtube\s*$", task_clean, re.I) or \
        re.search(r"youtube\s+(?:search\s+)?(?:for\s+)?[\"']?(.+?)[\"']?\s*$", task_clean, re.I) or \
        re.search(r"(?:play|watch)\s+[\"']?(.+?)[\"']?\s+youtube\s*$", task_clean, re.I)
    if m:
        yt_q = m.group(1).strip()
    if yt_q:
        from urllib.parse import quote_plus
        url = "https://www.youtube.com/results?search_query=" + quote_plus(yt_q)
        r = surf_website(url)
        return {"confidence": 1.0, "success": _ok(r), "results": r}

    # google / web search (deterministic URL, no browser DOM needed)
    google_q = None
    m = re.search(r"google\s+(?:search\s+)?(?:for\s+)?[\"']?([^\"']+?)[\"']?\s*$",
                  task_clean, re.I)
    if m:
        google_q = m.group(1).strip()
    if not google_q:
        m = re.search(r"search\s+(?:for\s+)?[\"']?([^\"']+?)[\"']?\s+on\s+google\s*$",
                      task_clean, re.I) or \
            re.search(r"search\s+(?:google\s+)(?:for\s+)?[\"']?([^\"']+?)[\"']?\s*$",
                      task_clean, re.I) or \
            re.search(r"(?:search|look up|lookup|find)\s+(?:the\s+)?(?:web|internet)\s+"
                      r"(?:for\s+)?[\"']?([^\"']+?)[\"']?\s*$",
                      task_clean, re.I)
        if m:
            google_q = m.group(1).strip()
    if google_q:
        from urllib.parse import quote_plus
        url = "https://www.google.com/search?q=" + quote_plus(google_q)
        r = surf_website(url)
        return {"confidence": 1.0, "success": _ok(r), "results": r}

    # surf a website / open a URL in the default browser
    site_match = re.fullmatch(
        r"(?:open|surf|browse|go to|visit|navigate to)\s+"
        r"(?:the\s+)?(?:website|site|url|page|webpage)?[\s:]*"
        r"[\"']?((?:https?://)?[a-z0-9][a-z0-9.\-]*(?:\.[a-z]{2,12})"
        r"(?:[/:][^\s\"']*)?)[\"']?\s*", task_clean, re.I)
    if site_match:
        url = site_match.group(1).strip()
        if url:
            r = surf_website(url)
            return {"confidence": 1.0, "success": _ok(r), "results": r}

    # notifications
    notif_match = re.search(r"\bread\s+(?:my\s+|windows\s+|toast\s+)*notifications?\b"
                            r"|\b(?:check|show|open)\s+(?:my\s+|windows\s+|toast\s+)*"
                            r"notifications?\b|\bnotifications?\b$", task_clean, re.I)
    if notif_match:
        r = read_notifications()
        return {"confidence": 1.0, "success": _ok(r), "results": r}

    # click / move (pixels or normalized 0..1)
    click_match = re.search(r"(?:click|move)[^\d]{0,16}(\d{1,4}(?:\.\d+)?)[,\s]+(\d{1,4}(?:\.\d+)?)",
                            task_clean, re.IGNORECASE)
    if click_match:
        x_val, y_val = float(click_match.group(1)), float(click_match.group(2))
        if 0.0 <= x_val <= 1.0 and 0.0 <= y_val <= 1.0:
            cx, cy = int(x_val * SCREEN_WIDTH), int(y_val * SCREEN_HEIGHT)
        else:
            cx, cy = int(x_val), int(y_val)
        if cy <= 5 or cy >= SCREEN_HEIGHT - 5:
            return {"confidence": 0.1, "success": False,
                    "results": f"REJECTED: ({cx},{cy}) is on the edge of the screen."}
        r = click_coordinate(cx, cy)
        return {"confidence": 1.0, "success": _ok(r), "results": r}

    # click element id N (uses the active DOM snapshot)
    id_match = re.match(r"click\s+(?:id|icon|element|elem)\s*[:=]?\s*(\d+)", task_clean, re.IGNORECASE)
    if id_match:
        r = click_element_id(int(id_match.group(1)))
        return {"confidence": 1.0, "success": _ok(r), "results": r}

    # launch / open app
    launch_match = re.match(r"^(?:launch|open)\s+(?:app\s+)?(.+?)$", task_clean, re.IGNORECASE)
    if launch_match:
        app_target = launch_match.group(1).strip()
        r = launch_app(app_target)
        return {"confidence": 1.0, "success": _ok(r), "results": r}

    # close app/window/tab
    close_match = re.match(r"^(?:close|quit|exit)\s+(?:app|window|tab|browser|program)(?:\s+(.+))?$", task_clean, re.IGNORECASE)
    if close_match:
        if "tab" in task_clean.lower():
            return {"confidence": 1.0, "success": True, "results": press_key("ctrl+w")}
        else:
            return {"confidence": 1.0, "success": True, "results": press_key("alt+f4")}

    # press key / hotkey
    press_match = re.match(r"^press\s+(.+)$", task_clean, re.IGNORECASE)
    if press_match:
        norm_k = _normalize_key(press_match.group(1))
        if "win+r" in norm_k:
            return {"confidence": 0.0, "success": False,
                    "results": "BLOCKED: Win+R (Run dialog) is forbidden."}
        r = press_key(press_match.group(1))
        return {"confidence": 1.0, "success": _ok(r), "results": r}

    # scroll
    scroll_match = re.match(r"^scroll\s+(-?\d+)$", task_clean, re.IGNORECASE)
    if scroll_match:
        r = scroll(int(scroll_match.group(1)))
        return {"confidence": 1.0, "success": _ok(r), "results": r}

    # write file
    write_match = re.match(r'^write\s+file\s+["\']([^"\']+)["\']\s+with\s+content\s+["\']([\s\S]+)["\']$', task_clean, re.IGNORECASE)
    if write_match:
        r = write_file(write_match.group(1), write_match.group(2))
        return {"confidence": 1.0, "success": _ok(r), "results": r}

    return {"confidence": 0.0, "success": False, "results": f"UNKNOWN_ACTION: {task_clean}"}


if __name__ == "__main__":
    assert _parse_type_command('type "it\'s fine" and press enter') == ("it's fine", True)
    assert _parse_type_command('type "hello"') == ("hello", False)
    assert _parse_type_command('type "line1\nline2"') == ("line1\nline2", False)
    assert _parse_type_command('type hello there') == ("hello there", False)
    assert _parse_type_command("click at 5 5") is None
    assert _ok("Mouse clicked at (5, 5)") is True
    assert _ok("Failed to click: boom") is False
    assert _ok("BLOCKED: Win+R") is False
    assert execute_task("press win+r")["success"] is False
    assert "UNKNOWN_ACTION" in execute_task("frobnicate the widgets")["results"]
    print("nidle_mk4_claude_edits self-check OK")
