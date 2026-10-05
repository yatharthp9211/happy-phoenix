import sys
import os
import threading
import queue
import time
import re
import warnings

warnings.filterwarnings("ignore", category=UserWarning, module="torch")
warnings.filterwarnings("ignore", category=FutureWarning, module="torch")
warnings.filterwarnings("ignore", category=FutureWarning, module="transformers")

# Safe imports for kokoro & sounddevice
try:
    from kokoro import KPipeline
except Exception:
    KPipeline = None

try:
    import sounddevice as sd
except Exception:
    sd = None

# Windows SAPI5 voice fallback
sapi_voice = None
if sys.platform == "win32":
    try:
        import win32com.client
        sapi_voice = win32com.client.Dispatch("SAPI.SpVoice")
    except Exception:
        sapi_voice = None

# Global state
tts_queue = queue.Queue()
pipeline = None
voice_name = sys.argv[1] if len(sys.argv) > 1 else 'af_sarah'


def stdin_reader():
    while True:
        try:
            raw_line = sys.stdin.buffer.readline()
            if not raw_line:
                tts_queue.put(("QUIT", 1.0))
                break
            line = raw_line.decode('utf-8', errors='replace').strip()
        except Exception:
            break

        if not line:
            continue

        if line == "QUIT":
            tts_queue.put(("QUIT", 1.0))
            break
        elif line == "STOP":
            with tts_queue.mutex:
                tts_queue.queue.clear()
            tts_queue.put(("STOP", 1.0))
            if sd is not None:
                try:
                    sd.stop()
                except Exception:
                    pass
            if sapi_voice is not None:
                try:
                    sapi_voice.Speak("", 2)
                except Exception:
                    pass
        elif line.startswith("SPEED|"):
            parts = line.split("|", 2)
            if len(parts) == 3:
                try:
                    speed = float(parts[1])
                except ValueError:
                    speed = 1.0
                text = parts[2]
                tts_queue.put((text, speed))
        elif line.startswith("SPEAK|"):
            text = line[6:]
            tts_queue.put((text, 1.0))


def humanize_text(text):
    text = re.sub(r'\*\*(.*?)\*\*', r'\1', text)
    text = re.sub(r'`(.*?)`', r'\1', text)
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'\[.*?\]', '', text)  # Remove brackets like [Chat Log End]

    # Clean emojis and high unicode surrogate characters that crash Kokoro / espeak phonemizers
    text = re.sub(r'[\U00010000-\U0010ffff]', '', text)

    # Clean common UTF-8 -> CP1252 mojibake artifacts
    mojibake_map = {
        "â€™": "'",
        "â€˜": "'",
        "â€œ": '"',
        "â€\x9d": '"',
        "â€”": " - ",
        "â€“": " - ",
        "â€": "-",
        "â": "",
        "ð": "",
    }
    for old_m, new_m in mojibake_map.items():
        text = text.replace(old_m, new_m)

    abbreviations = {
        " vs ": " versus ",
        " approx ": " approximately ",
        " info ": " information ",
        " temp ": " temperature ",
        " max ": " maximum ",
        " min ": " minimum ",
        " API ": " A P I ",
        " URL ": " U R L ",
    }
    for old, new in abbreviations.items():
        text = text.replace(old, new)

    text = text.replace('"', ', ')

    replacements = {
        "&": " and ",
        "@": " at ",
        "%": " percent ",
        "₹": " rupees ",
        "$": " dollars ",
        "#": " number ",
        "...": ", ",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)

    text = re.sub(r'([!?.,])\1+', r'\1', text)
    return text.strip()


def main():
    global pipeline

    pipeline_en = None
    pipeline_hi = None

    if KPipeline is not None and sd is not None:
        def _load(lang):
            for kwargs in ({"device": "cpu"}, {}):
                try:
                    return KPipeline(lang_code=lang, repo_id="hexgrad/Kokoro-82M", **kwargs)
                except TypeError:
                    continue
                except Exception as e:
                    return None
            return None

        try:
            pipeline_en = _load('a')
            pipeline_hi = _load('h')
            if pipeline_en is None:
                pipeline_en = pipeline_hi
            if pipeline_hi is None:
                pipeline_hi = pipeline_en
        except Exception:
            pass

    # Start stdin reader thread
    t = threading.Thread(target=stdin_reader, daemon=True)
    t.start()

    while True:
        try:
            item = tts_queue.get()
        except Exception:
            break

        if not isinstance(item, tuple):
            if item == "QUIT":
                break
            continue

        raw_text, speed = item

        if raw_text == "QUIT":
            break

        if raw_text == "STOP":
            continue

        text = humanize_text(raw_text)
        if not text:
            continue

        # Option A: Kokoro neural TTS
        spoken_successfully = False
        if pipeline_en is not None and sd is not None:
            is_hindi = any('\u0900' <= c <= '\u097F' for c in text)
            try:
                gen_pipe = pipeline_hi if (is_hindi and pipeline_hi is not None) else pipeline_en
                v_name = 'hm_omega' if is_hindi else voice_name
                generator = gen_pipe(text, voice=v_name, speed=speed, split_pattern=r'\n+')

                for graphemes, phonemes, audio in generator:
                    if tts_queue.qsize() > 0:
                        peek = tts_queue.queue[0]
                        if isinstance(peek, tuple) and peek[0] == "STOP":
                            break
                    try:
                        sd.play(audio, samplerate=24000)
                        sd.wait()
                        spoken_successfully = True
                    except Exception:
                        try:
                            sd.stop()
                        except Exception:
                            pass
                        break
            except Exception:
                spoken_successfully = False

        # Option B: Fallback to Windows SAPI5 voice if Kokoro fails or is not installed
        if not spoken_successfully and sapi_voice is not None:
            try:
                # 0 = synchronous speech so queue doesn't overlap
                sapi_voice.Speak(text, 0)
                spoken_successfully = True
            except Exception:
                pass


if __name__ == "__main__":
    main()
