# PHOENIX MK8 — Dynamic Island

The bot's real front end: a frameless, always-on-top island that floats at the
top-centre of your desktop and follows the actual state of `bot_mk8_vlm.py` —
every token as it is produced, every action it takes, every sentence it speaks.

```
python island_clone\phoenix_live.py           (or double-click ..\run_island.bat)
```

* **The island is `M.PY`.** The ModernGL file you built stays exactly as it is:
  borderless colorkey window, eyes that track the desktop cursor, eight
  emotions, the app-shortcut grid with its settings icon, the letterbox
  file-pickup animation and the runner-with-trail progress bar.
* **The bot is `bot_mk8_vlm.py`, untouched.** Nothing in this folder edits it.
* **You drive the bot from the island.** Click it or press Enter, type a task,
  press Enter — the turn runs on a worker thread, so the island never freezes.
* **Drag a real file onto it.** Windows hands over the real path; the island
  plays its intake animation and the bot gets a turn about that file.
* **Esc** cancels a running turn (bumping `generation_id` + `ctm.interrupt_now`),
  or closes the island when nothing is running.
* **The answer stays on screen.** The live token tail fills the thought line,
  the finished reply replaces it, the spoken sentence sits on the speech line,
  and the card retracts a few seconds after the turn ends.
* **The chat panel shows the whole turn.** While the bot works the card grows
  to 294 px and the AGENT box becomes a 120 px panel: your request and every
  sentence the bot speaks are word-wrapped into it, and a long turn scrolls
  (mouse wheel over the panel, or PageUp/PageDown — which reach the island even
  while it is click-through). Nothing is truncated to one line any more.
* **The shortcuts never disappear.** The pinned app grid moves into a
  right-hand column while a task runs (the action box steps aside for it) and
  back to the header when the card retracts — it is drawn *and* hit-tested by
  one shared layout, so the cells stay clickable in every mode. `current_action`
  is cleared on `waiting`/`done`, which is what used to strand the card in
  action mode. See `render_check/action_card.png` for the working card.
* **No raw model markup on screen.** The bot writes `<thought>…</thought>` and
  `<ACTION>…</ACTION>`; every piece of bot text passes through
  `_clean_ui_text()` before it reaches a surface, so the thought line, the
  action chip and the chat panel show the words, never the tags. It runs on
  the setter *and* on the queue path the render loop drains.
* **The shortcut icons are legible.** `font_emoji` puts the colour-emoji font
  *first* (`segoeuiemoji, segoeuisymbol, arial`) — Segoe UI Symbol has no
  💬🎵🌐 and pygame never falls through to the next family, so the old order
  drew the same tofu box in all four cells. `_draw_app_icon()` also puts each
  glyph on a light plate with an app-coloured border (a dark 🎵 on a dark cell
  was invisible), and `_glyph_renders()` compares the rendered glyph against a
  private-use codepoint so a genuinely missing glyph falls back to the app's
  initial rather than a box. Verified per-cell in `test_live_adapter.py` §10b.

## The avatar is a soft body, not a sticker

The face is not a rect that slides around. Two springs run every frame and the
head capsule is drawn as a **sheared, squashed rounded polygon** built from them:

* **tilt** shears the top of the head sideways, so it leans. Emotion adds to
  it (thinking tips one way, loving sways, error shivers) and a nod impulse
  fires when a reply lands.
* **squish** widens and shortens it while holding the area roughly constant
  (`|Δarea| < 25%` in `check_liveliness.py`), which is what makes it read as a
  soft body deforming rather than an image being resized.
* Both are real springs (`k=30`, `c=6`), so they **overshoot and settle**
  instead of easing to a stop dead flat. That overshoot is most of the weight.

Also: a sheen that slides as the head tilts, ears that flick when it shrugs,
breathing, blinks that sometimes **wink** (30% of blinks, one eye re-opens
early), a nod on every reply and a startle pop when an action starts.

Thinking and speaking both deform it, measurably: mean |tilt| goes
`0.084 → 0.284` when the emotion turns to thinking, and mean |squish|
`0.035 → 0.086` while it is speaking.

| Feature | What it does |
|---|---|
| Startup splash | The capsule grows to 180 px and the mascot orbits in on a shrinking spiral, overshoots as it lands, then the panel settles back to 76 px. `skip_splash()` jumps past it for renders/tests. |
| Activity states | `idle` / `working` / `done`, derived from `state`. Each is a *pose* (tilt, bob, arm lift, ring) and they **cross-fade** — `working` sweeps a rotating arc around the head, `done` leaves a green ring that fades. Nothing snaps. |
| Panel transitions | The expanded view fades and slides up on open, the tab bar drops in, and switching tabs **cross-slides** in the direction you switched. Closing animates too — routing is gated on `expanded_open or _expanded_anim > 0.002`, so the panel is still drawn (and still swallows clicks) for ~0.6 s after you press E instead of blanking on that frame. Draw and hit-test share one `_expanded_body()` rect, and the draw is clipped to the capsule — otherwise a mid-animation row would paint outside the island and the row you see would not be the row you click. |

Frames: `render_check/splash_frames.png`, `activity_states.png`,
`tilt_squish.png`. Checks: `scratch/check_liveliness.py` (63 assertions).

## How it is connected

`phoenix_live.py` is the only new part: it loads the island class out of `M.PY`
(upper-case extension, so it uses an explicit source loader), hands it the
callbacks and drives it from the state the bridge already produces.

```
bot_mk8_vlm --(4 hooks)--> IslandState --(IslandFeed, 8 Hz)--> island.set_state()
                                      set_emotion() / set_thought()
                                      set_action() / set_speech()

island.on_user_input  ->  BotBridge.submit()  ->  bot.submit_text(text)
island.on_interrupt   ->  bridge.stop() + bot.ctm.interrupt_now("user_cancel")
```

| production seam | what the island does with it |
|---|---|
| `model.chat_stream(...)` | every token → the thought line while it writes, then the reply |
| `log_event(component, msg)` | real narration → the action chip (`NEEDLE`, `TASK`, `VISION`) |
| `speak(text)` | the sentence → the speech line and the speaking mood |
| `status_overlay.set_state` | `working` / `waiting` → the mood the island renders |

The bot's own dot (`StatusOverlay`) is replaced by a passive object, so only one
front end is ever on screen.

The `Step n/24` badge counts the actions the bot has actually reported **this
turn** — one chip per `log_event` line (`IslandState.action_count`, reset every
message) — and 24 is the same ceiling the bot's own runaway guard uses
(`task_state max_steps=24` in `bot_mk8_vlm.py`).

## What the island tells the bot (`island_brain.py`)

Four more wrappers, installed on the same untouched production module:

* **The user's words become the face.** Every message is classified **once** by
  NEEDLE (`needle.extract`, one shot, ~0.1-0.6 s, cached per text) into an
  emotion, a `needs_screen` flag and any app it names. The label goes straight to
  `island.set_emotion()` and is held for 4 s so the bot's own mood does not
  overwrite it instantly. No second guess: whatever NEEDLE says is what is shown,
  so a wrong label is possible — that was a deliberate choice. `error` is in the
  classifier's vocabulary too, for messages that report a failure.
* **Failures have a face of their own.** The bot's `alert` mood (a failed
  action, a rejected click, an exception in a turn) maps to M.PY's new `error`
  expression: a shivering head, wide red eyes, a `!` badge, a red border and
  `Something went wrong - checking it` on the idle pill. It stays until the next
  turn starts, which clears it. `key 9` puts it on by hand.
* **A simple query never parses the screen.** `needs_vision` is wrapped: when
  NEEDLE says `needs_screen: false` and the text has no action word
  (`click`, `type`, `open`, `play`, …), the turn runs with `Vision: False` — no
  OCR, no YOLO, no DOM. Seen in the log: `[LLM] Generation 1 started (Vision: False)`
  for *"hey good morning!"*. A proactive turn and any action word always keep it on.
* **The model sees *behind* the island, not the island.** For scene-change
  detection the HUD is just painted out (so the island animating can no longer
  fire a `window_and_scene_changed` interrupt on its own), but everything that
  reaches the model — `_thumb`, `check_ocr_box`, `get_som_labeled_img` — gets the
  **real background**: the island hides itself for one capture and the true
  pixels are pasted in, so a search icon sitting under the capsule is visible and
  clickable instead of being a black hole. A just-captured background is reused
  for 1.5 s, so a turn that parses three times causes one blink, not three.
  Frames are repaired in place, the stored `latest_frame` keeps its black hole
  (verification thumbs stay stable), and webcam frames are left alone.
* **While the bot works, the capsule gets out of the way.** The feed toggles
  `WS_EX_TRANSPARENT` off `busy`: a running turn turns the island click-through,
  so the bot's own clicks fall through it to what is really underneath. The
  moment the turn ends it is interactive for you again.
* **Two standing rules ride along in the prompt.** The island is named as the
  bot's own HUD (never click it, never treat it as a window — click the real
  target's coordinates underneath), and the bot is told plainly **it does not
  need to type everything**: never type a reply, greeting or explanation into an
  app as an ACTION — answer in chat, and only type what the app itself needs.
* **The app you clicked is told to the bot.** A click on the grid (or the app
  named in a sentence) becomes the current app for 15 minutes; it goes into the
  system prompt as an `<island_context>` block which says a request that names no
  other app targets it, e.g. *play a song* means play it in **YouTube**, and the
  bot must **not** launch Spotify for it.

## Env

| variable | effect |
|---|---|
| `ISLAND_UI_ONLY=1` | no bot: a scripted turn, to watch the island alone |
| `ISLAND_ASK="..."` | one real turn the moment the bot is ready, then quit |
| `ISLAND_LIVE_SECONDS=N` | auto-quit after N seconds |
| `PHOENIX_NO_VISION=1` | skip `start_vision()` — never captures the screen |
| `PHOENIX_ISLAND_FILE=…` | use a different island file (default `<root>\M.PY`) |

## If the island shows "Brain offline"

That is the bot's own failure, reported honestly on the island.  The one seen on
this machine is llama-server failing to start with `cudaMalloc failed: out of
memory` while the bot's vision models are loaded — the production stdin path
(`python bot_mk8_vlm.py`) fails exactly the same way, so it is not the island.
Free the GPU (a reboot clears it fastest) or lower `PHOENIX_N_CTX` /
`PHOENIX_GPU_LAYERS`, as the bot's own error message suggests.

Keep the vision loop on for real turns.  With `PHOENIX_NO_VISION=1` one run died
silently inside the bot's `Extracting Screen DOM...` step — no traceback, the
process just vanished.  That variable is for demos and checks, not for turns.

## The island is semi-transparent, and the text stays readable

Settings -> *Semi-transparent window* switches the capsule from colorkey to
real per-pixel alpha (`UpdateLayeredWindow`), so the desktop shows through the
panel while the rounded corners stay rounded.

This path failed twice before. The cause was one line: in the Win32
`BITMAPINFOHEADER`, **`biPlanes` and `biBitCount` are WORD (16-bit), not
DWORD.** Declaring them 32-bit made the struct 44 bytes where Windows demands
exactly 40, so `CreateDIBSection` rejected the bogus `biSize` and returned
NULL - *without setting a last-error code*, which is why the log only ever
said "err 0" and two attempts dead-ended there. `M.PY` now asserts
`sizeof(BITMAPINFOHEADER) == 40` at runtime so this can never silently
regress, and the failure path logs a full traceback instead of a bare
`str(e)`.

### How translucent it is allowed to be

Over a white desktop an opaque panel pixel composites to
`a*C + (1-a)*255`, so the darkest background reachable at alpha `a` is
`(1-a)*255`. That floor - not taste - sets the limit. Measured against the
island's real text colours, worst case (a white desktop):

| Text colour | opaque | at the 0.85 floor |
|---|---|---|
| title (245,248,255) | 18.3:1 | 12.2:1 |
| body (240,245,255) | 17.8:1 | 11.9:1 |
| muted (160,168,185) | 8.1:1 | 5.5:1 |
| dimmest (150,158,175) | 7.2:1 | **4.84:1** |

The dimmest text stays readable down to alpha 0.84 and breaks at 0.83, so the
floor is 0.85 and the shipped default is 0.86. Every one of those clears WCAG
AA (4.5:1) against the brightest possible backdrop. Verified in
`scratch/check_translucency.py`.

## The logs are in the main app, not on the island

The island is a HUD: nowhere to scroll, nowhere to copy a line out of, and
it sits on top of the screen you are trying to debug.  So the logs live in
the main app window's **Logs** tab, which tails three files:

| Source | File | What it is |
|---|---|---|
| Bot | `~/.phoenix/bot.log` | PHOENIX's own stdout/stderr |
| Server | `llama_server.log` | llama.cpp: model load, slots, tokens/sec |
| Island | `phoenix_island.log` | the Dynamic Island's debug trace |

![the Logs tab](island_clone/render_check/logs_tab.png)

The bot never had a log file - it just `print()`s, and `run_island.bat` sends
that to a console you cannot scroll while the island is up.  `bot_mk9.py` is
**not edited**.  Instead `phoenix_live.main()` installs a `Tee` on
`sys.stdout`/`sys.stderr` before anything prints, so the launcher's lines and
the bot's land in one UTF-8 file.  The console copy is best effort: this
machine's console is cp1252 and cannot print an emoji, so one gets escaped
rather than taking the launcher down mid-turn.

`LogTail` (in `phoenix_logs.py`, deliberately Tk-free so it can be tested on
its own) follows a file the way `tail -f` does, but bounded - it keeps at
most 4000 lines and never reads more than 1 MB per poll, so a log that grows
to hundreds of megabytes over a week cannot freeze the window.  It survives
the three things that actually happen to log files:

* **being cleared** - the offset is past the new end of the file, so it
  re-reads from the start
* **a half-written last line** - held back until the rest of it arrives, so
  the final line does not flicker
* **a path that is not a file at all** - reports it instead of silently
  showing nothing forever

Levels are read out of the real formats: llama.cpp's
`0.04.948.628 I srv message` and the island's `[2026-10-04 15:56:02]`, with
errors/warnings in red and amber, and the timestamp in its own dim column.
A **Find** box filters, **Follow** auto-scrolls and turns itself off when
you scroll up to read something, and **Reread** goes back to the top of the
file.

## The reasoning is a dropdown, above the answer

The bot's `<thought>` used to be a flat line in the island header, cut at
**65 characters**.  That is why a thought "sometimes shows": either it was
short enough to fit the cut and appeared chopped mid-sentence, or it was not
and did not appear at all - and either way it shared a 20px strip with the
title and the step badge.

It is now a dropdown:

* **Folded** (the default) the header carries a `REASONING >` chip: a
  one-line preview, cut to the **pixel** rather than to 65 characters, plus
  the length in characters.  So you can tell there is something behind it
  without it taking up the screen, and a long unbreakable word still cannot
  run off the edge.
* **Opened** by clicking the chip, the capsule grows from 294 to 470 and the
  full thought gets its own wrapped, scrollable panel **directly above the
  agent's answer**.  The answer moves down rather than being squeezed or
  overlapped, and it keeps its full 120px.
* Clicking the open panel folds it away again; the wheel scrolls the
  reasoning while the pointer is over it (and only when it is open).
* A `waiting` or `done` state folds it automatically, so it can never be
  left hanging over a collapsed capsule.
* A new thought arriving never opens it by itself.  The user opens it.

`scratch/check_thoughts.py` proves the folded sheet really has no panel on
it, that the panel sits above the answer and does not squeeze it, that the
chip cannot be stolen by the avatar hit box or the step badge, and that
clicking each of those three does the right thing.

## The main app window, and the mascots

The island is a HUD.  Everything that is not a HUD belongs in a real window,
so there is now one: `phoenix_control.py`, a normal tkinter desktop window
with three tabs.

    python island_clone\phoenix_live.py         (the island launches it for you)
    python island_clone\phoenix_control.py      (or run it on its own)

**Mascots** — a gallery.  Each mascot carries its own name, personality,
head colour, accent colour, shape, emoji and size.  Click a card to put that
mascot on screen in the island; the panel on the right edits the one you
picked, live.  Add, Duplicate and Delete are there, and the last mascot can
never be deleted or swapped out from under the island.

    shape     blob (the original soft capsule), circle, hex, diamond,
              triangle, star, square, or emoji
    size      0.6x to 1.8x, applied on top of whatever scale the island is
              already drawing at, so a big mascot stays big in the idle view,
              the expanded view and the file-pickup animation
    emoji     an emoji head replaces the capsule entirely; on any other shape
              it becomes a small badge off the top-right corner

**Models** — every GGUF `scan_models()` finds, which one the brain is pointed
at, and the Hugging Face download queue (`start_model_download`, unchanged).

**Settings** — the island's feature switches, the voice toggle, and the panel
opacity slider, all wired straight to `setting_toggles`.

The **personality picker lives here now**, not in the island.  The island's
Settings tab and its shortcut panel both used to carry six personality chips;
they now carry a single *Phoenix main app* row that opens this window, and
**M** does the same from anywhere on the island.

**Closing this window closes nothing else.**  The island keeps rendering, stays
always-on-top and keeps its bot connection.  Press **M** to bring the window
back.  Verified in `test_control_app.py`.

### The window is glass

The main app window is semi-transparent: you see the desktop, and Windows
blurs it, through the panels.  Two separate mechanisms, both in
`phoenix_glass.py`:

* **Level** — `SetLayeredWindowAttributes(LWA_ALPHA)`, plus Tk's own
  `-alpha`.  This is what makes the window see-through.
* **Backdrop** — DWM, so the desktop is *blurred* rather than just sharp
  behind the glass.  The native Win11 `DWMWA_SYSTEMBACKDROP_TYPE`
  (acrylic) is tried first; the undocumented `SetWindowCompositionAttribute`
  accent states are the fallback for older builds.  On this machine
  (Windows 11 24H2, build 26300) the native acrylic takes.

Settings → **This window** has a Glass on/off box and a Transparency slider.
The level is remembered in the store, so the window comes back the way you
left it.

**Why the floor is 0.80 and not lower.**  The obvious way to build glass -
lighten the panels - makes it *less* readable, because a lighter panel eats
the contrast with light text.  So this palette goes the other way: the
backgrounds are deep and slightly blue, and the muted text is lighter than it
used to be.  Every text/background pair the window draws is then composited
against a pure white desktop - the worst possible backdrop - and measured:

| level | worst pair | |
|---|---|---|
| 1.00 | 7.24:1 | opaque |
| 0.88 | **5.55:1** | the shipped default |
| 0.85 | 5.14:1 | |
| 0.80 | **4.51:1** | the floor, WCAG AA |

The old palette could only have gone to 0.93.  The deeper one reaches 0.80,
and that headroom is what buys a translucency you can actually notice.  The
slider is clamped there, so the text can never be made unreadable.

### How the two halves stay in step

`phoenix_store.py` owns the shared state, in `~/.phoenix/control.json`, and is
dependency-free on purpose: it is imported from the project root (by the
island's loader), from `island_clone\` (by the window) and directly by the
offline checks.  A hand-edited or truncated file cannot take the island down —
bad entries are dropped and replaced by defaults.  The window also polls the
file every two seconds, so a mascot edited anywhere shows up in both places.

### Threading

Tk insists a window be built and driven from the thread that created it, and
the island already owns the main thread (`phoenix_live.run()`).  So the window
is **built on the main thread before the render loop starts** and then
**pumped from inside it**: `M.PY` calls a new `on_frame()` callback once per
rendered frame, and the launcher points it at `PhoenixControl.pump`, which
throttles Tk to 30 Hz and swallows every exception.  There is no `mainloop()`
anywhere on the live path.  If Tk cannot start at all, the island simply runs
without the window.

## Tests

```
python island_clone\test_island_bridge.py     # the 4 hooks, against a fake bot
python island_clone\test_live_adapter.py      # M.PY's real class + the feed mapping
python island_clone\test_island_brain.py      # emotion / screen gate / mask / app context
python island_clone\test_window_flags.py      # click-through + hide, on a real window
python island_clone\test_control_app.py       # the window, the mascots, the moved picker
python -X utf8 scratch\check_glass.py         # the glass level, the backdrop, the floor
python -X utf8 scratch\check_thoughts.py      # the reasoning dropdown, folded and open
python -X utf8 scratch\check_logs.py         # the log tail, the tee, and the Logs tab
```

or the whole offline suite at once:

```
bash scratch/run_offline_suite.sh
```

`test_island_bridge.py` proves the hooks carry the bot's signal.  `test_live_adapter.py`
loads the real `M.PY`, feeds a fake bot through the real bridge, and inspects the
island's own message queue: state, emotion, thought tail, action chips, speech,
the refusal while busy, Esc-cancels-vs-quits, and that a broken island cannot
take the feed down.

`test_control_app.py` builds the real tkinter window and closes it again, so
"closable while the island keeps running" is proved rather than asserted: it
checks that `island.running` is untouched, that the store survives a round trip
and refuses to be broken, that every shape really paints different pixels, that
colour and size really reach those pixels, that the chips are gone from both
island surfaces, that **M** opens the window through the real event handler,
and that `Live.attach_control()` wires the whole thing up.

`check_logs.py` proves the three sources resolve, that levels and timestamps
are parsed out of the real llama.cpp and island formats, and that a `LogTail`
is bounded, survives its file being cleared, rotated away and half-written,
and that the `Tee` really does capture a `print()` into a file while
escaping - not raising on - an emoji headed for a cp1252 console.  It also
builds the real window and asserts the Logs tab reads a file that grows.

`check_glass.py` splits in two.  The maths half is deterministic and finds
the readability floor.  The window half builds the real Tk window and reads
the compositing alpha **back** with `GetLayeredWindowAttributes`, because a
successful `SetLayeredWindowAttributes` only means the call was accepted -
the read-back is what Windows is really using.

It deliberately does *not* assert on a photograph of the window.  Measuring
screen pixels looked like the obvious check and is a trap: whatever else is
open ends up on top, DWM's acrylic adds a tint wash of its own, and every
capture races the compositor.  Three attempts at it gave three different
answers for the same window, which is exactly why the assertion reads the
attribute instead.  `render_mascots.py` still saves a computed preview of the
glass look (`glass_preview_*.png`) - clearly labelled as computed, not
photographed - so you can see the effect without launching anything.

## The other front ends

* `phoenix_island.py` — the earlier pygame + moderngl island: a chat panel with a
  transcript, a GPU starfield and spring physics.  `..\run_island_classic.bat`.
* `island_ui.py` / `island_frontend.py` / `island_gl.py` / `phoenix_frontend.py` —
  the Qt/QOpenGL variant with its own offscreen tests; superseded.
* `bot_island.py`, `demo_video.py`, `assets/` — the first HUD and the demo-video
  replica this all started from.

`render_check/` holds the screenshots the render tests wrote.
