"""
llm_server.py -- llama-server subprocess manager for PHOENIX MK4.

Spawns llama.cpp's `llama-server.exe` with the Qwen3-VL-4B GGUF + mmproj vision
projector and exposes a tiny client that returns llama-cpp-python-shaped
chunks, so the bot's existing streaming/TTS/thought-filter code works
unchanged after the swap from in-process inference.

Config (all env-overridable):
  PHOENIX_LLAMA_SERVER  path to llama-server.exe        (required; fail-loud)
  PHOENIX_MODEL         main GGUF path                  (default: searched in Downloads)
  PHOENIX_MMPROJ        vision projector GGUF path      (default: searched in Downloads)
  PHOENIX_SERVER_PORT   port to bind                    (default 8123)
  PHOENIX_N_CTX         context size                    (default 8192)
  PHOENIX_GPU_LAYERS    layers offloaded to GPU         (default 99)

VRAM budget on the RTX 4050 (6 GB): 4B Q4_K_M (~2.4 GB) + mmproj Q8_0
(~0.45 GB) + q8_0 KV cache @ 8k (~1.2 GB) + buffers. If load fails with OOM:
drop PHOENIX_N_CTX to 6144, then PHOENIX_GPU_LAYERS to ~28.
"""

import atexit
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import requests

DEFAULT_PORT = int(os.environ.get("PHOENIX_SERVER_PORT", "8123"))
DEFAULT_CTX = int(os.environ.get("PHOENIX_N_CTX", "8192"))
DEFAULT_NGL = int(os.environ.get("PHOENIX_GPU_LAYERS", "99"))
DOWNLOADS = Path.home() / "Downloads"

_HEALTH_TIMEOUT = 3.0


class LlamaServerRejectedGrammar(Exception):
    """The server refused the request's `grammar` field (old llama.cpp build)."""


def _log(msg):
    try:
        ts = time.strftime("%H:%M:%S")
        print(f"[{ts}] [SERVER] {msg}")
    except Exception:
        pass


def find_model_file():
    """Locate the main Qwen3-VL-4B GGUF (never an mmproj file).

    Preference tiers: qwen3vl-named > q4_k_m > largest. A Downloads folder
    full of other Q4 models (gemma etc.) must never win over the MK4 brain.
    """
    env = os.environ.get("PHOENIX_MODEL")
    if env and Path(env).is_file():
        return Path(env)
    candidates = []
    for base in (DOWNLOADS / "qwen3vl-4b", DOWNLOADS):
        if base.is_dir():
            for p in base.glob("*.gguf"):
                if "mmproj" not in p.name.lower():
                    candidates.append(p)
    candidates.sort(key=lambda p: ("qwen3vl" not in p.name.lower(),
                                   "q4_k_m" not in p.name.lower(),
                                   -p.stat().st_size))
    return candidates[0] if candidates else None


def find_mmproj_file():
    env = os.environ.get("PHOENIX_MMPROJ")
    if env and Path(env).is_file():
        return Path(env)
    for base in (DOWNLOADS / "qwen3vl-4b", DOWNLOADS):
        if base.is_dir():
            for p in sorted(base.glob("mmproj*.gguf")):
                if "qwen3vl" in p.name.lower():
                    return p
            # Fall back to any mmproj for a 4B-class model if the name does
            # not literally contain qwen3vl (renamed downloads).
            for p in sorted(base.glob("mmproj*.gguf")):
                if "4b" in p.name.lower():
                    return p
    return None


class LlamaServerManager:
    """Owns the llama-server process; safe to restart; one shared slot."""

    def __init__(self, server_path=None, model_path=None, mmproj_path=None,
                 port=DEFAULT_PORT, ctx=DEFAULT_CTX, ngl=DEFAULT_NGL,
                 start_on_init=True, health_timeout=300.0,
                 vision_on_boot=True):
        self.server_path = server_path or os.environ.get("PHOENIX_LLAMA_SERVER")
        self.model_path = Path(model_path) if model_path else find_model_file()
        self.mmproj_path = Path(mmproj_path) if mmproj_path else find_mmproj_file()
        self.port = port
        self.ctx = ctx
        self.ngl = ngl
        self.health_timeout = health_timeout
        # The vision encoder (mmproj) lives INSIDE this process, so it can only
        # be added or dropped by restarting. vision_on_boot=False starts a
        # text-only brain; ensure_vision() adds the encoder when a turn needs
        # it. Defaults to True, which is exactly the old always-vision boot.
        self.vision_enabled = bool(vision_on_boot)
        self._live_vision = None      # what the RUNNING process actually has
        self.proc = None
        self._log_fp = None
        self._lock = threading.Lock()
        self._watchdog_started = False
        self._atexit_done = False
        self._shutdown = False
        self._log_path = Path(__file__).resolve().parent / "llama_server.log"
        self.base_url = f"http://127.0.0.1:{port}"
        if start_on_init:
            self.start_or_restart(wait=True)
            self._start_watchdog()
            atexit.register(self.shutdown)

    # ---------------- process lifecycle -----------------------------------
    def _build_cmd(self):
        """Build the llama-server argv.

        The mmproj (vision encoder) and its image-token floor are only passed
        when vision is enabled, so a text-only boot never pays for the encoder.
        """
        cmd = [
            self.server_path,
            "-m", str(self.model_path),
            "--port", str(self.port),
            "--ctx-size", str(self.ctx),
            "-ngl", str(self.ngl),
            "--flash-attn", "on",   # newer builds take on|off|auto
            "--jinja",              # native chat template (required for VLM)
        ]
        if self.vision_enabled:
            cmd += ["--mmproj", str(self.mmproj_path)]
        cmd += [
            "--cache-type-k", "q8_0",
            "--cache-type-v", "q8_0",
        ]
        if self.vision_enabled:
            # Qwen-VL grounding accuracy needs >=1024 image tokens (llama.cpp
            # #16842); the server warns and mis-grounds without this.
            cmd += ["--image-min-tokens", "1024"]
        cmd += ["--parallel", "1", "--no-webui"]
        return cmd

    def has_vision(self):
        """True only if the RUNNING process was spawned with the encoder."""
        return bool(self._live_vision)

    def ensure_vision(self, wait=True):
        """Add the vision encoder, restarting the server if it is text-only.

        Returns True when the live server can actually accept images. Costs one
        model reload (~10-15s), which is why it stays warm once enabled.
        """
        if self.has_vision() and self.is_alive():
            return True
        if not self.vision_enabled:
            self.vision_enabled = True
            _log("vision requested: restarting llama-server WITH --mmproj")
        self.start_or_restart(wait=wait)
        self._start_watchdog()
        return self.has_vision()

    def start_or_restart(self, wait=True):
        with self._lock:
            self._stop_locked()
            required = [
                ("llama-server.exe (set PHOENIX_LLAMA_SERVER)", self.server_path),
                ("model GGUF (PHOENIX_MODEL)", self.model_path),
            ]
            if self.vision_enabled:
                # Only the vision path needs the projector, so a text-only
                # boot must not fail just because the mmproj is absent.
                required.append(("mmproj GGUF (PHOENIX_MMPROJ)", self.mmproj_path))
            missing = [label for label, path in required
                       if not path or not Path(path).is_file()]
            if missing:
                raise RuntimeError(
                    "llama-server cannot start, missing: " + "; ".join(missing)
                    + ". Download the Qwen3-VL-4B GGUFs and a llama.cpp Windows "
                      "CUDA build (b November 2025 or newer - Qwen3-VL support "
                      "landed then) and point PHOENIX_LLAMA_SERVER at "
                      "llama-server.exe."
                )
            if getattr(self, "_log_fp", None) is not None:
                try:
                    self._log_fp.close()
                except Exception:
                    pass
            self._log_fp = open(self._log_path, "ab", buffering=0)
            self._log_fp.write(f"\n===== spawn {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n".encode())
            self.proc = subprocess.Popen(
                self._build_cmd(),
                stdout=self._log_fp,
                stderr=self._log_fp,
                cwd=str(Path(self.server_path).parent),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            _log(f"spawned pid {self.proc.pid} (ctx={self.ctx}, ngl={self.ngl}, "
                 f"vision={self.vision_enabled}) "
                 f"-> {self.base_url} (log: {self._log_path.name})")
            self._live_vision = self.vision_enabled
        if wait:
            self.wait_healthy()

    def wait_healthy(self):
        deadline = time.time() + self.health_timeout
        url = f"{self.base_url}/health"
        while time.time() < deadline:
            if self.proc.poll() is not None:
                tail = self._log_tail()
                raise RuntimeError(
                    f"llama-server exited during startup (code {self.proc.returncode}). "
                    f"Last log lines:\n{tail}\n"
                    "If the log shows an unsupported architecture or vocab, your "
                    "llama.cpp build predates Qwen3-VL support (needs a build "
                    "from November 2025 or newer). If it shows CUDA OOM, lower "
                    "PHOENIX_N_CTX (6144) or PHOENIX_GPU_LAYERS."
                )
            try:
                r = requests.get(url, timeout=_HEALTH_TIMEOUT)
                if r.status_code == 200:
                    _log("healthy")
                    return True
            except requests.RequestException:
                pass
            time.sleep(1.5)
        raise RuntimeError(
            f"llama-server not healthy after {self.health_timeout:.0f}s "
            f"(log: {self._log_path})")

    def _log_tail(self, max_bytes=2000):
        try:
            with open(self._log_path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - max_bytes))
                return f.read().decode("utf-8", errors="replace")
        except Exception:
            return "(no log available)"

    def is_alive(self):
        return self.proc is not None and self.proc.poll() is None

    def _start_watchdog(self):
        if self._watchdog_started:
            return
        self._watchdog_started = True

        def _watch():
            restarts = 0
            while not self._shutdown:
                time.sleep(10)
                try:
                    if self.proc is not None and self.proc.poll() is not None:
                        restarts += 1
                        if restarts > 5:
                            _log("process keeps dying; giving up (check "
                                 "llama_server.log)")
                            return
                        _log(f"watchdog: process died (code "
                             f"{self.proc.returncode}); restarting "
                             f"({restarts}/5)")
                        self.start_or_restart(wait=True)
                except Exception as e:
                    _log(f"watchdog restart failed: {e}")

        threading.Thread(target=_watch, daemon=True).start()

    def ensure_started(self):
        """Full readiness: (re)start if needed, wait for /health, arm watchdog."""
        if self.is_alive():
            try:
                if requests.get(f"{self.base_url}/health",
                                timeout=_HEALTH_TIMEOUT).status_code == 200:
                    return
            except Exception:
                pass
            _log("process alive but unhealthy; restarting")
        self.start_or_restart(wait=True)
        self._start_watchdog()
        if not self._atexit_done:
            self._atexit_done = True
            atexit.register(self.shutdown)

    def ensure_healthy(self):
        """Cheap pre-generation check: restart on death, wait for /health."""
        self.ensure_started()

    def _stop_locked(self):
        # No process => no encoder, whatever vision_enabled says.
        self._live_vision = None
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            except Exception:
                pass
        self.proc = None

    def shutdown(self):
        self._shutdown = True
        with self._lock:
            self._stop_locked()
        if getattr(self, "_log_fp", None) is not None:
            try:
                self._log_fp.close()
            except Exception:
                pass

    # ---------------- chat API --------------------------------------------
    def _payload(self, messages, grammar=None, max_tokens=300, temperature=0.6,
                 top_p=0.9, stop=None, repeat_penalty=None, stream=False):
        body = {
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": top_p,
            "stream": stream,
        }
        if grammar:
            body["grammar"] = grammar
        if stop:
            body["stop"] = list(stop)
        if repeat_penalty is not None:
            body["repeat_penalty"] = repeat_penalty
        return body

    @staticmethod
    def _raise_for_grammar(resp):
        if resp.status_code == 400 and "grammar" in (resp.text or "").lower():
            raise LlamaServerRejectedGrammar(resp.text[:300])

    def chat(self, messages, grammar=None, max_tokens=300, temperature=0.6,
             top_p=0.9, stop=None, repeat_penalty=None):
        """Blocking completion. Returns an OpenAI-shaped dict (compatible with
        the llama-cpp-python non-streaming dict the bot already handles)."""
        self.ensure_healthy()
        resp = requests.post(
            f"{self.base_url}/v1/chat/completions",
            json=self._payload(messages, grammar, max_tokens, temperature,
                               top_p, stop, repeat_penalty, stream=False),
            timeout=600,
        )
        self._raise_for_grammar(resp)
        resp.raise_for_status()
        return resp.json()

    def chat_stream(self, messages, grammar=None, max_tokens=300,
                    temperature=0.6, top_p=0.9, stop=None, repeat_penalty=None):
        """SSE generator yielding llama-cpp-python-shaped stream chunks:
        {"choices": [{"delta": {"content": "..."}}]} -- the exact shape the
        bot's stream loop already consumes."""
        self.ensure_healthy()
        resp = requests.post(
            f"{self.base_url}/v1/chat/completions",
            json=self._payload(messages, grammar, max_tokens, temperature,
                               top_p, stop, repeat_penalty, stream=True),
            stream=True,
            timeout=600,
        )
        self._raise_for_grammar(resp)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        for raw in resp.iter_lines(decode_unicode=True):
            if not raw or not raw.startswith("data:"):
                continue
            data = raw[len("data:"):].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            choices = obj.get("choices") or [{}]
            delta = (choices[0].get("delta") or {}).get("content")
            if delta:
                yield {"choices": [{"delta": {"content": delta}}]}
