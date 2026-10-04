# Happy Phoenix — Personal Local AI Assistant & Dynamic Island HUD

**Happy Phoenix** is an autonomous, local desktop AI assistant powered by Vision-Language Models (VLM), desktop automation drivers, neural TTS, and an always-on-top, borderless **ModernGL Dynamic Island HUD** that floats on your desktop and reflects the assistant's live thought stream, actions, and spoken responses in real time.

---

## 🌟 Key Highlights

- **Desktop Dynamic Island HUD (`M.PY`)**:
  - Borderless, transparent floating capsule window powered by **ModernGL 3.3 Core Profile** (with automatic crash-proof Pygame software fallback).
  - **Framer Motion Spring Physics**: Smooth layout resizing when shifting between states (`waiting` $\leftrightarrow$ `thinking` / `acting`) using an analytical damped harmonic oscillator with continuous momentum preservation, tuned stiffness, damping, and organic settling.
  - **Soft-Body Mascot Avatar**: Sheared and squashed procedural polygon mesh with eye-tracking that follows your desktop cursor, blinks with occasional winks, reacts with 8 distinct emotions, and tilts/squishes expressively when thinking or speaking.
  - **Interactive File Pickup Animation**: Drag any real file directly onto the island; the bot opens a top slot, takes the file in, jumps left, and transforms into a progress runner with glowing particle trails.
  - **App Shortcuts Grid & Settings**: Quick launch WhatsApp, Spotify, YouTube, Browser, Snapchat, Discord, Terminal, and VS Code with customizable pinned apps via the top-right settings gear (⚙️).
  - **Expandable Reasoning & Chat Panel**: Full word-wrapped turn conversation history with mouse-wheel and PageUp/PageDown scrolling; collapsible reasoning thoughts dropdown (`720x470`).

- **Vision-Language Agent Engine (`bot_mk8_vlm.py` / `bot_mk9.py`)**:
  - Full desktop perception using local vision models and OmniParser.
  - Autonomous task planning, visual element grounding, and multi-step UI execution.
  - Real-time token streaming, action narration, and conversational turns.

- **Local Backend & Services**:
  - **`island_clone/phoenix_live.py`**: High-performance live feed connecting the bot bridge and ModernGL island overlay at 8 Hz.
  - **`island_clone/phoenix_control.py`**: Companion control panel for mascot customization, glass blur levels, telemetry, and system diagnostics.
  - **`llm_server.py`**: Local model inference endpoint with streaming support.
  - **`tts_worker_2.py`**: Offline neural text-to-speech engine with live vocal wave animation on the island.
  - **`rag_index.py` & `memory_manager.py`**: Vector retrieval, local document indexing, and persistent memory across sessions.
  - **`control_loop.py` & `app_drivers.py`**: Low-latency desktop input actuation and application drivers.

---

## 📁 Repository Structure

```
.
├── M.PY                        # Core ModernGL Dynamic Island Overlay with Framer Motion spring physics
├── run_island.bat              # One-click Windows launcher
├── requirements.txt            # Python dependencies (pygame, moderngl, etc.)
│
├── island_clone/               # Dynamic Island live integration suite
│   ├── phoenix_live.py         # Live event bridge connecting bot turns to M.PY
│   ├── phoenix_control.py      # Companion Tkinter Control Hub (models, mascots, glass)
│   ├── phoenix_glass.py        # Windows DWM blur / acrylic glass effects
│   ├── phoenix_store.py        # Mascot skins and settings persistence
│   ├── island_bridge.py        # Abstract bridge and state machine (waiting/thinking/acting)
│   ├── island_brain.py         # Screenshot capture, mask filters, and vision hooks
│   └── test_live_adapter.py    # Unit tests for adapter & glyph rendering
│
├── bot_mk8_vlm.py              # Main VLM autonomous desktop agent (MK8)
├── bot_mk9.py                  # Next-generation agent iteration (MK9)
├── control_loop.py             # Desktop task execution loop
├── app_drivers.py              # Application-specific automation drivers
├── nidle_mk4.py                # Human-like mouse and keyboard motion synthesizer
├── llm_server.py               # Local LLM server wrapper
├── tts_worker_2.py             # Background neural TTS worker
├── rag_index.py                # Local RAG vector indexing
├── memory_manager.py           # Long-term conversational memory
├── telemetry.py                # Turn metrics and action telemetry
└── coord_math.py               # Desktop coordinate transforms and DPI math
```

---

## 🚀 Getting Started

### 1. Prerequisites

Ensure you have **Python 3.10+** installed on Windows.

Install required dependencies:
```bash
pip install -r requirements.txt
```

### 2. Launching the Assistant

#### Option A: One-Click Launcher
Double-click `run_island.bat` in the root folder.

#### Option B: Live Bot Integration
Run the Phoenix Live adapter to start the assistant with the Dynamic Island:
```bash
python island_clone\phoenix_live.py
```

#### Option C: Standalone Dynamic Island HUD
To run the ModernGL Dynamic Island overlay standalone for testing or customization:
```bash
python M.PY
```

---

## ⌨️ Controls & Shortcuts

| Key / Action | Description |
|---|---|
| **Click Island** or **Enter** | Activate chat input bar to type a task or prompt directly to Phoenix. |
| **Esc** | Cancel active bot turn (`interrupt_now`), close chat input, or fold open panels. |
| **Mouse Wheel** / **PgUp / PgDn** | Scroll chat conversation panel or unfolded reasoning thoughts. |
| **V** | Toggle voice TTS output and live speech audio bars. |
| **M** | Open the companion **Phoenix Control Hub** window (skins, models, glass). |
| **E** | Toggle the **Expanded Island Hub** with GGUF model browser and logs. |
| **Drag & Drop File** | Drop any file onto the island to trigger the letterbox intake sequence and task prompt. |
| **Avatar Poke** | Click the mascot face to trigger playful reactions or dizzy physics when clicked rapidly. |

---

## 🧬 Dynamic Island Resizing Physics

The Dynamic Island transitions dynamically between states using **Framer Motion spring physics**:

- **Idle Waiting (`waiting` / `done`)**: Compact pill capsule (`540 × 76 px`, corner radius `28 px`).
- **Thinking & Acting (`working` / `thinking` / `acting`)**: Expands to full action card (`720 × 294 px`, corner radius `30 px`) accommodating the live token stream, action chips, and 120 px chat conversation panel.
- **Unfolded Thoughts (`toggle_thoughts`)**: Expands to deep reasoning mode (`720 × 470 px`).
- **Spring Formulation**: Built with exact damped harmonic oscillator integration ($m \ddot{x} + c \dot{x} + k(x - x_{\text{target}}) = 0$) preserving velocity across interruptions for seamless, tactile momentum.

---

## 🛡️ License

Private repository for Happy Phoenix / Phoenix Desktop Assistant. All rights reserved.
