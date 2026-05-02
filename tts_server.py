"""
Simple TTS HTTP server using Chatterbox.

POST /tts with JSON {"text": "Hello world"} to generate and play speech.
Optional: {"text": "...", "ref": "/path/to/reference.wav"} for voice cloning.
Full model also supports: {"text": "...", "exaggeration": 0.7, "cfg_weight": 0.5}

GET /tts?text=Hello+world also works for quick testing.
"""

import io
import os
import signal
import subprocess
import sys
import threading
import time
import tty
import termios
import wave

import numpy as np
import torch
from flask import Flask, request, jsonify, Response

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
VOICES_DIR = os.path.expanduser("~/voices")
USE_TURBO = True  # toggled at startup or via env


def _raw_key(fd):
    """Read a single keypress in raw mode. Returns a string like 'up', 'down', 'enter', 'space', or the character."""
    ch = os.read(fd, 1)
    if ch == b"\r" or ch == b"\n":
        return "enter"
    if ch == b" ":
        return "space"
    if ch == b"\x1b":
        seq = os.read(fd, 2)
        if seq == b"[A":
            return "up"
        if seq == b"[B":
            return "down"
        return "esc"
    return ch.decode("utf-8", errors="replace")


def _select_menu(title, options):
    """Generic arrow-key selection menu. Returns the selected index."""
    selected = 0

    def render():
        if render.drawn:
            sys.stdout.write(f"\033[{len(options)}A")
        for i, opt in enumerate(options):
            marker = "› " if i == selected else "  "
            highlight = "\033[1;36m" if i == selected else "\033[0m"
            sys.stdout.write(f"\r\033[2K{highlight}{marker}{opt}\033[0m\r\n")
        sys.stdout.flush()
        render.drawn = True
    render.drawn = False

    print(f"\n\033[1m{title}\033[0m\n")

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        render()
        while True:
            key = _raw_key(fd)
            if key == "enter":
                break
            elif key in ("up", "k"):
                selected = (selected - 1) % len(options)
            elif key in ("down", "j"):
                selected = (selected + 1) % len(options)
            elif key.isdigit():
                idx = int(key)
                if idx < len(options):
                    selected = idx
            render()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    return selected


def record_new_voice():
    """Record a new voice sample from the microphone via PulseAudio."""
    import tempfile

    print("\n\033[1m🎙  Recording a new voice sample\033[0m")
    print("Speak naturally for 5–15 seconds. Read a paragraph, count numbers, etc.")
    print("\033[1mPress SPACE to stop recording.\033[0m\n")

    # Start recording to a temp file
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    proc = subprocess.Popen(
        ["parecord", "--format=s16le", "--rate=24000", "--channels=1", tmp.name],
    )

    start = time.time()

    # Wait for spacebar in raw mode
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        while True:
            # Show elapsed time
            elapsed = time.time() - start
            sys.stdout.write(f"\r\033[2K  ● Recording... {elapsed:.1f}s")
            sys.stdout.flush()
            # Non-blocking read with a short timeout via select
            import select
            ready, _, _ = select.select([fd], [], [], 0.1)
            if ready:
                key = _raw_key(fd)
                if key == "space":
                    break
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    # Stop recording
    proc.send_signal(signal.SIGINT)
    proc.wait()
    elapsed = time.time() - start
    print(f"\r\033[2K  ✓ Recorded {elapsed:.1f}s\n")

    # Ask for a name
    while True:
        name = input("\033[1mVoice name:\033[0m ").strip()
        if name:
            break
        print("  Name cannot be empty.")

    # Save to voices dir
    os.makedirs(VOICES_DIR, exist_ok=True)
    dest = os.path.join(VOICES_DIR, name + ".wav")
    os.rename(tmp.name, dest)
    print(f"  Saved to {dest}\n")
    return dest


def pick_voice():
    """Arrow-key selection menu for choosing a voice at startup."""

    def _build_options():
        voices = []
        if os.path.isdir(VOICES_DIR):
            voices = sorted(
                f for f in os.listdir(VOICES_DIR) if f.endswith(".wav")
            )
        options = (
            ["(default — no voice sample, fastest)"]
            + [os.path.splitext(v)[0] for v in voices]
            + ["(new — record a voice sample)"]
        )
        return voices, options

    voices, options = _build_options()
    selected = 0

    def render():
        if render.drawn:
            # Clear the previous menu AND the hint line below it
            sys.stdout.write(f"\033[{len(render.prev_options) + 1}A")
        for i, opt in enumerate(options):
            marker = "› " if i == selected else "  "
            highlight = "\033[1;36m" if i == selected else "\033[0m"
            sys.stdout.write(f"\r\033[2K{highlight}{marker}{opt}\033[0m\r\n")
        # Hint line
        is_voice = 0 < selected < len(options) - 1
        hint = "  \033[2m(d) delete\033[0m" if is_voice else ""
        sys.stdout.write(f"\r\033[2K{hint}\r\n")
        sys.stdout.flush()
        render.drawn = True
        render.prev_options = list(options)
    render.drawn = False
    render.prev_options = []

    print(f"\n\033[1mSelect a voice (↑/↓ then Enter):\033[0m\n")

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        render()
        while True:
            key = _raw_key(fd)
            if key == "enter":
                break
            elif key in ("up", "k"):
                selected = (selected - 1) % len(options)
            elif key in ("down", "j"):
                selected = (selected + 1) % len(options)
            elif key.isdigit():
                idx = int(key)
                if idx < len(options):
                    selected = idx
            elif key in ("d", "D"):
                # Only allow deleting actual voice entries (not default/new)
                is_voice = 0 < selected < len(options) - 1
                if is_voice:
                    voice_name = options[selected]
                    # Clear menu, show confirmation
                    total_lines = len(render.prev_options) + 1  # menu + hint
                    sys.stdout.write(f"\033[{total_lines}A")
                    for _ in range(total_lines):
                        sys.stdout.write(f"\r\033[2K\r\n")
                    sys.stdout.write(f"\033[{total_lines}A")
                    sys.stdout.write(
                        f"\r\033[2K  \033[1;31mDelete '{voice_name}'? "
                        f"Press d to confirm, any other key to cancel.\033[0m\r\n"
                    )
                    sys.stdout.flush()
                    confirm = _raw_key(fd)
                    if confirm in ("d", "D"):
                        voice_file = os.path.join(VOICES_DIR, voices[selected - 1])
                        os.remove(voice_file)
                        voices, options = _build_options()
                        if selected >= len(options):
                            selected = len(options) - 1
                    # Clear the confirmation line and redraw fresh
                    sys.stdout.write(f"\033[1A\r\033[2K")
                    sys.stdout.flush()
                    render.drawn = False
            render()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    if selected == 0:
        print(f"\n\033[1mVoice:\033[0m (default — no sample)\n")
        return None
    elif selected == len(options) - 1:
        return record_new_voice()
    else:
        voice_file = os.path.join(VOICES_DIR, voices[selected - 1])
        print(f"\n\033[1mVoice:\033[0m {voices[selected - 1]}\n")
        return voice_file


# def pick_model():
#     """Choose between Turbo and Full model at startup."""
#     options = [
#         "Turbo (fast, no emotion controls)",
#         "Full (slower, supports exaggeration & cfg_weight)",
#     ]
#     selected = _select_menu("Select model:", options)
#     name = "Turbo" if selected == 0 else "Full"
#     print(f"\n\033[1mModel:\033[0m {name}\n")
#     return selected == 0


_voice = os.environ.get("TTS_VOICE", None)
# _model_env = os.environ.get("TTS_MODEL", None)
# if _model_env:
#     USE_TURBO = _model_env.lower() != "full"
# elif sys.stdin.isatty():
#     USE_TURBO = pick_model()

if _voice:
    if not _voice.endswith(".wav"):
        _voice = os.path.join(VOICES_DIR, _voice + ".wav")
    DEFAULT_REF = _voice
elif sys.stdin.isatty():
    DEFAULT_REF = pick_voice()
else:
    DEFAULT_REF = None

app = Flask(__name__)
model = None


def get_model():
    global model
    if model is None:
        from chatterbox.tts_turbo import ChatterboxTurboTTS
        print(f"Loading Chatterbox-Turbo on {DEVICE}...")
        model = ChatterboxTurboTTS.from_pretrained(DEVICE)
        # if not USE_TURBO:
        #     from chatterbox.tts import ChatterboxTTS
        #     print(f"Loading Chatterbox-Full on {DEVICE}...")
        #     model = ChatterboxTTS.from_pretrained(DEVICE)
        if DEFAULT_REF:
            print(f"Loading default voice: {DEFAULT_REF}")
            model.prepare_conditionals(DEFAULT_REF)
    return model


def wav_bytes(audio_np, sample_rate):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes((audio_np * 32767).astype(np.int16).tobytes())
    return buf.getvalue()


def play_audio(data, sample_rate):
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        tmp_path = f.name
        f.write(wav_bytes(data, sample_rate))
    subprocess.run(["paplay", tmp_path])
    os.unlink(tmp_path)


@app.route("/tts", methods=["GET", "POST"])
def tts():
    if request.method == "POST":
        body = request.get_json(force=True)
        text = body.get("text", "")
        ref = body.get("ref", DEFAULT_REF)
        play = body.get("play", True)
        exaggeration = body.get("exaggeration", None)
        cfg_weight = body.get("cfg_weight", None)
    else:
        text = request.args.get("text", "")
        ref = request.args.get("ref", DEFAULT_REF)
        play = request.args.get("play", "true").lower() != "false"
        exaggeration = request.args.get("exaggeration", None, type=float)
        cfg_weight = request.args.get("cfg_weight", None, type=float)

    if not text:
        return jsonify({"error": "no text provided"}), 400

    m = get_model()

    # # Full model extra kwargs (commented out — using Turbo only)
    # extra = {}
    # if not USE_TURBO:
    #     if exaggeration is not None:
    #         extra["exaggeration"] = float(exaggeration)
    #     if cfg_weight is not None:
    #         extra["cfg_weight"] = float(cfg_weight)

    with torch.no_grad():
        if ref and ref != DEFAULT_REF:
            wav_tensor = m.generate(text, audio_prompt_path=ref)
        elif ref or DEFAULT_REF:
            # use cached conditionals from default voice
            wav_tensor = m.generate(text)
        else:
            wav_tensor = m.generate(text, audio_prompt_path=ref)
    audio_np = wav_tensor.squeeze(0).numpy()
    del wav_tensor
    if torch.cuda.is_available():
        torch.cuda.empty_cache()

    data = wav_bytes(audio_np, m.sr)

    if play:
        threading.Thread(target=play_audio, args=(audio_np, m.sr), daemon=True).start()

    return Response(data, mimetype="audio/wav")


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "model_loaded": model is not None,
        "model_type": "turbo" if USE_TURBO else "full",
    })


if __name__ == "__main__":
    get_model()  # preload
    app.run(host="0.0.0.0", port=8070, threaded=True)
