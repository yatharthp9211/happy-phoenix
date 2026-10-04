import sys
import threading
import queue
import time
import re
from kokoro import KPipeline
import sounddevice as sd
import warnings

warnings.filterwarnings("ignore", category=UserWarning, module="torch")
warnings.filterwarnings("ignore", category=FutureWarning, module="torch")
warnings.filterwarnings("ignore", category=FutureWarning, module="transformers")

# Global state
tts_queue = queue.Queue()
pipeline = None
# The parent passes the voice on argv; it used to be ignored entirely,
# so VOICE_ID in bot_mk9.py had no effect at all.
voice_name = sys.argv[1] if len(sys.argv) > 1 else 'af_sarah'

def stdin_reader():
    while True:
        try:
            raw_line = sys.stdin.buffer.readline()
            if not raw_line:
                tts_queue.put("QUIT")
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
            try:
                sd.stop()
            except Exception:
                pass
        elif line.startswith("SPEED|"):
            parts = line.split("|", 2)
            if len(parts) == 3:
                speed = float(parts[1])
                text = parts[2]
                tts_queue.put((text, speed))
        elif line.startswith("SPEAK|"):
            text = line[6:]
            tts_queue.put((text, 1.0))

def humanize_text(text):
    text = re.sub(r'\*\*(.*?)\*\*', r'\1', text)
    text = re.sub(r'`(.*?)`', r'\1', text)
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'\[.*?\]', '', text) # Remove brackets like [Chat Log End]
    
    abbreviations = {
        " vs ": " versus ",
        " approx ": " approximately ",
        " info ": " information ",
        " temp ": " temperature ",
        " max ": " maximum ",
        " min ": " minimum ",
        " API ": " A P I ",
        " URL ": " U R L "
    }
    for old, new in abbreviations.items():
        text = text.replace(old, new)
        
    text = text.replace('"', ', ')
    
    replacements = {"&": " and ", "@": " at ", "%": " percent ", "₹": " rupees ", "$": " dollars ", "#": " number ", "...": ", "}
    for old, new in replacements.items():
        text = text.replace(old, new)
        
    text = re.sub(r'([!?.,])\1+', r'\1', text)
    return text.strip()

def main():
    global pipeline
    
    # Initialize Kokoro on CPU (Dual Language)
    print("Loading Kokoro TTS Pipelines (English & Hindi)...")

    def _load(lang):
        """Load one pipeline, trying the device kwarg then without it.

        Every failure is contained: an older kokoro without `device=`, a
        missing voice file, or no network for the model must NOT take the
        whole process down, or the parent respawns us forever.
        """
        for kwargs in ({"device": "cpu"}, {}):
            try:
                return KPipeline(lang_code=lang, repo_id="hexgrad/Kokoro-82M",
                                 **kwargs)
            except TypeError:
                continue          # this build does not know the kwarg
            except Exception as e:
                print(f"TTS: pipeline '{lang}' failed: {type(e).__name__}: {e}")
                return None
        return None

    pipeline_en = _load('a')
    pipeline_hi = _load('h')

    if pipeline_en is None and pipeline_hi is None:
        # Stay ALIVE and keep draining stdin. A dead worker is worse than a
        # silent one: the parent restarts it on every single utterance, and
        # each restart repeats this same failure - that was the crash loop.
        print("TTS: no Kokoro pipeline could be loaded; speech is disabled.")
        while True:
            try:
                line = sys.stdin.buffer.readline()
                if not line:
                    return
                cmd = line.decode("utf-8", errors="replace").strip()
                if cmd == "QUIT":
                    return
            except Exception:
                return
    if pipeline_en is None:
        pipeline_en = pipeline_hi          # Hindi can read Latin text too
    if pipeline_hi is None:
        pipeline_hi = pipeline_en

    # Start reader thread
    t = threading.Thread(target=stdin_reader, daemon=True)
    t.start()
    
    while True:
        item = tts_queue.get()
        
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
        
        # Check if text contains Devanagari characters (Hindi)
        is_hindi = any('\u0900' <= c <= '\u097F' for c in text)
        
        try:
            generator = (pipeline_hi if pipeline_hi is not None
                         else pipeline_en)(
                text, 
                voice='hm_omega', # Default Hindi Male
                speed=speed, 
                split_pattern=r'\n+'
            ) if is_hindi else pipeline_en(
                text, 
                voice=voice_name, 
                speed=speed, 
                split_pattern=r'\n+'
            )
            
            for graphemes, phonemes, audio in generator:
                # Check for interruption
                if tts_queue.qsize() > 0:
                    peek = tts_queue.queue[0]
                    if peek == "STOP":
                        break
                        
                try:
                    sd.play(audio, samplerate=24000)
                    sd.wait() # This will block until finished, but is interrupted by sd.stop() in the reader thread
                except Exception:
                    sd.stop()
                    continue
        except Exception as e:
            pass # Ignore generation errors

if __name__ == "__main__":
    main()
