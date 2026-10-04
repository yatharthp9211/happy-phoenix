"""
Needle 4 - Mark IV Desktop Automation Agent (Powered by Cactus Needle)
Executes atomic real-time actions from the PHOENIX brain via PyAutoGUI and Needle 3 routing.
"""

import os
import re
import sys
import time
import ctypes
import pyautogui
import needle
import webbrowser
import subprocess

try:
    import wikipedia
except ImportError:
    wikipedia = None

try:
    import winrt.windows.ui.notifications as notifications
except ImportError:
    notifications = None

pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0.2

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
        pyautogui.moveTo(x, y, duration=0.7)
        time.sleep(0.15)
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
        pyautogui.moveTo(x, y, duration=0.7)
        return f"Cursor moved to ({x}, {y})"
    except Exception as e:
        return f"Failed to move cursor: {e}"

@needle.tool
def type_text(text: str, press_enter: bool = False):
    """Type the provided text string using the keyboard. If press_enter is true, press the enter key afterwards."""
    try:
        pyautogui.write(text, interval=0.03)
        if press_enter:
            time.sleep(0.1)
            pyautogui.press("enter")
            return f"Typed '{text}' and pressed enter"
        return f"Typed '{text}'"
    except Exception as e:
        return f"Failed to type: {e}"

@needle.tool
def press_key(key: str):
    """Press a keyboard key or hotkey combination (e.g. 'enter', 'win', 'ctrl+c')."""
    # Simple normalization for modifiers
    key = key.lower().replace("windows", "win").replace("return", "enter")
    parts = [k.strip() for k in key.split("+")]
    
    try:
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

# ---------------------------------------------------------------------------
# Advanced Tools (Apps, OS, Internet)
# ---------------------------------------------------------------------------

@needle.tool
def launch_app(app_name: str):
    """Launch an application on Windows by its name."""
    try:
        pyautogui.press('win')
        time.sleep(0.5)
        pyautogui.write(app_name, interval=0.03)
        time.sleep(0.5)
        pyautogui.press('enter')
        return f"Launch command sent for '{app_name}' via Start menu"
    except Exception as e:
        return f"Failed to launch app: {e}"

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
    """Extract the human-readable <text> nodes from a toast XML payload
    (title + body lines), so the bot never sees raw toast XML."""
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

    # Strategy 1: toast history for the calling app.
    try:
        toasts = manager.history.get_history()
    except Exception as e:
        last_err = e

    # Strategy 2: per-app history for common apps (get_history_with needs an
    # AUMID; the blanket get_history() can raise "Element not found" when the
    # process is not a registered toast-enabled app).
    if toasts is None or not toasts:
        common_aumids = (
            "Microsoft.Windows.Explorer",
            "Microsoft.Office.Outlook",
            "MicrosoftTeams",
            "Spotify.exe",
            "whatsapp",
            "org.telegram.telegrammessenger",
        )
        for aumid in common_aumids:
            try:
                per = manager.history.get_history_with(aumid)
                if per:
                    toasts = list(toasts or []) + list(per)
            except Exception as e:
                last_err = e

    if toasts is None or not toasts:
        reason = f" ({last_err})" if last_err else ""
        return (f"Toast notification history unavailable on this system{reason}. "
                "No notifications were readable.")

    notif_list = []
    for t in toasts:
        app = getattr(t, 'app_user_model_id', 'unknown') or 'unknown'
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

@needle.tool
def extract_messaging_intent(contact: str, message: str = "Hello from Phoenix MK3", app: str = "whatsapp"):
    """Send a message, text, or introduce yourself to a person/contact on a messaging platform (WhatsApp, Snapchat, etc.)."""
    return {"driver": "messaging", "contact": contact, "payload": message, "app": app}

@needle.tool
def extract_launch_intent(app_name: str):
    """Open or launch a desktop application."""
    return {"driver": "launch", "app": app_name}

@needle.tool
def extract_search_intent(query: str):
    """Search for a specific song, video, title, or query within an app (e.g. play a song on YouTube, search on Spotify)."""
    return {"driver": "search", "query": query}

intent_parser = needle.Needle(tools=[extract_messaging_intent, extract_launch_intent, extract_search_intent])

# ---------------------------------------------------------------------------
# Deterministic dispatcher (bypasses the LLM router: see problems.md resolved #1)
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

def execute_task(task_description: str):
    """Deterministic regex dispatch for atomic actions. Mirrors nidle_mk3 but
    calls THIS module's pyautogui primitives so the 3B model never depends on
    the LLM-based needle_router for a known command (fast + reliable)."""
    task_clean = (task_description or "").strip()
    if not task_clean:
        return {"confidence": 0.0, "success": False, "results": "Empty action."}

    # type "..." (checked before click so text containing 'click' isn't hijacked)
    type_match = re.search(r"type\s+[\"']([^\"']+)[\"']", task_clean, re.IGNORECASE)
    if type_match:
        text = type_match.group(1)
        press_spec = re.search(r"press\s+(enter|return)\b|and\s+enter\b", task_clean, re.IGNORECASE)
        return {"confidence": 1.0, "success": True,
                "results": type_text(text, press_enter=bool(press_spec))}

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
        return {"confidence": 1.0, "success": True, "results": press_key(verb_key)}

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
            return {"confidence": 1.0, "success": True,
                    "results": wikipedia_search(query)}

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
        return {"confidence": 1.0, "success": True, "results": surf_website(url)}

    # surf a website / open a URL in the default browser
    site_match = re.fullmatch(
        r"(?:open|surf|browse|go to|visit|navigate to)\s+"
        r"(?:the\s+)?(?:website|site|url|page|webpage)?[\s:]*"
        r"[\"']?((?:https?://)?[a-z0-9][a-z0-9.\-]*(?:\.[a-z]{2,12})"
        r"(?:[/:][^\s\"']*)?)[\"']?\s*", task_clean, re.I)
    if site_match:
        url = site_match.group(1).strip()
        if url:
            return {"confidence": 1.0, "success": True,
                    "results": surf_website(url)}

    # notifications
    notif_match = re.search(r"\bread\s+(?:my\s+|windows\s+|toast\s+)*notifications?\b"
                            r"|\b(?:check|show|open)\s+(?:my\s+|windows\s+|toast\s+)*"
                            r"notifications?\b|\bnotifications?\b$", task_clean, re.I)
    if notif_match:
        return {"confidence": 1.0, "success": True,
                "results": read_notifications()}

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
        return {"confidence": 1.0, "success": True, "results": click_coordinate(cx, cy)}

    # click element id N (uses the active DOM snapshot)
    id_match = re.match(r"click\s+(?:id|icon|element|elem)\s*[:=]?\s*(\d+)", task_clean, re.IGNORECASE)
    if id_match:
        return {"confidence": 1.0, "success": True,
                "results": click_element_id(int(id_match.group(1)))}

    # launch / open app
    launch_match = re.match(r"^(?:launch|open)\s+(?:app\s+)?(.+?)$", task_clean, re.IGNORECASE)
    if launch_match:
        return {"confidence": 1.0, "success": True,
                "results": launch_app(launch_match.group(1).strip())}

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
        return {"confidence": 1.0, "success": True,
                "results": press_key(press_match.group(1))}

    # scroll
    scroll_match = re.match(r"^scroll\s+(-?\d+)$", task_clean, re.IGNORECASE)
    if scroll_match:
        return {"confidence": 1.0, "success": True,
                "results": scroll(int(scroll_match.group(1)))}

    return {"confidence": 0.0, "success": False, "results": f"UNKNOWN_ACTION: {task_clean}"}
