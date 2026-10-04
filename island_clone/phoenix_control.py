"""
phoenix_control.py - the Phoenix main app window.

    python island_clone\\phoenix_control.py            (standalone, read-only island)

    python island_clone\\phoenix_live.py                (launched by the island)

A normal, closable desktop window that sits NEXT TO the floating Dynamic
Island - not on top of it.  It owns everything that used to be crammed into
the island's Settings panel and did not fit there:

    Mascots   a gallery of mascots.  Each one has its own personality, its
              own colour, its own shape (or emoji) and its own size.  Click a
              card to put it on screen in the island; edit the one on the
              right to change its colour, shape, emoji, size and personality.
              Add, duplicate and delete.  This is where the personality picker
              lives now - the island no longer has one.
    Models    every GGUF the island can find, which one the brain is pointed
              at, and the Hugging Face download queue.
    Settings  the island's feature switches, the voice toggle, the panel
              opacity slider and the transparency fallback.

Closing this window closes nothing else.  The island keeps rendering, stays
on top and keeps talking to the bot; press M on the island (or use the Main
app row in its Settings tab) to bring this window back.

THREADING.  Tk insists that a window be built and driven from the thread that
created it, and the island's render loop already owns the main thread
(phoenix_live.py).  So we never call mainloop() and never touch Tk from the
bot's worker threads: build() happens on the main thread before the island
starts, and pump() is called once per rendered frame by the island's
on_frame hook.  pump() is cheap, wrapped so that a Tk error can never reach
the render loop, and a no-op once the window is closed for good.

GLASS.  The window is semi-transparent, with Windows asked to blur the
desktop behind it.  Both live in phoenix_glass.py; the level is clamped to
a floor that was measured against the worst possible backdrop rather than
picked by eye, and it is remembered in the store like everything else.

The state both windows read and write lives in phoenix_store.py.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for _p in (str(HERE), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import phoenix_store as store_mod
import phoenix_glass as glass
import phoenix_logs as logs_mod
from phoenix_logs import LogTail, SOURCES, level_of
from phoenix_store import (MascotStore, PERSONALITIES, SHAPES, PALETTE,
                           ACCENTS, EMOJI, SIZE_MIN, SIZE_MAX, rgb_to_hex)

# The window's colours.  Deep and slightly blue on purpose: a darker panel
# keeps MORE contrast against light text, which is what lets the glass go as
# far as alpha 0.80 and still be readable over a white desktop.  See
# phoenix_glass.py for the measured numbers.
BG = glass.BG
PANEL = glass.PANEL
CARD = glass.CARD
CARD_ON = glass.CARD_ON
EDGE = glass.EDGE
TEXT = glass.TEXT
MUTED = glass.MUTED
ACCENT = glass.ACCENT

FONT = "Segoe UI"

#: Pumping Tk on every one of 60 frames would be wasted work; 30 Hz is
#: indistinguishable to a person and leaves the island most of its frame.
PUMP_HZ = 30.0


def log(msg: str):
    try:
        sys.stdout.write(f"[control] {msg}\n")
        sys.stdout.flush()
    except Exception:
        pass


class MascotCard:
    """One entry in the gallery.  Just data + the geometry it was drawn at,
    so a click can be turned back into a mascot key."""

    def __init__(self, key: str, rect, tag: str):
        self.key = key
        self.rect = rect
        self.tag = tag


class PhoenixControl:
    """The main app window.  Create on the main thread; drive with pump()."""

    def __init__(self, island=None, store: Optional[MascotStore] = None):
        self.island = island
        self.store = store or MascotStore()
        self.root = None
        self.closed = False              # True once the window is gone for good
        self.visible = False
        self.selected = self.store.active
        self._pump_interval = 1.0 / max(1.0, PUMP_HZ)
        self._since_pump = 0.0
        self._since_poll = 0.0

        # widget handles, filled in by build()
        self.nb = None
        self.gallery = None
        self.gallery_inner = None
        self.cards: List[MascotCard] = []
        self.name_var = None
        self.personality_var = None
        self.shape_var = None
        self.size_var = None
        self.size_label = None
        self.head_buttons: List[Any] = []
        self.accent_buttons: List[Any] = []
        self.preview = None
        self.emoji_buttons: List[Any] = []
        self.model_list = None
        self.model_status = None
        self.download_list = None
        self.repo_var = None
        self.file_var = None
        self.toggles: Dict[str, Any] = {}
        self.opacity_var = None
        self.opacity_label = None
        self.glass_var = None
        self.glass_level_var = None
        self.glass_label = None
        self.glass_backdrop = None
        self.glass_backdrop_note = None
        self.glass_on = None
        self.glass_level_scale = None
        # Logs tab
        self.log_key = "bot"
        self.tails: Dict[str, LogTail] = {}
        self.log_text = None
        self.log_filter = None
        self.log_status = None
        self.log_follow = None
        self.log_source_buttons: List[Any] = []
        self._log_lines_drawn = 0
        self.voice_state = None
        self.hint = None
        self.status = None

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def build(self) -> bool:
        """Create the window.  Main thread only.  Returns False if Tk cannot
        start here, which must never stop the island."""
        if self.root is not None:
            return True
        try:
            import tkinter as tk
            from tkinter import ttk
        except Exception as exc:                 # no tkinter on this box
            log(f"tkinter unavailable: {exc}")
            return False
        try:
            self.root = tk.Tk()
        except Exception as exc:
            log(f"no display: {exc}")
            self.root = None
            return False

        self.tk = tk
        self.ttk = ttk
        self._style(ttk)

        root = self.root
        root.title("Phoenix")
        root.configure(bg=BG)
        root.geometry(self._window_geometry())
        root.minsize(760, 520)

        self.nb = ttk.Notebook(root)
        self.nb.pack(fill="both", expand=True, padx=10, pady=(10, 0))
        self._build_mascots_tab(self.nb)
        self._build_models_tab(self.nb)
        self._build_settings_tab(self.nb)
        self._build_logs_tab(self.nb)

        foot = tk.Frame(root, bg=BG)
        foot.pack(fill="x", side="bottom", padx=12, pady=(6, 10))
        self.hint = tk.Label(
            foot, bg=BG, fg=MUTED, anchor="w", font=(FONT, 9),
            text="The floating island keeps running when this window closes. "
                 "Press M on it to reopen.")
        self.hint.pack(side="left")
        self.status = tk.Label(foot, bg=BG, fg=ACCENT, anchor="e", font=(FONT, 9))
        self.status.pack(side="right")

        # Closing this window must never touch the island.  Withdraw instead
        # of destroy: Tk roots are cheap to keep and awkward to rebuild after
        # a destroy (a second Tk() on this thread can fail outright), and the
        # user still wants M to bring the window back.
        root.protocol("WM_DELETE_WINDOW", self.close)

        self.apply_glass(self.saved_glass_level())
        self.refresh_all()
        # Ensure the companion control window starts withdrawn until opened (via 'M' or show)
        root.withdraw()
        return True

    # ------------------------------------------------------------------
    # glass
    # ------------------------------------------------------------------
    def saved_glass_level(self) -> float:
        """The level from the store, or the shipped default."""
        return glass.clamp_level(self.store.settings.get(
            "glass_level", glass.GLASS_DEFAULT))

    def apply_glass(self, level=None, with_backdrop: bool = True):
        """Make the window frosted.

        Tk's -alpha does the compositing; phoenix_glass makes the same call
        directly and asks DWM to blur the desktop underneath.  Either can
        fail without taking the window with it - a machine with no DWM just
        gets translucency without blur, and one with neither gets an ordinary
        window.
        """
        level = self.saved_glass_level() if level is None \
            else glass.clamp_level(level)
        if self.root is None:
            return False
        try:
            self.root.attributes("-alpha", level)
            self.root.update_idletasks()
            # Tk does not create the real HWND until the window has been
            # mapped, so ask again after a full update before giving up.
            hwnd = glass.hwnd_of(self.root)
            if not hwnd:
                self.root.update()
                hwnd = glass.hwnd_of(self.root)
            if hwnd:
                glass.apply_level(hwnd, level)
                self.glass_backdrop = (glass.apply_backdrop(hwnd)
                                       if with_backdrop
                                       else glass.clear_backdrop(hwnd))
                if self.glass_backdrop_note is not None:
                    self.glass_backdrop_note.configure(
                        text=f"backdrop: {self.glass_backdrop or 'none available'}"
                             f"  ·  composited at {int(round(level * 100))}%")
            if self.glass_label is not None:
                self.glass_label.configure(
                    text=f"{int(round(level * 100))}%  (floor "
                         f"{int(round(glass.GLASS_MIN * 100))}%)")
        except Exception as exc:
            log(f"glass: {exc}")
            return False
        return level

    def set_glass_level(self, level):
        """The slider: live, and remembered."""
        self.store.settings["glass_level"] = glass.clamp_level(level)
        self.apply_glass(self.store.settings["glass_level"])
        self.persist()
        return self.store.settings["glass_level"]

    def _window_geometry(self) -> str:
        """A sensible place and size, biased toward the middle of the screen
        so the window does not cover the island at the top."""
        try:
            import tkinter as tk
            tmp = tk.Tk()
            sw, sh = tmp.winfo_screenwidth(), tmp.winfo_screenheight()
            tmp.destroy()
        except Exception:
            return "900x620+120+90"
        w = min(1080, max(760, int(sw * 0.62)))
        h = min(700, max(520, int(sh * 0.72)))
        x = max(0, (sw - w) // 2)
        y = max(90, int(sh * 0.10))
        return f"{w}x{h}+{x}+{y}"

    def _style(self, ttk):
        try:
            s = ttk.Style()
            try:
                s.theme_use("clam")
            except Exception:
                pass
            s.configure("Phoenix.TFrame", background=BG)
            s.configure("Card.TFrame", background=CARD)
            s.configure("TNotebook", background=BG, borderwidth=0)
            s.configure("TNotebook.Tab", background=PANEL, foreground=TEXT,
                        padding=(16, 8))
            s.map("TNotebook.Tab",
                  background=[("selected", CARD_ON)],
                  foreground=[("selected", TEXT)])
            s.configure("TLabelframe", background=BG, borderwidth=0)
            s.configure("TLabelframe.Label", background=BG, foreground=MUTED)
            s.configure("TLabel", background=BG, foreground=TEXT)
            s.configure("TCheckbutton", background=BG, foreground=TEXT)
            s.configure("TButton", background=PANEL, foreground=TEXT)
            s.map("TButton", background=[("active", CARD_ON)])
            s.configure("TEntry", fieldbackground=PANEL, foreground=TEXT)
            s.configure("TCombobox", fieldbackground=PANEL, foreground=TEXT)
            s.configure("TListbox", background=PANEL, foreground=TEXT)
            # clam paints the scale trough in a pale cream that reads as a
            # bright bar against this dark UI - tone it down to the panel
            s.configure("Horizontal.TScale", background=BG, troughcolor=PANEL,
                        darkcolor=PANEL, lightcolor=PANEL,
                        bordercolor=PANEL)
        except Exception as exc:
            log(f"style: {exc}")

    # ------------------------------------------------------------------
    # closing / reopening
    # ------------------------------------------------------------------
    def show(self):
        """Bring the window up.  Safe to call from any frame, many times."""
        if self.closed or self.root is None:
            return False
        try:
            if not self.visible:
                self.refresh_all()
                self.root.deiconify()
            self.visible = True
            self.root.lift()
            # now that the window is really on screen, (re)apply the glass:
            # the HWND only exists once Tk has mapped it
            self.apply_glass()
            try:
                self.root.attributes("-topmost", True)
                self.root.after(150, self._drop_topmost)
            except Exception:
                pass
            self.root.focus_force()
            self._say("open")
            return True
        except Exception as exc:
            log(f"show failed: {exc}")
            return False

    def _drop_topmost(self):
        """Stop stealing focus: brought to the front once, then normal."""
        try:
            if self.root is not None:
                self.root.attributes("-topmost", False)
        except Exception:
            pass

    def close(self):
        """The window's own close button.

        The island is deliberately NOT touched: it keeps its render loop, its
        always-on-top window and its bot connection.  Only the Tk window goes
        away, and show() brings it straight back.
        """
        if self.closed or self.root is None:
            return
        try:
            self.persist()
            self.visible = False
            self.root.withdraw()
            log("closed - the island keeps running")
        except Exception as exc:
            log(f"close failed: {exc}")

    def destroy(self):
        """Give up the window for good.  Only the launcher does this, when
        the island itself is shutting down."""
        self.closed = True
        self.visible = False
        try:
            if self.root is not None:
                self.root.destroy()
        except Exception:
            pass
        self.root = None

    def pump(self, dt: float = 0.0):
        """Let Tk process its events.  Called once per island frame.

        Everything here is guarded: the island's render loop must never see
        an exception raised by the window, and must never be made to wait.
        """
        if self.closed or self.root is None or not self.visible:
            return
        try:
            self._since_pump += dt or self._pump_interval
            if self._since_pump < self._pump_interval:
                return
            self._since_pump = 0.0
            self.root.update()
            self._since_poll += self._pump_interval
            if self._since_poll >= 2.0:
                self._since_poll = 0.0
                self._refresh_if_changed()
            # the logs follow at 4 Hz - fast enough to look live, slow enough
            # that nobody notices the gap
            self._since_log = getattr(self, "_since_log", 0.0) + self._pump_interval
            if self._since_log >= 0.25:
                self._since_log = 0.0
                self.poll_logs()
        except Exception as exc:
            log(f"pump: {exc}")
            # A broken Tk loop must not take 60 fps of island with it.
            try:
                self.closed = True
                self.visible = False
            except Exception:
                pass

    # ------------------------------------------------------------------
    # the shared state
    # ------------------------------------------------------------------
    def persist(self):
        """Write the store and push it into the island.  This is the only
        place the two windows are kept in step."""
        if not self.store.save() and self.store.last_error:
            self._say("save failed", bad=True)
            log(self.store.last_error)
        self.push_to_island()

    def push_to_island(self):
        """Hand the island its mascot.  A missing island is normal - the
        window also runs on its own, as a gallery and a model browser."""
        isl = self.island
        if isl is None:
            return False
        try:
            isl.set_mascots(self.store.mascots, self.store.active)
            return True
        except Exception as exc:
            log(f"island sync: {exc}")
            return False

    def _refresh_if_changed(self):
        """Pick up edits made to control.json by something else."""
        try:
            before = (self.store.active, self._fingerprint())
            fresh = MascotStore(self.store.path).load()
            if fresh.last_error:
                return
            after = (fresh.active, self._fingerprint(fresh))
            if before != after:
                self.store.apply(fresh.to_dict())
                self.refresh_all()
                self.push_to_island()
        except Exception as exc:
            log(f"poll: {exc}")

    def _fingerprint(self, st=None) -> tuple:
        st = st or self.store
        return tuple(sorted(
            (m["key"], m["personality"], tuple(m["head"]), tuple(m["accent"]),
             m["shape"], m["emoji"], round(float(m["size"]), 3), m["name"])
            for m in st.mascots))

    def selected_mascot(self) -> Dict[str, Any]:
        return self.store.get(self.selected) or self.store.active_mascot()

    def _say(self, msg: str, bad: bool = False):
        if self.status is None:
            return
        try:
            self.status.configure(text=msg, fg="#ff7676" if bad else ACCENT)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Mascots tab
    # ------------------------------------------------------------------
    def _build_mascots_tab(self, parent):
        tk = self.tk
        page = tk.Frame(parent, bg=BG)
        parent.add(page, text="Mascots")

        left = tk.Frame(page, bg=BG)
        left.pack(side="left", fill="both", expand=True, padx=(0, 10), pady=10)
        bar = tk.Frame(left, bg=BG)
        bar.pack(fill="x", pady=(0, 6))
        tk.Label(bar, text="Gallery", bg=BG, fg=MUTED,
                 font=(FONT, 9, "bold")).pack(side="left")
        tk.Button(bar, text="Add", width=7, command=self.add_mascot,
                  bg=CARD, fg=TEXT, activebackground=CARD_ON,
                  relief="flat", bd=0).pack(side="right", padx=3)
        tk.Button(bar, text="Duplicate", width=10, command=self.duplicate_mascot,
                  bg=CARD, fg=TEXT, activebackground=CARD_ON,
                  relief="flat", bd=0).pack(side="right", padx=3)
        tk.Button(bar, text="Delete", width=8, command=self.delete_mascot,
                  bg=CARD, fg=TEXT, activebackground=CARD_ON,
                  relief="flat", bd=0).pack(side="right", padx=3)

        holder = tk.Frame(left, bg=BG)
        holder.pack(fill="both", expand=True)
        self.gallery = tk.Canvas(holder, bg=BG, highlightthickness=0,
                                 bd=0)
        vs = tk.Scrollbar(holder, orient="vertical", command=self.gallery.yview)
        self.gallery.configure(yscrollcommand=vs.set)
        vs.pack(side="right", fill="y")
        self.gallery.pack(side="left", fill="both", expand=True)
        self.gallery.bind("<Configure>", lambda _e: self.redraw_gallery())
        self.gallery.bind("<Button-1>", self._gallery_click)
        self.gallery.bind("<MouseWheel>", self._gallery_wheel)

        right = tk.Frame(page, bg=PANEL, width=340)
        right.pack(side="right", fill="y", pady=10)
        right.pack_propagate(False)
        self._build_customise(right)

    def _build_customise(self, parent):
        tk, ttk = self.tk, self.ttk
        tk.Label(parent, text="Customise", bg=PANEL, fg=MUTED,
                 font=(FONT, 9, "bold")).pack(anchor="w", padx=14, pady=(12, 8))

        self.preview = tk.Canvas(parent, bg=PANEL, highlightthickness=0, height=120)
        self.preview.pack(fill="x", padx=14)

        form = tk.Frame(parent, bg=PANEL)
        form.pack(fill="x", padx=14, pady=10)

        tk.Label(form, text="Name", bg=PANEL, fg=MUTED,
                 font=(FONT, 9)).grid(row=0, column=0, sticky="w", pady=3)
        self.name_var = tk.StringVar()
        e = tk.Entry(form, textvariable=self.name_var, bg=PANEL, fg=TEXT,
                     insertbackground=TEXT, relief="flat", highlightthickness=1,
                     highlightbackground=EDGE, highlightcolor=ACCENT)
        e.grid(row=0, column=1, sticky="ew", padx=(10, 0), pady=3, ipady=3)
        e.bind("<Return>", lambda _ev: self.commit_name())
        e.bind("<FocusOut>", lambda _ev: self.commit_name())

        tk.Label(form, text="Personality", bg=PANEL, fg=MUTED,
                 font=(FONT, 9)).grid(row=1, column=0, sticky="w", pady=3)
        self.personality_var = tk.StringVar()
        cb = ttk.Combobox(form, textvariable=self.personality_var, state="readonly",
                         values=[PERSONALITIES[k] for k in PERSONALITIES])
        cb.grid(row=1, column=1, sticky="ew", padx=(10, 0), pady=3)
        cb.bind("<<ComboboxSelected>>", lambda _ev: self.commit_personality())

        tk.Label(form, text="Shape", bg=PANEL, fg=MUTED,
                 font=(FONT, 9)).grid(row=2, column=0, sticky="w", pady=3)
        self.shape_var = tk.StringVar()
        cb2 = ttk.Combobox(form, textvariable=self.shape_var, state="readonly",
                           values=list(SHAPES))
        cb2.grid(row=2, column=1, sticky="ew", padx=(10, 0), pady=3)
        cb2.bind("<<ComboboxSelected>>", lambda _ev: self.commit_shape())

        tk.Label(form, text="Size", bg=PANEL, fg=MUTED,
                 font=(FONT, 9)).grid(row=3, column=0, sticky="w", pady=3)
        self.size_var = tk.DoubleVar(value=1.0)
        sc = ttk.Scale(form, from_=SIZE_MIN, to=SIZE_MAX, variable=self.size_var,
                       orient="horizontal", command=lambda _v: self.preview_size())
        # the island follows the drag live, but the file is only written once
        # on release - a slider drag would otherwise save ~60 times a second
        sc.bind("<ButtonRelease-1>", lambda _e: self.commit_size())
        sc.grid(row=3, column=1, sticky="ew", padx=(10, 0), pady=3)
        self.size_label = tk.Label(form, text="100%", bg=PANEL, fg=TEXT,
                                   font=(FONT, 9), width=6, anchor="e")
        self.size_label.grid(row=3, column=2, padx=(6, 0))

        form.columnconfigure(1, weight=1)

        self._swatch_row(parent, "Head colour", PALETTE, "head")
        self._swatch_row(parent, "Accent colour", ACCENTS, "accent")

        tk.Label(parent, text="Emoji", bg=PANEL, fg=MUTED,
                 font=(FONT, 9, "bold")).pack(anchor="w", padx=14, pady=(8, 2))
        grid = tk.Frame(parent, bg=PANEL)
        grid.pack(fill="x", padx=14)
        self.emoji_buttons = []
        for i, glyph in enumerate(EMOJI):
            b = tk.Button(grid, text=glyph, font=("Segoe UI Emoji", 14),
                          command=lambda g=glyph: self.commit_emoji(g),
                          bg=CARD, fg=TEXT, relief="flat", bd=0, width=3)
            b.grid(row=i // 6, column=i % 6, padx=2, pady=2)
            self.emoji_buttons.append((glyph, b))
        b = tk.Button(grid, text="none", font=(FONT, 8),
                      command=lambda: self.commit_emoji(""),
                      bg=CARD, fg=MUTED, relief="flat", bd=0, width=5)
        b.grid(row=len(EMOJI) // 6, column=len(EMOJI) % 6, padx=2, pady=2)

        tk.Button(parent, text="Use this mascot on the island",
                  command=self.use_selected, bg="#2d5f9e", fg=TEXT,
                  activebackground="#3a7ac9", relief="flat", bd=0,
                  font=(FONT, 10, "bold")).pack(fill="x", padx=14, pady=(14, 14))

    def _swatch_row(self, parent, label, colours, which):
        tk = self.tk
        tk.Label(parent, text=label, bg=PANEL, fg=MUTED,
                 font=(FONT, 9, "bold")).pack(anchor="w", padx=14, pady=(8, 2))
        row = tk.Frame(parent, bg=PANEL)
        row.pack(fill="x", padx=14)
        buttons = []
        for i, col in enumerate(colours):
            b = tk.Button(row, text="  ", bg=rgb_to_hex(col), relief="flat",
                          bd=0, highlightthickness=0, width=3,
                          command=lambda c=col, w=which: self.commit_colour(w, c))
            b.grid(row=0, column=i, padx=2, pady=2)
            buttons.append((col, b))
        more = tk.Button(row, text="pick", font=(FONT, 8), bg=CARD, fg=MUTED,
                         relief="flat", bd=0, width=5,
                         command=lambda w=which: self.pick_colour(w))
        more.grid(row=0, column=len(colours), padx=(8, 0))
        if which == "head":
            self.head_buttons = buttons
        else:
            self.accent_buttons = buttons

    # -- the gallery drawing ------------------------------------------
    def redraw_gallery(self):
        cv = self.gallery
        if cv is None:
            return
        try:
            cv.delete("all")
            self.cards = []
            w = max(320, cv.winfo_width())
            card_w, card_h, gap = 196, 96, 10
            cols = max(1, (w - gap) // (card_w + gap))
            for i, m in enumerate(self.store.mascots):
                col, row = i % cols, i // cols
                x = gap + col * (card_w + gap)
                y = gap + row * (card_h + gap)
                self._draw_card(cv, m, x, y, card_w, card_h, i)
            rows = (len(self.store.mascots) + cols - 1) // cols
            cv.configure(scrollregion=(0, 0, w, gap + rows * (card_h + gap) + gap))
        except Exception as exc:
            log(f"gallery: {exc}")

    def _draw_card(self, cv, m, x, y, w, h, index):
        active = m["key"] == self.store.active
        picked = m["key"] == self.selected
        bg = CARD_ON if picked else CARD
        rect = (x, y, w, h)
        cv.create_rectangle(x, y, x + w, y + h, fill=bg, outline=ACCENT if picked else EDGE,
                            width=2 if picked else 1)
        tag = f"card{index}"
        cv.create_rectangle(x, y, x + w, y + h, outline="", fill="", tags=tag)
        cx, cy = x + 40, y + h // 2
        self._draw_shape(cv, m, cx, cy, 26)
        label = m["name"]
        if active:
            label = "* " + label
        cv.create_text(x + 74, y + 30, anchor="w", text=label, fill=TEXT,
                       font=(FONT, 10, "bold"), tags=tag)
        cv.create_text(x + 74, y + 52, anchor="w",
                       text=PERSONALITIES.get(m["personality"], "Normal"),
                       fill=MUTED, font=(FONT, 9), tags=tag)
        cv.create_text(x + 74, y + 72, anchor="w",
                       text=f"{m['shape']}  -  {int(round(float(m['size']) * 100))}%",
                       fill=MUTED, font=(FONT, 8), tags=tag)
        self.cards.append(MascotCard(m["key"], (x, y, x + w, y + h), tag))

    def _draw_shape(self, cv, m, cx, cy, r):
        """The same shapes the island draws, in Tk canvas terms."""
        shape = m.get("shape", "blob")
        col = rgb_to_hex(m["head"])
        edge = rgb_to_hex(m["accent"])
        size = float(m.get("size", 1.0))
        rx = r * size
        ry = r * 0.88 * size
        if shape == "emoji" and m.get("emoji"):
            cv.create_text(cx, cy, text=m["emoji"], fill=edge,
                           font=("Segoe UI Emoji", int(max(10, rx * 1.7))))
            return
        if shape in ("blob", "square"):
            if shape == "square":
                cv.create_rectangle(cx - rx, cy - ry, cx + rx, cy + ry,
                                    fill=col, outline=edge)
                return
            # Tk's canvas has no rounded rectangle, so the soft blob is
            # a rectangle between two ovals, outlined with two arcs and two
            # lines - the same silhouette the island draws, at card size.
            cap = min(ry * 0.55, rx * 0.45)
            top, bot = cy - ry, cy + ry
            cv.create_rectangle(cx - rx, top + cap, cx + rx, bot - cap,
                                fill=col, outline="")
            cv.create_oval(cx - rx, top, cx + rx, top + 2 * cap,
                           fill=col, outline="")
            cv.create_oval(cx - rx, bot - 2 * cap, cx + rx, bot,
                           fill=col, outline="")
            cv.create_line(cx - rx, top + cap, cx - rx, bot - cap, fill=edge)
            cv.create_line(cx + rx, top + cap, cx + rx, bot - cap, fill=edge)
            cv.create_arc(cx - rx, top, cx + rx, top + 2 * cap, start=0,
                          extent=180, style="arc", outline=edge, width=1)
            cv.create_arc(cx - rx, bot - 2 * cap, cx + rx, bot, start=180,
                          extent=180, style="arc", outline=edge, width=1)
            return
        if shape == "circle":
            cv.create_oval(cx - rx, cy - ry, cx + rx, cy + ry, fill=col, outline=edge)
            return
        if shape == "hex":
            pts = [(cx + rx * 1.02 * _cos(60 * i - 90),
                    cy + ry * 1.02 * _sin(60 * i - 90)) for i in range(6)]
        elif shape == "diamond":
            pts = [(cx, cy - ry * 1.14), (cx + rx * 1.14, cy),
                   (cx, cy + ry * 1.14), (cx - rx * 1.14, cy)]
        elif shape == "triangle":
            pts = [(cx, cy - ry * 1.18), (cx + rx * 1.12, cy + ry * 0.84),
                   (cx - rx * 1.12, cy + ry * 0.84)]
        elif shape == "star":
            pts = []
            for i in range(10):
                ang = 36 * i - 90
                rad = 1.16 if i % 2 == 0 else 0.52
                pts.append((cx + rx * rad * _cos(ang), cy + ry * rad * _sin(ang)))
        else:
            cv.create_oval(cx - rx, cy - ry, cx + rx, cy + ry, fill=col, outline=edge)
            return
        pts = [p for pair in pts for p in pair]
        cv.create_polygon(*pts, fill=col, outline=edge)

    def _draw_preview(self):
        if self.preview is None:
            return
        try:
            self.preview.delete("all")
            m = self.selected_mascot()
            self._draw_shape(self.preview, m, 170, 60, 34)
        except Exception as exc:
            log(f"preview: {exc}")

    # -- gallery interaction -------------------------------------------
    def _gallery_click(self, event):
        x, y = event.x, event.y
        for card in self.cards:
            x0, y0, x1, y1 = card.rect
            if x0 <= x <= x1 and y0 <= y <= y1:
                self.selected = card.key
                self.redraw_gallery()
                self.refresh_customise()
                self._say(f"selected {self.selected}")
                return

    def _gallery_wheel(self, event):
        try:
            self.gallery.yview_scroll(-1 * (event.delta // 60)
                                      if event.delta else 0, "units")
        except Exception:
            pass

    # -- committing edits ----------------------------------------------
    def _apply(self, **fields):
        m = self.store.get(self.selected)
        if not m:
            return
        m.update(fields)
        m = store_mod.normalise_mascot(m)
        self.store.upsert(m)
        self.push_to_island()
        self.persist()
        self.redraw_gallery()
        self._draw_preview()

    def commit_name(self):
        if self.name_var is None:
            return
        text = (self.name_var.get() or "").strip()
        if text and text != self.selected_mascot().get("name"):
            self._apply(name=text)

    def commit_personality(self):
        label = self.personality_var.get()
        for key, name in PERSONALITIES.items():
            if name == label:
                self._apply(personality=key)
                return

    def commit_shape(self):
        self._apply(shape=self.shape_var.get())

    def commit_emoji(self, glyph):
        self._apply(emoji=glyph)
        self._draw_preview()

    def commit_colour(self, which, rgb):
        self._apply(**{which: rgb})

    def preview_size(self):
        val = max(SIZE_MIN, min(SIZE_MAX, float(self.size_var.get())))
        if self.size_label is not None:
            self.size_label.configure(text=f"{int(round(val * 100))}%")
        self._draw_preview()
        isl = self.island
        if isl is not None:
            try:
                isl.set_mascot_appearance(size=val)
            except Exception:
                pass

    def commit_size(self):
        self._apply(size=float(self.size_var.get()))

    def pick_colour(self, which):
        """The palette covers the common cases; this is the escape hatch."""
        try:
            from tkinter import colorchooser
            cur = rgb_to_hex(self.selected_mascot().get(which))
            picked = colorchooser.askcolor(color=cur, parent=self.root)
            if picked and picked[0]:
                self.commit_colour(which, tuple(int(c) for c in picked[0]))
        except Exception as exc:
            log(f"colour picker: {exc}")

    def add_mascot(self):
        base = "Mascot"
        n = len(self.store.mascots) + 1
        while self.store.get(self.store.unique_key(base)):
            n += 1
            base = f"Mascot{n}"
        key = self.store.unique_key(base)
        seed = PALETTE[len(self.store.mascots) % len(PALETTE)]
        new = dict(key=key, name=base,
                   personality=list(PERSONALITIES)[len(self.store.mascots) % 6],
                   head=seed, accent=ACCENTS[len(self.store.mascots) % len(ACCENTS)],
                   shape="blob", emoji=EMOJI[len(self.store.mascots) % len(EMOJI)],
                   size=1.0)
        self.store.upsert(new)
        self.selected = key
        self.persist()
        self.redraw_gallery()
        self.refresh_customise()
        self._say(f"added {base}")

    def duplicate_mascot(self):
        src = self.selected_mascot()
        key = self.store.unique_key(src["name"])
        copy = dict(src)
        copy["key"] = key
        copy["name"] = f"{src['name']} copy"[:24]
        self.store.upsert(copy)
        self.selected = key
        self.persist()
        self.redraw_gallery()
        self.refresh_customise()
        self._say(f"duplicated as {copy['name']}")

    def delete_mascot(self):
        if self.store.delete(self.selected):
            self.selected = self.store.active
            self.persist()
            self.redraw_gallery()
            self.refresh_customise()
            self._say("deleted")
        else:
            self._say("cannot delete the mascot in use", bad=True)

    def use_selected(self):
        """Put this mascot on screen in the floating island."""
        self.store.active = self.selected
        self.persist()
        self.redraw_gallery()
        self._say(f"{self.selected_mascot()['name']} is live")

    # ------------------------------------------------------------------
    # Models tab
    # ------------------------------------------------------------------
    def _build_models_tab(self, parent):
        tk, ttk = self.tk, self.ttk
        page = tk.Frame(parent, bg=BG)
        parent.add(page, text="Models")

        top = tk.Frame(page, bg=BG)
        top.pack(fill="both", expand=True, padx=10, pady=10)
        bar = tk.Frame(top, bg=BG)
        bar.pack(fill="x", pady=(0, 6))
        tk.Label(bar, text="GGUF files on this machine", bg=BG, fg=MUTED,
                 font=(FONT, 9, "bold")).pack(side="left")
        tk.Button(bar, text="Rescan", width=9, command=self.rescan_models,
                  bg=CARD, fg=TEXT, activebackground=CARD_ON,
                  relief="flat", bd=0).pack(side="right", padx=3)
        tk.Button(bar, text="Use selected", width=13, command=self.use_model,
                  bg="#2d5f9e", fg=TEXT, activebackground="#3a7ac9",
                  relief="flat", bd=0).pack(side="right", padx=3)

        holder = tk.Frame(top, bg=BG)
        holder.pack(fill="both", expand=True)
        self.model_list = tk.Listbox(holder, bg=PANEL, fg=TEXT,
                                    selectbackground="#2d5f9e",
                                    highlightthickness=1,
                                    highlightbackground=EDGE, bd=0,
                                    font=(FONT, 10))
        sb = tk.Scrollbar(holder, orient="vertical", command=self.model_list.yview)
        self.model_list.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.model_list.pack(side="left", fill="both", expand=True)
        self.model_list.bind("<Double-Button-1>", lambda _e: self.use_model())

        self.model_status = tk.Label(page, bg=BG, fg=MUTED, anchor="w",
                                     font=(FONT, 9), text="")
        self.model_status.pack(fill="x", padx=10)

        dl = tk.LabelFrame(page, text="Download from Hugging Face",
                           bg=PANEL, fg=MUTED)
        dl.pack(fill="x", padx=10, pady=(10, 10))
        r1 = tk.Frame(dl, bg=PANEL)
        r1.pack(fill="x", padx=8, pady=(8, 2))
        tk.Label(r1, text="repo", bg=PANEL, fg=MUTED, width=6,
                 anchor="w", font=(FONT, 9)).pack(side="left")
        self.repo_var = tk.StringVar(value="Qwen/Qwen3-VL-4B-Instruct-GGUF")
        tk.Entry(r1, textvariable=self.repo_var, bg=PANEL, fg=TEXT,
                 insertbackground=TEXT, relief="flat",
                 highlightthickness=1, highlightbackground=EDGE).pack(
                     side="left", fill="x", expand=True, ipady=3)
        r2 = tk.Frame(dl, bg=PANEL)
        r2.pack(fill="x", padx=8, pady=(2, 8))
        tk.Label(r2, text="file", bg=PANEL, fg=MUTED, width=6,
                 anchor="w", font=(FONT, 9)).pack(side="left")
        self.file_var = tk.StringVar()
        tk.Entry(r2, textvariable=self.file_var, bg=PANEL, fg=TEXT,
                 insertbackground=TEXT, relief="flat",
                 highlightthickness=1, highlightbackground=EDGE).pack(
                     side="left", fill="x", expand=True, ipady=3)
        tk.Button(r2, text="Download", width=10, command=self.start_download,
                  bg=CARD, fg=TEXT, activebackground=CARD_ON,
                  relief="flat", bd=0).pack(side="left", padx=(8, 0))
        self.download_list = tk.Label(dl, bg=PANEL, fg=MUTED, anchor="w",
                                      justify="left", font=(FONT, 9),
                                      text="no downloads yet")
        self.download_list.pack(fill="x", padx=8, pady=(0, 8))

    def rescan_models(self):
        if self.island is None:
            self._say("the island is not attached", bad=True)
            return
        try:
            found = self.island.scan_models(force=True)
            self.refresh_models()
            self._say(f"{len(found)} model file(s) found")
        except Exception as exc:
            log(f"rescan: {exc}")
            self._say("scan failed", bad=True)

    def refresh_models(self):
        if self.model_list is None or self.island is None:
            return
        try:
            keep = self.model_list.curselection()
            self.model_list.delete(0, "end")
            models = self.island.scan_models()
            for m in models:
                mark = "  <- in use" if m["path"] == getattr(
                    self.island, "active_model_path", "") else ""
                self.model_list.insert(
                    "end",
                    f"{m['name']}   {m['size_mb']:.0f} MB{mark}")
                self.model_list.itemconfig(
                    "end", foreground="#7fe0a8" if mark else TEXT)
            if keep:
                self.model_list.selection_set(int(keep[0]))
            paths = [m["path"] for m in models]
            used = [os.path.basename(p) for p in (
                getattr(self.island, "active_model_path", ""),
                getattr(self.island, "active_mmproj_path", "")) if p]
            self.model_status.configure(
                text=f"{len(paths)} file(s).  brain: "
                     f"{', '.join(used) if used else 'the default model'}")
        except Exception as exc:
            log(f"refresh_models: {exc}")

    def use_model(self):
        if self.island is None or self.model_list is None:
            return
        sel = self.model_list.curselection()
        if not sel:
            self._say("pick a model first", bad=True)
            return
        try:
            models = self.island.scan_models()
            m = models[int(sel[0])]
            if self.island.set_active_model(m["path"]):
                self.refresh_models()
                self._say(f"brain -> {m['name']}")
        except Exception as exc:
            log(f"use_model: {exc}")
            self._say("could not set that model", bad=True)

    def start_download(self):
        if self.island is None:
            self._say("the island is not attached", bad=True)
            return
        repo = (self.repo_var.get() or "").strip()
        fn = (self.file_var.get() or "").strip()
        if not repo or not fn:
            self._say("a repo and a file name are both needed", bad=True)
            return
        try:
            self.island.start_model_download(repo, fn)
            self._say(f"queued {fn}")
            self._draw_downloads()
        except Exception as exc:
            log(f"download: {exc}")
            self._say("could not queue that download", bad=True)

    def _draw_downloads(self):
        if self.download_list is None or self.island is None:
            return
        try:
            dl = getattr(self.island, "downloads", {}) or {}
            if not dl:
                self.download_list.configure(text="no downloads yet")
                return
            lines = []
            for name, s in dl.items():
                if s.get("state") == "done":
                    lines.append(f"{name}  -  done")
                elif s.get("state") == "error":
                    lines.append(f"{name}  -  failed: {s.get('error')}")
                else:
                    lines.append(f"{name}  -  {s.get('state', '?')} "
                                 f"{int(float(s.get('pct', 0)) * 100)}%")
            self.download_list.configure(text="\n".join(lines[:6]))
        except Exception as exc:
            log(f"downloads: {exc}")

    # ------------------------------------------------------------------
    # Settings tab
    # ------------------------------------------------------------------
    def _build_settings_tab(self, parent):
        tk, ttk = self.tk, self.ttk
        page = tk.Frame(parent, bg=BG)
        parent.add(page, text="Settings")

        isl = self.island
        box = tk.Frame(page, bg=BG)
        box.pack(fill="both", expand=True, padx=22, pady=18)

        tk.Label(box, text="Island", bg=BG, fg=MUTED,
                 font=(FONT, 10, "bold")).pack(anchor="w", pady=(0, 8))

        voice = tk.Frame(box, bg=BG)
        voice.pack(fill="x", pady=3)
        self.voice_state = tk.BooleanVar(value=self._voice_on())
        tk.Checkbutton(voice, text="Voice output", variable=self.voice_state,
                       command=self.toggle_voice, bg=BG, fg=TEXT,
                       selectcolor=BG, activebackground=BG, activeforeground=TEXT,
                       font=(FONT, 10), anchor="w").pack(side="left")
        tk.Label(voice, text="toggle it on the island too (V)",
                 bg=BG, fg=MUTED, font=(FONT, 9)).pack(side="left", padx=10)

        rows = getattr(isl, "_SETTING_ROWS", ()) if isl is not None else ()
        for key, label, why in rows:
            var = tk.BooleanVar(value=bool(isl.setting_toggles.get(key)))
            self.toggles[key] = var
            row = tk.Frame(box, bg=BG)
            row.pack(fill="x", pady=5)
            tk.Checkbutton(row, text=label, variable=var,
                           command=lambda k=key: self.commit_toggle(k),
                           bg=BG, fg=TEXT, selectcolor=BG, activebackground=BG,
                           activeforeground=TEXT, font=(FONT, 10),
                           anchor="w").pack(side="left")
            tk.Label(row, text=why, bg=BG, fg=MUTED,
                     font=(FONT, 9)).pack(side="left", padx=10)

        if isl is not None and "translucent_level" in isl.setting_toggles:
            tk.Label(box, text="Panel opacity", bg=BG, fg=MUTED,
                     font=(FONT, 10, "bold")).pack(anchor="w", pady=(16, 4))
            lvl = isl.setting_toggles["translucent_level"]
            self.opacity_var = tk.DoubleVar(value=float(lvl))
            self.opacity_label = tk.Label(box, text="", bg=BG, fg=TEXT,
                                          font=(FONT, 9))
            self.opacity_label.pack(anchor="w", pady=(2, 0))
            ttk.Scale(box, from_=0.85, to=1.0, orient="horizontal",
                      variable=self.opacity_var,
                      command=lambda _v: self.preview_opacity()).pack(fill="x")

        self._build_glass_section(box)

        tk.Frame(box, bg=BG).pack(fill="both", expand=True)
        foot = tk.Frame(box, bg=BG)
        foot.pack(fill="x", pady=(10, 0))
        tk.Button(foot, text="Open the mascot folder", command=self.open_folder,
                  bg=CARD, fg=TEXT, activebackground=CARD_ON, relief="flat",
                  bd=0, width=24).pack(side="left")
        tk.Button(foot, text=f"Store: {self.store.path}", state="disabled",
                  bg=BG, fg=MUTED, relief="flat", bd=0).pack(side="right")

    def _build_glass_section(self, parent):
        """The window's own glass controls.

        Two separate things, kept as two controls because only one of them is
        about legibility:
          - Glass ON/OFF, which is really "level 100% vs whatever you had"
          - the level itself, floored at the value the contrast maths allows
        """
        tk, ttk = self.tk, self.ttk
        tk.Label(parent, text="This window", bg=BG, fg=MUTED,
                 font=(FONT, 10, "bold")).pack(anchor="w", pady=(18, 6))

        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", pady=3)
        self.glass_var = tk.BooleanVar(value=True)
        self.glass_on = tk.Checkbutton(
            row, text="Glass (see the desktop through it)", variable=self.glass_var,
            command=self.toggle_glass, bg=BG, fg=TEXT, selectcolor=BG,
            activebackground=BG, activeforeground=TEXT, font=(FONT, 10),
            anchor="w")
        self.glass_on.pack(side="left")

        lvl_row = tk.Frame(parent, bg=BG)
        lvl_row.pack(fill="x", pady=(4, 0))
        tk.Label(lvl_row, text="Transparency", bg=BG, fg=MUTED,
                 font=(FONT, 9), width=14, anchor="w").pack(side="left")
        self.glass_level_var = tk.DoubleVar(value=self.saved_glass_level())
        self.glass_level_scale = ttk.Scale(
            lvl_row, from_=glass.GLASS_MIN, to=1.0, orient="horizontal",
            variable=self.glass_level_var, command=self._glass_drag)
        self.glass_level_scale.pack(side="left", fill="x", expand=True)
        # only save on release: a drag would otherwise write the store ~60x a
        # second, exactly the mistake the size slider already avoids
        self.glass_level_scale.bind("<ButtonRelease-1>",
                                    lambda _e: self.commit_glass())
        self.glass_label = tk.Label(lvl_row, text="", bg=BG, fg=TEXT,
                                    font=(FONT, 9), width=18, anchor="e")
        self.glass_label.pack(side="right")

        sup = glass.support()
        bits = []
        if sup.get("windows"):
            bits.append("acrylic backdrop available"
                        if sup.get("native_backdrop") or sup.get("composition_attribute")
                        else "translucency only (no DWM backdrop here)")
        else:
            bits.append("not Windows: an ordinary window")
        self.glass_backdrop_note = tk.Label(parent, text=" - ".join(bits), bg=BG,
                                            fg=MUTED, font=(FONT, 8), anchor="w",
                                            justify="left")
        self.glass_backdrop_note.pack(fill="x", pady=(4, 0))

    def _glass_drag(self, value):
        """Live while dragging: change the window, do not save yet."""
        level = glass.clamp_level(float(value))
        if self.glass_var is not None:
            self.glass_var.set(level < 0.999)
        self.apply_glass(level)

    def commit_glass(self):
        return self.set_glass_level(float(self.glass_level_var.get()))

    def toggle_glass(self):
        if bool(self.glass_var.get()):
            # Turning it back on must not read back the opaque level that
            # turning it OFF just stored - that would leave the checkbox on
            # and the window still solid, which is a silent no-op.
            level = self.saved_glass_level()
            if level >= 0.999:
                level = glass.GLASS_DEFAULT
            self.glass_level_var.set(level)
            self.set_glass_level(level)
        else:
            # OFF is just a fully opaque window, not a different palette
            self.store.settings["glass_level"] = glass.GLASS_OFF
            self.apply_glass(glass.GLASS_OFF, with_backdrop=False)
            self.persist()

    # ------------------------------------------------------------------
    # Logs tab
    # ------------------------------------------------------------------
    def _build_logs_tab(self, parent):
        """Server and bot logs, here instead of on the island.

        The island is a HUD: nowhere to scroll, nowhere to copy a line out of,
        and it sits on top of the screen you are trying to debug.  So the
        logs live in a normal window with a text box you can select from.
        """
        tk = self.tk
        page = tk.Frame(parent, bg=BG)
        parent.add(page, text="Logs")

        top = tk.Frame(page, bg=BG)
        top.pack(fill="x", padx=12, pady=(12, 8))
        self.log_source_buttons = []
        for key, label, path, blurb in SOURCES:
            state = {"on": False}
            btn = tk.Button(
                top, text=label, width=10, relief="flat", bd=0,
                bg=PANEL, fg=MUTED, activebackground=CARD_ON,
                command=lambda k=key: self.show_log(k))
            btn.pack(side="left", padx=(0, 6))
            self.log_source_buttons.append((key, btn, state))

        self.log_follow = tk.BooleanVar(value=True)
        tk.Checkbutton(top, text="Follow", variable=self.log_follow,
                       command=self.refresh_logs, bg=BG, fg=TEXT,
                       selectcolor=BG, activebackground=BG,
                       activeforeground=TEXT, font=(FONT, 9)).pack(side="left", padx=6)
        tk.Button(top, text="Reread", width=9, command=self.reread_log,
                  bg=CARD, fg=TEXT, activebackground=CARD_ON,
                  relief="flat", bd=0).pack(side="left", padx=3)
        tk.Button(top, text="Open file", width=11, command=self.open_log_file,
                  bg=CARD, fg=TEXT, activebackground=CARD_ON,
                  relief="flat", bd=0).pack(side="left", padx=3)

        filt = tk.Frame(page, bg=BG)
        filt.pack(fill="x", padx=12)
        tk.Label(filt, text="Find", bg=BG, fg=MUTED, width=6, anchor="w",
                 font=(FONT, 9)).pack(side="left")
        self.log_filter = tk.StringVar()
        entry = tk.Entry(filt, textvariable=self.log_filter, bg=PANEL, fg=TEXT,
                         insertbackground=TEXT, relief="flat",
                         highlightthickness=1, highlightbackground=EDGE)
        entry.pack(side="left", fill="x", expand=True, ipady=3)
        entry.bind("<KeyRelease>", lambda _e: self.refresh_logs())
        tk.Button(filt, text="Clear", width=8, command=self.clear_filter,
                  bg=CARD, fg=MUTED, relief="flat", bd=0).pack(side="left", padx=(6, 0))

        holder = tk.Frame(page, bg=BG)
        holder.pack(fill="both", expand=True, padx=12, pady=8)
        sb = tk.Scrollbar(holder, orient="vertical")
        self.log_text = tk.Text(holder, bg=PANEL, fg=TEXT, wrap="none",
                                height=12, relief="flat", bd=0,
                                highlightthickness=1, highlightbackground=EDGE,
                                font=("Consolas", 9), padx=8, pady=6,
                                selectbackground="#2d5f9e", yscrollcommand=sb.set)
        sb.configure(command=self.log_text.yview)
        sb.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)
        self.log_text.tag_configure("E", foreground="#ff8a80")
        self.log_text.tag_configure("W", foreground="#ffd479")
        self.log_text.tag_configure("I", foreground=TEXT)
        self.log_text.tag_configure("D", foreground=MUTED)
        self.log_text.tag_configure("ts", foreground="#6f7f9c")
        self.log_text.configure(state="disabled")
        # scrolling up means "I am reading this" - stop yanking the view away
        self.log_text.bind("<MouseWheel>", self._log_wheel)

        self.log_status = tk.Label(page, text="", bg=BG, fg=MUTED, anchor="w",
                                   font=(FONT, 8))
        self.log_status.pack(fill="x", padx=12, pady=(0, 10))

        self.tails = {k: LogTail(p) for k, _l, p, _b in SOURCES}
        self.show_log(self.log_key)

    def show_log(self, key):
        if key not in self.tails:
            return False
        self.log_key = key
        for k, btn, _state in self.log_source_buttons:
            active = (k == key)
            btn.configure(bg=CARD_ON if active else PANEL,
                          fg=TEXT if active else MUTED)
        # read the file BEFORE rendering it.  refresh_logs() only draws; if
        # it did not poll, a freshly selected source would show an empty box
        # until something else happened to poll it.
        tail = self.tails.get(key)
        if tail is not None:
            tail.poll()
        self._log_stamp = None
        self.refresh_logs()
        return True

    def poll_logs(self):
        """Pick up new lines.  Only the visible log is rendered; the others
        are still read so switching tabs shows history, not an empty box."""
        if not self.tails or self.log_text is None:
            return
        for key, tail in self.tails.items():
            tail.poll()
        if self.log_key in self.tails:
            self.refresh_logs()

    def _filter_text(self) -> str:
        try:
            return (self.log_filter.get() if self.log_filter else "") or ""
        except Exception:
            return ""

    def refresh_logs(self):
        """Re-render the visible log.  Cheap when nothing changed: the tail
        reports how many lines it added and we only redraw on a change."""
        if self.log_text is None:
            return 0
        tail = self.tails.get(self.log_key)
        if tail is None:
            return 0
        rows = tail.matches(self._filter_text())
        stamp = (len(rows), tail.offset, tail.truncated, self._filter_text())
        if stamp == getattr(self, "_log_stamp", None):
            self._update_log_status(tail, len(rows))
            return 0
        self._log_stamp = stamp

        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        for text, lvl in rows:
            ts = logs_mod.timestamp_of(text)
            body = text
            if ts:
                body = text[text.find(ts) + len(ts):].strip() or text
            if ts:
                self.log_text.insert("end", ts + "  ", "ts")
            self.log_text.insert("end", body + "\n", lvl)
        self.log_text.configure(state="disabled")
        self._log_lines_drawn = len(rows)
        if self.log_follow is not None and self.log_follow.get():
            self.log_text.see("end")
        self._update_log_status(tail, len(rows))
        return len(rows)

    def _update_log_status(self, tail, shown):
        if self.log_status is None:
            return
        label, path, blurb = next(
            (l, p, b) for k, l, p, b in SOURCES if k == self.log_key)
        if tail.missing:
            state = f"{os.path.basename(path)} does not exist yet - {blurb}"
        elif tail.error:
            state = f"read error: {tail.error}"
        else:
            bits = [f"{os.path.basename(path)}  ({tail.size() / 1024:.0f} KB)"]
            bits.append(f"{len(tail.lines)} lines kept"
                        + (f", older ones dropped" if tail.truncated else ""))
            if self._filter_text():
                bits.append(f"{shown} match '{self._filter_text()}'")
            bits.append("following" if (self.log_follow is not None
                                       and self.log_follow.get()) else "paused")
            state = "  -  ".join(bits)
        try:
            self.log_status.configure(text=state)
        except Exception:
            pass

    def _log_wheel(self, event):
        """Wheel scrolls, and scrolling away from the bottom means pause."""
        try:
            self.log_text.yview_scroll(-1 * (event.delta // 120), "units")
        except Exception:
            return
        try:
            at_bottom = self.log_text.yview()[1] >= 0.995
            if self.log_follow is not None and self.log_follow.get() != at_bottom:
                self.log_follow.set(at_bottom)
        except Exception:
            pass

    def reread_log(self):
        tail = self.tails.get(self.log_key)
        if tail is not None:
            tail.reopen()
            self._log_stamp = None
            self.refresh_logs()

    def clear_filter(self):
        if self.log_filter is not None:
            self.log_filter.set("")
        self._log_stamp = None
        self.refresh_logs()

    def open_log_file(self):
        import os
        for k, _label, path, _blurb in SOURCES:
            if k == self.log_key:
                try:
                    if os.path.isfile(path):
                        os.startfile(path)
                    else:
                        self._say("that log has not been written yet", bad=True)
                except Exception as exc:
                    log(f"open log: {exc}")
                return

    def _voice_on(self) -> bool:
        try:
            if self.island is not None:
                return bool(self.island._voice_on())
        except Exception:
            pass
        return True

    def toggle_voice(self):
        if self.island is None:
            return
        try:
            self.island._toggle_voice()
        except Exception as exc:
            log(f"voice: {exc}")
        if self.voice_state is not None:
            self.voice_state.set(self._voice_on())

    def commit_toggle(self, key):
        if self.island is None:
            return
        var = self.toggles.get(key)
        if var is None:
            return
        try:
            self.island.setting_toggles[key] = bool(var.get())
            self.island._apply_transparency()
            self._say(f"{key} = {self.island.setting_toggles[key]}")
        except Exception as exc:
            log(f"toggle {key}: {exc}")

    def preview_opacity(self):
        if self.island is None or self.opacity_var is None:
            return
        val = max(0.85, min(1.0, float(self.opacity_var.get())))
        if self.opacity_label is not None:
            self.opacity_label.configure(text=f"{int(round(val * 100))}%")
        try:
            self.island.setting_toggles["translucent_level"] = val
            self.island._apply_transparency()
        except Exception as exc:
            log(f"opacity: {exc}")

    def open_folder(self):
        try:
            folder = os.path.dirname(self.store.path)
            os.makedirs(folder, exist_ok=True)
            os.startfile(folder)
        except Exception as exc:
            log(f"folder: {exc}")

    # ------------------------------------------------------------------
    # refreshing from the store
    # ------------------------------------------------------------------
    def refresh_all(self):
        if self.root is None:
            return
        try:
            if not self.store.get(self.selected):
                self.selected = self.store.active
            self._refresh_glass_widgets()
            self.redraw_gallery()
            self.refresh_customise()
            self.refresh_models()
            self._draw_downloads()
        except Exception as exc:
            log(f"refresh: {exc}")

    def _refresh_glass_widgets(self):
        """Put the saved glass level back into the widgets after a refresh."""
        if self.glass_level_var is None:
            return
        try:
            level = self.saved_glass_level()
            self.glass_level_var.set(level)
            if self.glass_var is not None:
                self.glass_var.set(level < 0.999)
        except Exception:
            pass

    def refresh_customise(self):
        m = self.selected_mascot()
        try:
            if self.name_var is not None:
                self.name_var.set(m.get("name", ""))
            if self.personality_var is not None:
                self.personality_var.set(PERSONALITIES.get(m.get("personality"), "Normal"))
            if self.shape_var is not None:
                self.shape_var.set(m.get("shape", "blob"))
            if self.size_var is not None:
                self.size_var.set(float(m.get("size", 1.0)))
            if self.size_label is not None:
                self.size_label.configure(text=f"{int(round(float(m.get('size', 1.0)) * 100))}%")
            for col, btn in self.head_buttons:
                _mark(btn, col, m.get("head"))
            for col, btn in self.accent_buttons:
                _mark(btn, col, m.get("accent"))
            self._draw_preview()
        except Exception as exc:
            log(f"customise: {exc}")


def _mark(btn, colour, current):
    """Ring the swatch that is in use so the choice is visible."""
    try:
        same = tuple(colour) == tuple(current)
        btn.configure(highlightthickness=2 if same else 0,
                      highlightbackground=ACCENT,
                      highlightcolor=ACCENT)
    except Exception:
        pass


def _cos(deg):
    import math
    return math.cos(math.radians(deg))


def _sin(deg):
    import math
    return math.sin(math.radians(deg))


# ----------------------------------------------------------------------
# standalone: a gallery and model browser with no island attached
# ----------------------------------------------------------------------
def main():
    app = PhoenixControl()
    app.store.load()
    app.build()
    if app.root is None:
        log("no window could be created")
        return 1
    app.show()
    app.root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())