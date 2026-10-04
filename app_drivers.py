"""AppDriver interface (problems.md #1: fragile app interactions).

Abstracts the deterministic, out-of-Loop interaction logic for fragile apps
(YouTube, Snapchat, WhatsApp) into an `AppDriver` base with specialized
subclasses. Each subclass owns one app's geometric + semantic fallback
heuristics and polls a phase-bound state machine. Every action routed through
the orchestrator gate + `_dispatch_proposal` (secure, stale-safe); drivers never
call the LLM and never type blind after a fixed sleep.

Lifecycle contract for `step()`:
  True  -> the driver performed ONE step; the caller should re-observe the
           screen and re-enter the interaction loop.
  False -> the driver owns nothing right now (or gave up); the caller may let
           the LLM take the turn.
"""

from control_loop import VALID


class DriverHost:
    """Inject all live bot state/callbacks. bot_mk7 wires this in one place so
    drivers stay unit-testable and never read bot globals directly."""

    # --- mutable state (shared references) ---
    ts = None        # task_state dict
    dom = None       # current observation elements (list)

    # --- callables bound by bot_mk7 ---
    def gate(self, action):
        return (None, None)

    def dispatch(self, proposal, action):
        return {"success": False, "results": "no host"}

    def click_target(self, action, dom):
        raise NotImplementedError

    def best_result(self, dom, query):
        raise NotImplementedError

    def settle(self, min_s, max_s):
        pass

    def reenter(self):
        pass

    def speak(self, text):
        pass

    def append(self, role, content):
        pass

    def prune(self):
        pass

    def log(self, section, message):
        pass

    def publish(self, source, event_type, level, payload):
        pass

    def transcript(self, role, content=None, meta=None):
        pass

    def sleep(self, secs):
        pass


class AppDriver:
    """Abstract base for the deterministic app drivers."""

    name = "generic"
    phase = "pre"   # drivers run before the semantic-annotation pass by default

    def matches(self, ts):
        """Return True if THIS driver owns the current task state."""
        return False

    def step(self, host):
        raise NotImplementedError

    # ---- shared helpers ----
    def _log(self, host, message):
        host.log("TASK", f"[{self.name.upper()}-DRIVER] {message}")

    def _gate_dispatch(self, host, action):
        """Gate then dispatch; returns res dict, or None if refused/failed."""
        gv, gprop = host.gate(action)
        if gv != VALID:
            self._log(host, f"refused ({gv}): {action}")
            return None
        try:
            return host.dispatch(gprop, action)
        except Exception as e:
            self._log(host, f"exec error: {e}")
            return None

    def _finish(self, host, system_msg, speak_text):
        ts = host.ts
        ts["task_done"] = True
        ts["active"] = False
        host.append("user", system_msg)
        host.prune()
        host.settle(1.0, 6.0)
        try:
            host.speak(speak_text)
        except Exception:
            pass
        host.reenter()
        return True


class SearchDriver(AppDriver):
    """Generic search/play task driver (backstop for non-YouTube apps)."""

    name = "search"

    def matches(self, ts):
        return bool(ts.get("active")
                    and ts.get("expected_text")
                    and ts.get("target_launched")
                    and ts.get("driver") != "messaging")

    def _find_search_input(self, dom):
        best_input, best_any = None, None
        for i, el in enumerate(dom):
            t = (el.get("type") or "").strip().lower()
            c = (el.get("content") or "").strip().lower()
            if t == "input" and "search" in c:
                return i
            if t == "input" and best_input is None:
                best_input = i
            if t in ("search", "searchbox") and best_any is None:
                best_any = i
            if "what do you want to play" in c:
                return i
        if best_input is not None:
            return best_input
        if best_any is not None:
            return best_any
        for i, el in enumerate(dom):
            t = (el.get("type") or "").strip().lower()
            c = (el.get("content") or "").strip().lower()
            if t == "search" or "search" in c:
                if best_any is None:
                    best_any = i
                if len(c.split()) <= 3:
                    return i
        return best_any

    def _find_result(self, host, dom, query):
        return host.best_result(dom, query)

    def step(self, host):
        ts = host.ts
        if not ts.get("active"):
            return False
        et = ts.get("expected_text")
        if not et or not ts.get("target_launched"):
            return False
        dom = host.dom()
        if not dom:
            return False

        phase = None
        if not ts.get("search_clicked"):
            idx = self._find_search_input(dom)
            if idx is None:
                return False
            coords = host.click_target(f"click id {idx}", dom)
            if not coords:
                return False
            action = f"click at {coords[0]} {coords[1]}"
            phase = "search_clicked"
        elif not ts.get("searched"):
            action = f'type "{et}" and press enter'
            phase = "searched"
        elif not ts.get("result_clicked"):
            idx = self._find_result(host, dom, et)
            if idx is None:
                retries = ts.get("search_retries", 0)
                if retries >= 3:
                    self._log(host, "giving up waiting for results; LLM takes over.")
                    return False
                ts["search_retries"] = retries + 1
                self._log(host, f"no result yet (retry {retries + 1}/3), waiting.")
                host.sleep(2.0)
                host.reenter()
                return True
            coords = host.click_target(f"click id {idx}", dom)
            if not coords:
                return False
            action = f"click at {coords[0]} {coords[1]}"
            phase = "result_clicked"
        else:
            ts["task_done"] = True
            ts["active"] = False
            self._log(host, f"task complete: '{et}'.")
            host.append("user", (
                f"[System: The search/play task for '{et}' completed: the result was "
                "clicked and is now playing. Do not perform further actions.]"))
            host.prune()
            host.settle(1.0, 6.0)
            try:
                host.speak(f"Playing {et} now.")
            except Exception:
                pass
            host.reenter()
            return True

        ts["steps"] += 1
        if ts["steps"] > ts["max_steps"]:
            return False
        self._log(host, f"executing: {action}")
        host.publish("action", "needle_dispatch", "INFO", {"task": action})
        res = self._gate_dispatch(host, action)
        if res is None:
            return False
        ts[phase] = True
        success = bool(res.get("success")) if isinstance(res, dict) else True
        res_str = res.get("results") if isinstance(res, dict) else str(res)
        status = "SUCCESS" if success else "FAILED_OR_UNKNOWN"
        self._log(host, f"result: {res_str}")
        host.append("assistant", f"<ACTION>{action}</ACTION>")
        host.append("user", (
            f"[System: Task driver executed '{action}'. Status: {status}. "
            f"Result: {res_str}. Re-evaluate the screen and continue the "
            "search/play task.]"))
        host.prune()
        host.transcript("system", f"[action] {action} -> {res_str}",
                        {"status": status, "driver": True})
        host.settle(1.0, 6.0)
        host.reenter()
        return True


class YouTubeDriver(SearchDriver):
    """YouTube-specific search/play driver.

    Extra geometric heuristics beyond the generic driver: the YouTube search box
    sits in the top band of the main content column (not the address/browser
    chrome), so a wide input above y < 0.3 is a strong candidate even when the
    omni-parser reports it as a plain 'input' with no 'search' label."""
    name = "youtube"

    def matches(self, ts):
        if not super().matches(ts):
            return False
        app = (ts.get("target_app") or "").lower()
        return "youtube" in app

    def _find_search_input(self, dom):
        idx = super()._find_search_input(dom)
        if idx is not None:
            return idx
        best_candidate = None
        best_area = 0.0
        for i, el in enumerate(dom):
            t = (el.get("type") or "").strip().lower()
            bbox = el.get("bbox", [0, 0, 0, 0])
            if len(bbox) != 4:
                continue
            x, y = float(bbox[0]), float(bbox[1])
            w = float(bbox[2]) - float(bbox[0])
            h = float(bbox[3]) - float(bbox[1])
            if t in ("input", "searchbox", "search") and y < 0.3 and x < 0.8:
                area = w * h
                if area > best_area:
                    best_area = area
                    best_candidate = i
        return best_candidate

    def _find_result(self, host, dom, query):
        idx = host.best_result(dom, query)
        if idx is not None:
            return idx
        # Geometric fallback: YouTube's first video card is a large central box
        # below the filter row (y between ~0.25 and ~0.6), left-aligned.
        best_candidate = None
        best_area = 0.0
        for i, el in enumerate(dom):
            t = (el.get("type") or "").strip().lower()
            c = (el.get("content") or "").strip()
            if not c or t in ("input", "search", "button"):
                continue
            bbox = el.get("bbox", [0, 0, 0, 0])
            if len(bbox) != 4:
                continue
            y = float(bbox[1])
            w = float(bbox[2]) - float(bbox[0])
            h = float(bbox[3]) - float(bbox[1])
            if 0.25 <= y <= 0.7 and h > 0.08:
                area = w * h
                if area > best_area:
                    best_area = area
                    best_candidate = i
        return best_candidate


class BaseMessagingDriver(AppDriver):
    """Phase-bound messaging state machine (FIND_CONTACT -> COMPOSE [+SEND]).

    Subclasses override the finders with app-specific geometry/semantics and
    choose whether the send button must be clicked (Snapchat) or Enter is used
    (WhatsApp).
    """

    name = "messaging"
    uses_enter = True
    phase = "post"   # messaging roles need the semantic-annotation pass first

    def matches(self, ts):
        return bool(ts.get("driver") == "messaging"
                    and ts.get("active")
                    and ts.get("target_contact")
                    and ts.get("message_payload"))

    # ---- override points ----
    def _find_search_input(self, dom):
        best_candidate = None
        best_y = 1.0
        for i, el in enumerate(dom):
            t = (el.get("type") or "").strip().lower()
            c = (el.get("content") or "").strip().lower()
            if "channel" in c:
                continue
            role = el.get("role")
            bbox = el.get("bbox", [0, 0, 0, 0])
            if len(bbox) == 4:
                x, y = float(bbox[0]), float(bbox[1])
                w = float(bbox[2]) - float(bbox[0])
                h = float(bbox[3]) - float(bbox[1])
                if x < 0.45 and y < 0.3:
                    if role == "search_input" or "search" in c or t in ("search", "searchbox"):
                        return i
                    if t == "input" and w > 2 * h:
                        if y < best_y:
                            best_y = y
                            best_candidate = i
        return best_candidate

    def _find_contact_result(self, dom):
        target_contact = (self._ts or {}).get("target_contact")
        best_id = None
        for i, el in enumerate(dom):
            c = (el.get("content") or "").strip().lower()
            if "channel" in c:
                continue
            if target_contact and target_contact.lower() in c:
                if "ask my ai" in c or "my ai" in c:
                    continue
                if el.get("role") == "conversation_list_item":
                    return i
                if best_id is None:
                    best_id = i
        return best_id

    def _find_compose_input(self, dom):
        input_id = None
        best_y = -1
        for i, el in enumerate(dom):
            t = (el.get("type") or "").strip().lower()
            c = (el.get("content") or "").strip().lower()
            bbox = el.get("bbox", [0, 0, 0, 0])
            if len(bbox) == 4:
                y = float(bbox[1])
                is_candidate = (
                    el.get("role") == "message_input"
                    or t in ("input", "textbox", "textarea")
                    or c in ("type a message", "message", "type a message...",
                             "type here", "send a chat")
                    or "type a message" in c or "send a chat" in c
                )
                if is_candidate and y > best_y:
                    best_y = y
                    input_id = i
        if input_id is not None:
            return input_id
        # Geometric fallback: many chat apps render the composer as a
        # contenteditable <div> that OmniParser tags type 'text'/'div' with
        # EMPTY content (placeholder not captured). It is a WIDE box hugging
        # the bottom of the main panel. Pick the bottom-most wide box with
        # little-to-no content (chat message bubbles are ruled out by the
        # content-length guard).
        best_id = None
        best_cy = -1.0
        for i, el in enumerate(dom):
            t = (el.get("type") or "").strip().lower()
            c = (el.get("content") or "").strip()
            bbox = el.get("bbox", [0, 0, 0, 0])
            if len(bbox) != 4:
                continue
            x0, y0 = float(bbox[0]), float(bbox[1])
            x1, y1 = float(bbox[2]), float(bbox[3])
            w = x1 - x0
            h = y1 - y0
            cx = (x0 + x1) / 2.0
            cy = (y0 + y1) / 2.0
            if w <= 0 or h <= 0:
                continue
            if cy < 0.78 or cx < 0.30 or w < 0.20 or h > 0.30:
                continue
            if t in ("button", "icon", "send", "menu", "option", "tab"):
                continue
            if t == "text" and len(c) > 6 and not c.lower().startswith(("send a", "type a", "message")):
                continue
            if cy > best_cy:
                best_cy = cy
                best_id = i
        return best_id

    def _find_send_button(self, dom):
        return None

    # ---- failure handling ----
    def _give_up_messaging(self, host, reason):
        """Cleanly end a messaging task that cannot make progress.

        Returns True (turn consumed) so the caller never hands a locked phase
        to the LLM - the capability gate refuses LLM actions during
        COMPOSE/SEND, which would otherwise deadlock the loop."""
        ts = host.ts
        contact = ts.get("target_contact", "")
        payload = ts.get("message_payload", "")
        ts["task_done"] = True
        ts["active"] = False
        self._log(host, f"giving up: {reason}")
        host.append("user", (
            f"[System: The messaging task to '{contact}' with payload "
            f"'{payload}' failed deterministically because {reason}. Do not "
            "perform further actions.]"))
        host.prune()
        host.settle(1.0, 6.0)
        try:
            host.speak(f"Could not complete the message to {contact}.")
        except Exception:
            pass
        host.reenter()
        return True

    # ---- state machine ----
    def step(self, host):
        ts = host.ts
        self._ts = ts
        if ts.get("driver") != "messaging":
            print(f"[DEBUG-DRIVER] Failed on driver != messaging (was {ts.get('driver')})")
            return False
        if not ts.get("active"):
            print(f"[DEBUG-DRIVER] Failed on active == {ts.get('active')}")
            return False
        dom = host.dom()
        if not dom:
            print(f"[DEBUG-DRIVER] Failed on not dom (dom is {dom})")
            return False
        target_contact = ts.get("target_contact")
        payload = ts.get("message_payload")
        if not target_contact or not payload:
            print(f"[DEBUG-DRIVER] Failed on missing contact/payload ({target_contact}, {payload})")
            return False
        phase = ts.get("messaging_phase", "FIND_CONTACT")
        self._log(host, f"Current phase: {phase} for '{target_contact}'")

        if phase in ("FIND_CONTACT", "VERIFY_CONTACT"):
            chat_header_id = None
            for i, el in enumerate(dom):
                if (el.get("role") == "chat_header"
                        and target_contact.lower() in (el.get("content") or "").lower()):
                    chat_header_id = i
                    break
            if chat_header_id is not None:
                ts["messaging_phase"] = "COMPOSE"
                phase = "COMPOSE"
                self._log(host, "Verified chat header. Transitioning to COMPOSE.")

        if phase == "COMPOSE":
            input_id = self._find_compose_input(dom)
            if input_id is None:
                retries = ts.get("compose_retries", 0)
                if retries >= 3:
                    ts["compose_retries"] = 0
                    return self._give_up_messaging(
                        host, "the message input box could not be found on the screen.")
                ts["compose_retries"] = retries + 1
                self._log(host, f"compose input not found (retry {retries + 1}/3), waiting.")
                host.sleep(2.0)
                host.reenter()
                return True
            ts["compose_retries"] = 0
            coords = host.click_target(f"click id {input_id}", dom)
            if not coords:
                return self._give_up_messaging(
                    host, "the message input box could not be located.")
            action = f"click at {coords[0]} {coords[1]}"
            if not ts.get("compose_clicked"):
                res = self._gate_dispatch(host, action)
                if res is None:
                    return self._give_up_messaging(
                        host, "the compose click was refused by the orchestrator.")
                ts["compose_clicked"] = True
                host.append("assistant", f"<ACTION>{action}</ACTION>")
                host.append("user", (
                    f"[System: {self.name} driver clicked the compose input: "
                    f"{res}. Re-observe the screen, then type the message.]"))
                host.prune()
                host.settle(0.5, 4.0)
                host.reenter()
                return True
            # Second pass: input focused on the CURRENT screen.
            if self.uses_enter:
                type_cmd = f'type "{payload}" and press enter'
                res = self._gate_dispatch(host, type_cmd)
                if res is None:
                    ts["compose_clicked"] = False
                    return self._give_up_messaging(
                        host, "typing the message was refused by the orchestrator.")
                ts["compose_clicked"] = False
                return self._finish(
                    host,
                    f"[System: The messaging task to '{target_contact}' "
                    "completed deterministically. Do not perform further "
                    "actions.]",
                    f"Sent message to {target_contact}.")
            else:
                type_cmd = f'type "{payload}"'
                res = self._gate_dispatch(host, type_cmd)
                if res is None:
                    ts["compose_clicked"] = False
                    return self._give_up_messaging(
                        host, "typing the message was refused by the orchestrator.")
                ts["compose_clicked"] = False
                ts["messaging_phase"] = "SEND"
                host.append("assistant", f"<ACTION>{type_cmd}</ACTION>")
                host.append("user", (
                    "[System: Message text is in the composer. Now find "
                    "the send button and click it.]"))
                host.prune()
                host.settle(0.5, 4.0)
                host.reenter()
                return True

        if phase == "SEND":
            send_id = self._find_send_button(dom)
            if send_id is None:
                fallback = "press enter"
                self._log(host, "no send button found; pressing Enter as fallback.")
                res = self._gate_dispatch(host, fallback)
                if res is None:
                    return self._give_up_messaging(
                        host, "no send button was found and the Enter fallback was refused.")
                return self._finish(
                    host,
                    f"[System: The messaging task to '{target_contact}' completed "
                    "deterministically (Enter fallback). Do not perform further "
                    "actions.]",
                    f"Sent message to {target_contact}.")
            coords = host.click_target(f"click id {send_id}", dom)
            if not coords:
                return self._give_up_messaging(
                    host, "the send button could not be located.")
            action = f"click at {coords[0]} {coords[1]}"
            res = self._gate_dispatch(host, action)
            if res is None:
                return self._give_up_messaging(
                    host, "the send-button click was refused by the orchestrator.")
            return self._finish(
                host,
                f"[System: The messaging task to '{target_contact}' completed "
                "deterministically (send button clicked). Do not perform further "
                "actions.]",
                f"Sent message to {target_contact}.")

        if phase == "FIND_CONTACT":
            if not ts.get("contact_search_typed"):
                idx = self._find_search_input(dom)
                if idx is not None:
                    coords = host.click_target(f"click id {idx}", dom)
                    if coords:
                        if not ts.get("contact_search_clicked"):
                            action = f"click at {coords[0]} {coords[1]}"
                            self._log(host, f"FIND_CONTACT search click on id {idx}")
                            res = self._gate_dispatch(host, action)
                            if res is None:
                                return False
                            ts["contact_search_clicked"] = True
                            host.settle(0.5, 4.0)
                            host.reenter()
                            return True
                        if not ts.get("contact_search_typed"):
                            action = f'type "{target_contact}" and press enter'
                            self._log(host, f"FIND_CONTACT typing '{target_contact}'")
                            res = self._gate_dispatch(host, action)
                            if res is None:
                                ts["contact_search_clicked"] = False
                                return False
                            ts["contact_search_typed"] = True
                            ts["contact_search_clicked"] = False
                            host.sleep(1.5)
                            host.reenter()
                            return True
            else:
                best_id = self._find_contact_result(dom)
                if best_id is not None:
                    coords = host.click_target(f"click id {best_id}", dom)
                    if coords:
                        self._log(host, f"Forcing FIND_CONTACT result click on id {best_id}")
                        action = f"click at {coords[0]} {coords[1]}"
                        res = self._gate_dispatch(host, action)
                        if res is None:
                            return False
                        ts["contact_search_typed"] = False
                        ts["search_retries"] = 0
                        ts["messaging_phase"] = "COMPOSE"
                        host.sleep(1.5)
                        host.reenter()
                        return True
                else:
                    retries = ts.get("search_retries", 0)
                    if retries >= 3:
                        self._log(host, "giving up waiting for contact result; LLM takes over.")
                        ts["search_retries"] = 0
                        host.append("user", "[System: The driver could not find the contact in the search results. They may not exist, or the UI is slow. Please evaluate the screen to decide how to proceed.]")
                        host.prune()
                        return False
                    ts["search_retries"] = retries + 1
                    self._log(host, f"no contact result yet (retry {retries + 1}/3), waiting.")
                    host.sleep(2.0)
                    host.reenter()
                    return True

        return False


class WhatsAppDriver(BaseMessagingDriver):
    """WhatsApp messaging: search box in the top-left panel (x<0.45, y<0.3),
    chat list items in the left rail, compose input at the very bottom of the
    main chat column, message sent with Enter."""
    name = "whatsapp"
    uses_enter = True

    def matches(self, ts):
        if not super().matches(ts):
            return False
        app = (ts.get("target_app") or "").lower()
        return "snapchat" not in app


class SnapchatDriver(BaseMessagingDriver):
    """Snapchat messaging.

    Snapchat's desktop UI differs from WhatsApp: a left rail of conversations
    with the "New Chat"/search field at the very top (|x| < 0.7, y < 0.3), a
    composer sitting low in the main panel, and a paper-plane SEND button to the
    right of the composer (Enter does not reliably send). Geometric + semantic
    fallbacks mirror that layout."""
    name = "snapchat"
    uses_enter = False

    def matches(self, ts):
        if not super().matches(ts):
            return False
        app = (ts.get("target_app") or "").lower()
        hint = (ts.get("goal_hint") or "").lower()
        return "snapchat" in app or "snapchat" in hint

    def _find_search_input(self, dom):
        idx = super()._find_search_input(dom)
        if idx is not None:
            return idx
        best_candidate = None
        best_score = -1.0
        for i, el in enumerate(dom):
            t = (el.get("type") or "").strip().lower()
            c = (el.get("content") or "").strip().lower()
            role = el.get("role")
            bbox = el.get("bbox", [0, 0, 0, 0])
            if len(bbox) != 4:
                continue
            x, y = float(bbox[0]), float(bbox[1])
            w = float(bbox[2]) - float(bbox[0])
            h = float(bbox[3]) - float(bbox[1])
            if x >= 0.4 or y >= 0.25:
                continue
            # Snapchat web's "New Chat" field is a wide pill near the top of
            # the left rail OR the bold "New Chat" button that opens the
            # search overlay. Accept either, preferring smaller-X leftmost.
            is_field = (role == "search_input"
                        or t in ("input", "search", "searchbox", "textbox")
                        or "search" in c or "new chat" in c)
            if not is_field:
                continue
            score = -x
            if w > 1.5 * h:          # wide pill beats a compact icon
                score += 0.5
            if "search" in c or "new chat" in c:
                score += 0.25
            if score > best_score:
                best_score = score
                best_candidate = i
        return best_candidate

    def _find_compose_input(self, dom):
        idx = super()._find_compose_input(dom)
        if idx is not None:
            return idx
        # Snapchat web: composer is the bottom-most WIDE box in the MAIN chat
        # column (x > 0.4). Never reach into the left rail (x < 0.4), where the
        # search field and conversation items live, and ignore the tiny
        # send/emoji icons squatting at the very bottom-right.
        best_id = None
        best_cy = -1.0
        for i, el in enumerate(dom):
            t = (el.get("type") or "").strip().lower()
            c = (el.get("content") or "").strip()
            bbox = el.get("bbox", [0, 0, 0, 0])
            if len(bbox) != 4:
                continue
            x0, y0 = float(bbox[0]), float(bbox[1])
            x1, y1 = float(bbox[2]), float(bbox[3])
            w = x1 - x0
            h = y1 - y0
            cx = (x0 + x1) / 2.0
            cy = (y0 + y1) / 2.0
            if w <= 0 or h <= 0:
                continue
            if cx < 0.40 or cy < 0.72 or w < 0.18 or h > 0.30:
                continue
            if t in ("button", "icon", "send", "menu", "option", "tab"):
                continue
            if t == "text" and len(c) > 6 and not c.lower().startswith(("send a", "type a", "message")):
                continue
            if cy > best_cy:
                best_cy = cy
                best_id = i
        return best_id

    def _find_send_button(self, dom):
        # 1. Explicit roles / text matches (any x; Snapchat's pill can sit wide)
        for i, el in enumerate(dom):
            c = (el.get("content") or "").strip().lower()
            role = el.get("role")
            bbox = el.get("bbox", [0, 0, 0, 0])
            x = float(bbox[0]) if len(bbox) == 4 else 0.0
            y = float(bbox[1]) if len(bbox) == 4 else 0.0
            if role == "send_button":
                return i
            if c and x > 0.4 and any(k in c for k in
                    ("send", "paper plane", "folded plane", "chat to", "paperplane", "plane")):
                return i

        # 2. Geometric: Snapchat web shows a circular SEND button just to the
        # RIGHT of the composer once text is typed. It is the RIGHT-MOST small
        # round icon in the bottom-right cluster ([Send] -> [Emoji] -> [Camera]
        # -> [GIF]), NOT the left-most (that would be the emoji/smile).
        exclude = ("emoji", "smile", "smiley", "camera", "gallery", "gif",
                   "sticker", "sticker pack", "voice", "audio", "attach",
                   "attachment", "bitmoji", "map", "music", "video call",
                   "capture", "meme")
        best = None
        best_x = -1.0
        for i, el in enumerate(dom):
            bbox = el.get("bbox", [0, 0, 0, 0])
            if len(bbox) != 4:
                continue
            x0, y0 = float(bbox[0]), float(bbox[1])
            x1, y1 = float(bbox[2]), float(bbox[3])
            w = x1 - x0
            h = y1 - y0
            cx = (x0 + x1) / 2.0
            cy = (y0 + y1) / 2.0
            if cx <= 0.5 or cy < 0.75 or w <= 0 or h <= 0:
                continue
            # send circle is small and roughly square
            if w > 0.16 or h > 0.16:
                continue
            t = (el.get("type") or "").strip().lower()
            c = (el.get("content") or "").strip().lower()
            if t == "text" or (c and len(c) > 3):
                continue
            if any(k in c for k in exclude):
                continue
            if cx > best_x:
                best_x = cx
                best = i
        return best


class InfoDriver(AppDriver):
    """Non-GUI deterministic drivers: notifications, media keys, wikipedia,
    google / web search. They carry no DOM dependency; step() dispatches a
    known atomic command and hands the result back to the loop so the LLM can
    answer from real system data instead of hallucinating."""

    name = "info"
    phase = "pre"

    def matches(self, ts):
        return bool(ts.get("active") and ts.get("driver") in
                    ("notifications", "media", "wikipedia", "google"))

    def _run(self, host, action, intro):
        ts = host.ts
        res = self._gate_dispatch(host, action)
        if res is None:
            ts["task_done"] = True
            ts["active"] = False
            self._log(host, f"refused to dispatch '{action}'")
            host.append("user", "[System: The deterministic command was refused "
                                "and no result is available. Do not fabricate one.]")
            host.prune()
            host.settle(1.0, 6.0)
            host.reenter()
            return True
        res_str = res.get("results") if isinstance(res, dict) else str(res)
        status = "SUCCESS" if (isinstance(res, dict) and res.get("success")) else "UNCERTAIN"
        self._log(host, f"result: {res_str}")
        
        is_google = ts.get("driver") == "google"
        if not is_google:
            ts["task_done"] = True
            ts["active"] = False
            host.append("user", (
                f"[System: {intro} Deterministic result ({status}): {res_str}. "
                "Use that real result to answer; do not perform further actions.]"))
        else:
            # For web searches, hand off to the LLM so the Vision model
            # can actually explore the resulting webpage.
            ts["driver"] = None
            ts["target_app"] = "browser"
            ts["target_launched"] = True
            ts["expected_text"] = None  # Free exploration!
            host.append("user", (
                f"[System: {intro} Deterministic result ({status}): {res_str}. "
                "The browser is now open. Continue the task by interacting with the UI.]"))

        host.prune()
        host.settle(1.0, 6.0)
        try:
            host.speak("Done.")
        except Exception:
            pass
        host.reenter()
        return True

    def step(self, host):
        ts = host.ts
        kind = ts.get("driver")
        if kind == "notifications":
            return self._run(host, "read notifications",
                             "The user asked about desktop notifications.")
        if kind == "media":
            key = ts.get("media_cmd") or "playpause"
            return self._run(host, key,
                             "The user asked for a media control action.")
        if kind == "wikipedia":
            q = ts.get("info_query") or ts.get("expected_text") or ""
            return self._run(host, f'search wikipedia for "{q}"',
                             f"The user asked what Wikipedia says about '{q}'.")
        if kind == "google":
            q = ts.get("info_query") or ts.get("expected_text") or ""
            return self._run(host, f"google {q}",
                             f"The user asked to search the web for '{q}'.")
        return False


def select_app_driver(ts):
    """Registry: pick the right deterministic driver for the current task."""
    if ts.get("active") and ts.get("driver") in ("notifications", "media",
                                                 "wikipedia", "google"):
        return InfoDriver()
    if ts.get("driver") == "messaging" and ts.get("active") and ts.get("target_contact"):
        if "snapchat" in (ts.get("target_app") or "").lower() or \
           "snapchat" in (ts.get("goal_hint") or "").lower():
            return SnapchatDriver()
        return WhatsAppDriver()
    if ts.get("active") and ts.get("expected_text") and ts.get("target_launched"):
        if "youtube" in (ts.get("target_app") or "").lower():
            return YouTubeDriver()
        return SearchDriver()
    print(f"[DEBUG-DRIVER] select_app_driver returned None. active={ts.get('active')}, driver={ts.get('driver')}, contact={ts.get('target_contact')}")
    return None