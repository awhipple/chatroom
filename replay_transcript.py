"""
Replay a chat transcript through TTS.

Parses transcripts with any speaker tags (e.g. "You:", "AI:", "Smith:", "Neo:"),
renders audio per-speaker, and plays back with controls.

Usage:
    python replay_transcript.py              # interactive menu
    python replay_transcript.py <file>       # play a specific file
    python replay_transcript.py <file> --render
    python replay_transcript.py <file> --start 3
    python replay_transcript.py <file> --list
"""

import json
import os
import re
import shutil
import subprocess
import sys
import termios
import threading
import time
import tty
import requests

TTS_URL = os.environ.get("TTS_URL", "http://localhost:8070/tts")
STORIES_DIR = os.path.expanduser("~/notes/stories")


# --- Text processing (matches chat.py) ---

def clean_for_tts(text):
    """Strip markdown and non-speech content."""
    text = re.sub(r'```[\s\S]*?```', '', text)
    text = re.sub(r'`[^`]*`', '', text)
    text = re.sub(r'\*+([^*]*)\*+', r'\1', text)
    text = re.sub(r'#{1,6}\s*', '', text)
    text = re.sub(r'^\s*[-*\u2022]\s*', '', text, flags=re.MULTILINE)
    text = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', text)
    text = text.replace('\u2018', "'").replace('\u2019', "'")
    text = re.sub(r'\u2026|\.{3}', ',', text)
    text = re.sub(r'[\u2014\u2013]', ', ', text)
    text = re.sub(r'[^\w\s,.!?\'-]', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def split_sentences(text):
    """Split text into sentences on .!? boundaries."""
    sentences = []
    while True:
        match = re.search(r'[.!?](?:\s+|$)', text)
        if not match:
            break
        end = match.end()
        sentence = text[:end].strip()
        text = text[end:]
        if sentence:
            sentences.append(sentence)
    if text.strip():
        sentences.append(text.strip())
    return sentences


# --- Transcript parsing ---

def load_meta(filepath):
    """Load speaker metadata sidecar (.meta.json) if it exists."""
    meta_path = os.path.splitext(filepath)[0] + ".meta.json"
    if os.path.exists(meta_path):
        with open(meta_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return None


# ANSI color lookup matching chat.py's SPEAKER_COLORS
_COLOR_ANSI = {
    "Red": "\033[1;31m",
    "Green": "\033[1;32m",
    "Yellow": "\033[1;33m",
    "Blue": "\033[1;34m",
    "Magenta": "\033[1;35m",
    "Cyan": "\033[1;36m",
    "White": "\033[1;37m",
    "Bright Red": "\033[1;91m",
    "Bright Green": "\033[1;92m",
    "Bright Blue": "\033[1;94m",
}


def speaker_ansi(speaker, meta):
    """Return the ANSI color code for a speaker, or default cyan."""
    if meta and "speakers" in meta:
        info = meta["speakers"].get(speaker)
        if info:
            return _COLOR_ANSI.get(info.get("color", ""), "\033[1;36m")
    return "\033[1;36m"


def _detect_format(filepath):
    """Detect whether a transcript uses [Name]: (new) or Name: (legacy) format.
    Returns 'bracketed' or 'legacy'."""
    with open(filepath, 'r', encoding='utf-8') as f:
        for line in f:
            if re.match(r'^\[.+\]: ', line):
                return "bracketed"
            if re.match(r'^[A-Za-z][A-Za-z0-9]*(?: [A-Za-z][A-Za-z0-9]*){0,2}: ', line):
                return "legacy"
    return "legacy"


def detect_tags(filepath):
    """Scan a file and return all unique speaker tags found.
    If a .meta.json sidecar exists, use its speaker names as authoritative."""
    meta = load_meta(filepath)
    if meta and "speakers" in meta:
        return list(meta["speakers"].keys())

    fmt = _detect_format(filepath)
    tags = []
    seen = set()
    with open(filepath, 'r', encoding='utf-8') as f:
        for line in f:
            if fmt == "bracketed":
                m = re.match(r'^\[([^\]]+)\]: ', line)
            else:
                m = re.match(r'^([A-Za-z][A-Za-z0-9]*(?: [A-Za-z][A-Za-z0-9]*){0,2}): ', line)
                if m and len(m.group(1)) > 30:
                    continue
            if m:
                tag = m.group(1)
                if tag not in seen:
                    seen.add(tag)
                    tags.append(tag)
    return tags


def parse_transcript(filepath):
    """Parse transcript into a list of (speaker, text, start_line) tuples.
    Auto-detects bracketed [Name]: or legacy Name: format."""
    fmt = _detect_format(filepath)
    tags = detect_tags(filepath)
    if not tags:
        return []

    if fmt == "bracketed":
        tag_pattern = re.compile(r'^\[(' + '|'.join(re.escape(t) for t in tags) + r')\]: ')
    else:
        tag_pattern = re.compile(r'^(' + '|'.join(re.escape(t) for t in tags) + r'): ')

    with open(filepath, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    turns = []
    current_speaker = None
    current_lines = []
    current_start = 0

    for lineno, line in enumerate(lines, 1):
        line = line.rstrip('\n')
        m = tag_pattern.match(line)
        if m:
            if current_speaker and current_lines:
                turns.append((current_speaker, '\n'.join(current_lines), current_start))
            current_speaker = m.group(1)
            current_lines = [line[m.end():]]
            current_start = lineno
        elif current_speaker:
            current_lines.append(line)

    if current_speaker and current_lines:
        turns.append((current_speaker, '\n'.join(current_lines), current_start))

    return turns


# --- Audio directory helpers ---

def audio_dir_for(filepath):
    """Return the .audio/<stem> directory path for a transcript file."""
    parent = os.path.dirname(os.path.abspath(filepath))
    stem = os.path.splitext(os.path.basename(filepath))[0]
    return os.path.join(parent, ".audio", stem)


def load_manifest(filepath):
    """Load the manifest for a transcript, or None if not yet created."""
    manifest_path = os.path.join(audio_dir_for(filepath), "manifest.json")
    if not os.path.exists(manifest_path):
        return None
    with open(manifest_path, 'r') as f:
        return json.load(f)


def save_manifest(filepath, manifest):
    """Save manifest to disk."""
    adir = audio_dir_for(filepath)
    os.makedirs(adir, exist_ok=True)
    manifest_path = os.path.join(adir, "manifest.json")
    with open(manifest_path, 'w') as f:
        json.dump(manifest, f, indent=2)


def build_manifest(filepath):
    """Build a fresh manifest from the transcript (no audio files)."""
    turns = parse_transcript(filepath)
    entries = []
    for turn_idx, (speaker, text, lineno) in enumerate(turns):
        sentences = split_sentences(text)
        for sentence in sentences:
            cleaned = clean_for_tts(sentence)
            if cleaned:
                entries.append({
                    "file": None,
                    "text": sentence,
                    "turn": turn_idx + 1,
                    "line": lineno,
                    "speaker": speaker,
                })
    return {
        "source": os.path.basename(filepath),
        "rendered_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "sentences": entries,
    }


def ensure_manifest(filepath):
    """Load existing manifest, or create one if none exists."""
    manifest = load_manifest(filepath)
    if manifest is None:
        manifest = build_manifest(filepath)
        save_manifest(filepath, manifest)
    return manifest


def sync_manifest(filepath):
    """Rebuild manifest from transcript, preserving audio for unchanged sentences.
    Call this explicitly when the transcript may have been edited externally."""
    old_manifest = load_manifest(filepath)
    new_manifest = build_manifest(filepath)

    if old_manifest is not None:
        # Build lookup: (speaker, text) -> file, allowing multiple matches
        audio_lookup = {}
        for s in old_manifest["sentences"]:
            if s.get("file"):
                key = (s["speaker"], s["text"])
                if key not in audio_lookup:
                    audio_lookup[key] = []
                audio_lookup[key].append(s["file"])

        # Match new sentences to old audio
        for s in new_manifest["sentences"]:
            key = (s["speaker"], s["text"])
            if key in audio_lookup and audio_lookup[key]:
                s["file"] = audio_lookup[key].pop(0)

        # Clean up orphaned wav files
        adir = audio_dir_for(filepath)
        if os.path.isdir(adir):
            used_files = {s["file"] for s in new_manifest["sentences"] if s.get("file")}
            for f in os.listdir(adir):
                if f.endswith('.wav') and f not in used_files:
                    os.remove(os.path.join(adir, f))

    save_manifest(filepath, new_manifest)
    return new_manifest


def get_render_stats(manifest):
    """Return dict of {speaker: (rendered_count, total_count)}."""
    stats = {}
    for s in manifest["sentences"]:
        speaker = s["speaker"]
        if speaker not in stats:
            stats[speaker] = [0, 0]
        stats[speaker][1] += 1
        if s.get("file"):
            stats[speaker][0] += 1
    return stats


# --- TUI helpers ---

def _raw_key(fd):
    """Read a single keypress in raw mode."""
    import select
    ch = os.read(fd, 1)
    if ch == b"\r" or ch == b"\n":
        return "enter"
    if ch == b" ":
        return "space"
    if ch == b"\x1b":
        ready, _, _ = select.select([fd], [], [], 0.05)
        if not ready:
            return "esc"
        seq = os.read(fd, 2)
        if seq == b"[A":
            return "up"
        if seq == b"[B":
            return "down"
        return "esc"
    if ch == b"\x03":
        raise KeyboardInterrupt
    return ch.decode("utf-8", errors="replace")


def _select_menu(title, options):
    """Arrow-key selection menu. Returns the selected index, or None if Esc."""
    selected = 0

    def render():
        if render.drawn:
            sys.stdout.write(f"\033[{len(options)}A")
        for i, opt in enumerate(options):
            marker = "\u203a " if i == selected else "  "
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
            elif key in ("esc", "q"):
                selected = None
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


# --- Render for a specific speaker ---

def render_speaker(filepath, speaker, clean_speaker=False):
    """Render (or re-render) audio for all sentences by a specific speaker."""
    adir = audio_dir_for(filepath)
    os.makedirs(adir, exist_ok=True)
    manifest = ensure_manifest(filepath)

    # Check TTS server
    try:
        r = requests.get(TTS_URL.replace("/tts", "/health"), timeout=2)
        if not r.ok:
            print("TTS server not responding.")
            return
    except Exception:
        print("TTS server not reachable. Is it running?")
        return

    # Find sentences for this speaker
    target_entries = [(i, s) for i, s in enumerate(manifest["sentences"]) if s["speaker"] == speaker]

    if clean_speaker:
        # Delete existing wav files for this speaker
        for _, s in target_entries:
            if s.get("file"):
                wav_path = os.path.join(adir, s["file"])
                if os.path.exists(wav_path):
                    os.remove(wav_path)
                s["file"] = None
        save_manifest(filepath, manifest)

    unrendered = [(i, s) for i, s in target_entries if not s.get("file")]
    already_rendered = len(target_entries) - len(unrendered)
    total_for_speaker = len(target_entries)

    if not unrendered:
        print(f"All {total_for_speaker} sentences for '{speaker}' are already rendered.")
        return

    # Find the highest existing wav number
    existing_wavs = [f for f in os.listdir(adir) if f.endswith('.wav')]
    max_num = max((int(os.path.splitext(f)[0]) for f in existing_wavs), default=0)

    print(f"Rendering {len(unrendered)} sentences for '{speaker}'...\n")

    import select
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    stop_flag = threading.Event()
    interrupted = False

    def _render_key_listener():
        while not stop_flag.is_set():
            ready, _, _ = select.select([fd], [], [], 0.1)
            if not ready:
                continue
            try:
                key = _raw_key(fd)
            except OSError:
                break
            if key in ("esc", "q"):
                sys.stdout.write(f"\r\033[2K  \033[1;31m\u23f9  Stopping after current sentence...\033[0m\n")
                sys.stdout.flush()
                stop_flag.set()
                break

    try:
        tty.setcbreak(fd)
        listener = threading.Thread(target=_render_key_listener, daemon=True)
        listener.start()

        for idx, (manifest_idx, entry) in enumerate(unrendered):
            if stop_flag.is_set():
                interrupted = True
                break

            max_num += 1
            wav_name = f"{max_num:04d}.wav"
            wav_path = os.path.join(adir, wav_name)

            # Skip if file already exists on disk (shouldn't happen after clean, but safe)
            if os.path.exists(wav_path):
                max_num += 1
                wav_name = f"{max_num:04d}.wav"
                wav_path = os.path.join(adir, wav_name)

            cleaned = clean_for_tts(entry["text"])
            if not cleaned:
                continue

            preview = entry["text"][:80].replace('\n', ' ')
            print(f"  [{already_rendered + idx + 1}/{total_for_speaker}] {preview}")
            try:
                resp = requests.post(TTS_URL, json={"text": cleaned, "play": False}, timeout=120)
                if resp.ok:
                    with open(wav_path, 'wb') as f:
                        f.write(resp.content)
                    manifest["sentences"][manifest_idx]["file"] = wav_name
                else:
                    print(f"    \033[31mWARNING: TTS returned {resp.status_code}, skipping\033[0m")
            except Exception as e:
                print(f"    \033[31mERROR: {e}, skipping\033[0m")
    except KeyboardInterrupt:
        interrupted = True
    finally:
        stop_flag.set()
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    save_manifest(filepath, manifest)

    if not interrupted:
        print(f"\nDone! Rendered {len(unrendered)} sentences for '{speaker}'.")
    else:
        print(f"\n\033[1mProgress saved.\033[0m")


# --- Pre-rendered playback ---

class PlayerState:
    """Shared state for the input listener thread."""
    def __init__(self):
        self.paused = False
        self.quit = False
        self.current_sentence = 0
        self.total = 0
        self.lock = threading.Lock()


def _input_listener(state):
    """Background thread that listens for keypresses during playback."""
    import select
    fd = sys.stdin.fileno()
    while not state.quit:
        ready, _, _ = select.select([fd], [], [], 0.1)
        if not ready:
            continue
        try:
            key = _raw_key(fd)
        except OSError:
            break
        if key == "space":
            with state.lock:
                state.paused = not state.paused
                if state.paused:
                    i = state.current_sentence
                    sys.stdout.write(f"\r\033[2K  \033[1;33m\u23f8  Paused at [{i+1}/{state.total}] \u2014 space to resume, q/esc to quit\033[0m")
                    sys.stdout.flush()
                else:
                    sys.stdout.write(f"\r\033[2K")
                    sys.stdout.flush()
        elif key in ("q", "esc"):
            state.quit = True
            i = state.current_sentence
            sys.stdout.write(f"\r\033[2K  \033[1;31m\u23f9  Stopping after sentence {i+1}...\033[0m")
            sys.stdout.flush()
            break


def _wait_while_paused(state):
    """Block until unpaused or quit."""
    while True:
        with state.lock:
            if not state.paused or state.quit:
                return
        time.sleep(0.05)


def play_prerendered(filepath, manifest, start_sentence=0, meta=None):
    """Play from pre-rendered audio files with spacebar pause."""
    if meta is None:
        meta = load_meta(filepath)
    adir = audio_dir_for(filepath)
    entries = manifest["sentences"]
    total = len(entries)

    if start_sentence >= total:
        print(f"Only {total} sentences. Cannot start from sentence {start_sentence + 1}.")
        return

    print(f"Playing from sentence {start_sentence + 1}/{total}")
    print(f"\033[2mspace=pause  q/esc=quit\033[0m\n")

    state = PlayerState()
    state.total = total

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        listener = threading.Thread(target=_input_listener, args=(state,), daemon=True)
        listener.start()

        last_speaker = None
        for i in range(start_sentence, total):
            state.current_sentence = i

            if state.quit:
                break

            if state.paused:
                _wait_while_paused(state)
                if state.quit:
                    break

            entry = entries[i]
            speaker = entry["speaker"]
            preview = entry["text"].replace('\n', ' ')

            # Blank line between different speakers
            if last_speaker is not None and speaker != last_speaker:
                print()
            last_speaker = speaker

            if entry.get("file"):
                # Has audio — play it
                wav_path = os.path.join(adir, entry["file"])
                if os.path.exists(wav_path):
                    sc = speaker_ansi(speaker, meta)
                    print(f"  \033[2m[{i+1}/{total}]\033[0m {sc}{speaker}:\033[0m \033[0;37m{preview}\033[0m")
                    subprocess.run(["paplay", wav_path])

                    if state.paused:
                        _wait_while_paused(state)
                        if state.quit:
                            break

                    time.sleep(0.3)
            else:
                # No audio — display in yellow and auto-pause
                sc = speaker_ansi(speaker, meta)
                print(f"  \033[2m[{i+1}/{total}]\033[0m {sc}{speaker}:\033[0m \033[33m{preview}\033[0m")
                sys.stdout.write(f"\n  \033[1;37m\u23f8  Press space to continue...\033[0m")
                sys.stdout.flush()
                with state.lock:
                    state.paused = True
                _wait_while_paused(state)
                # Clear the prompt line and the blank line
                sys.stdout.write(f"\r\033[2K\033[1A\033[2K")
                sys.stdout.flush()
                if state.quit:
                    break

        if state.quit:
            print(f"\n\n\033[1mStopped at sentence {i+1}/{total}.\033[0m")
        else:
            print("\n\033[1mDone.\033[0m")
    except KeyboardInterrupt:
        print(f"\n\n\033[1mStopped at sentence {i+1}/{total}.\033[0m")
    finally:
        state.quit = True
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


# --- Interactive menu ---

def interactive_menu():
    """Full interactive TUI for selecting and playing stories."""
    os.system("clear")
    if not os.path.isdir(STORIES_DIR):
        print(f"No stories directory found at {STORIES_DIR}")
        sys.exit(1)

    stories = sorted(
        f for f in os.listdir(STORIES_DIR)
        if f.endswith('.md')
    )

    if not stories:
        print("No stories found in ~/notes/stories/")
        sys.exit(1)

    # Build story options with render status
    story_options = []
    for s in stories:
        stem = os.path.splitext(s)[0]
        fpath = os.path.join(STORIES_DIR, s)
        adir = audio_dir_for(fpath)
        manifest = load_manifest(fpath)
        if manifest:
            rendered = len([e for e in manifest["sentences"] if e.get("file")])
            total = len(manifest["sentences"])
            if rendered > 0 and os.path.isdir(adir):
                total_bytes = sum(
                    os.path.getsize(os.path.join(adir, f))
                    for f in os.listdir(adir) if f.endswith('.wav')
                )
                size_mb = total_bytes / (1024 * 1024)
                tag = f" \033[1;32m[{rendered}/{total} lines, {size_mb:.1f} MB]\033[0m"
            else:
                tag = f" \033[2m[0/{total} lines]\033[0m"
        else:
            # No manifest yet — count sentences from transcript
            turns = parse_transcript(fpath)
            total = sum(1 for _, text, _ in turns for s in split_sentences(text) if clean_for_tts(s))
            tag = f" \033[2m[0/{total} lines]\033[0m"
        story_options.append(f"{stem}{tag}")

    selected_story = _select_menu("Select a story:", story_options)
    if selected_story is None:
        return
    filepath = os.path.join(STORIES_DIR, stories[selected_story])
    sync_manifest(filepath)  # sync once on entry in case transcript was edited externally
    story_menu(filepath)


def story_menu(filepath):
    """Show the action menu for a specific story."""
    os.system("clear")
    manifest = ensure_manifest(filepath)
    story_name = os.path.splitext(os.path.basename(filepath))[0]

    actions = ["Play", "Speakers", "Delete"]

    action_idx = _select_menu(f"{story_name}", actions)
    if action_idx is None:
        interactive_menu()
        return
    action = actions[action_idx]

    if action == "Play":
        manifest = ensure_manifest(filepath)
        entries = manifest["sentences"]
        total = len(entries)
        print(f"\n\033[1mEnter sentence number to start from (1-{total}, Enter for beginning):\033[0m ", end="", flush=True)
        try:
            num_input = input().strip()
        except EOFError:
            num_input = ""
        if not num_input:
            target = 1
        else:
            try:
                target = int(num_input)
            except ValueError:
                print("Invalid number.")
                return
            if target < 1 or target > total:
                print(f"Must be between 1 and {total}.")
                return
        print()
        play_prerendered(filepath, manifest, start_sentence=target - 1)

    elif action == "Speakers":
        speakers_menu(filepath)
        story_menu(filepath)

    elif action == "Delete":
        story_name = os.path.splitext(os.path.basename(filepath))[0]
        print(f"\n\033[1;31mDelete '{story_name}' and all rendered audio?\033[0m")
        print(f"\033[2mPress Enter twice to confirm, or Esc to cancel.\033[0m")
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            key = _raw_key(fd)
            if key != "enter":
                print("\r\n\033[2mCancelled.\033[0m")
                return
            key = _raw_key(fd)
            if key != "enter":
                print("\r\n\033[2mCancelled.\033[0m")
                return
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        adir = audio_dir_for(filepath)
        if os.path.isdir(adir):
            shutil.rmtree(adir)
        meta_path = os.path.splitext(filepath)[0] + ".meta.json"
        if os.path.exists(meta_path):
            os.remove(meta_path)
        os.remove(filepath)
        print(f"\n\033[1;31mDeleted '{story_name}'.\033[0m")
        time.sleep(1)
        interactive_menu()


def speakers_menu(filepath):
    """Speaker list with hotkeys for render, delete audio, and rename."""
    os.system("clear")
    manifest = ensure_manifest(filepath)
    stats = get_render_stats(manifest)
    speakers = list(stats.keys())

    options = []
    for speaker in speakers:
        rendered, total = stats[speaker]
        if rendered == total:
            options.append(f"\033[1;32m{speaker} ({rendered}/{total})\033[0m")
        else:
            options.append(f"\033[1;33m{speaker} ({rendered}/{total})\033[0m")

    selected = 0

    def render():
        if render.drawn:
            # menu lines + hint line
            sys.stdout.write(f"\033[{len(options) + 2}A")
        for i, opt in enumerate(options):
            marker = "\u203a " if i == selected else "  "
            highlight = "\033[1;36m" if i == selected else "\033[0m"
            sys.stdout.write(f"\r\033[2K{highlight}{marker}{opt}\033[0m\r\n")
        # Blank line + dynamic legend
        rendered, total = stats[speakers[selected]]
        fully_rendered = rendered == total
        hints = []
        if not fully_rendered:
            hints.append("Enter \u2014 render voice")
        hints.append("d \u2014 delete voice")
        hints.append("r \u2014 rename")
        hints.append("Esc \u2014 go back")
        legend = "   ".join(hints)
        sys.stdout.write(f"\r\033[2K\r\n")
        sys.stdout.write(f"\r\033[2K  \033[2m{legend}\033[0m\r\n")
        sys.stdout.flush()
        render.drawn = True
    render.drawn = False

    print(f"\n\033[1mSpeakers\033[0m\n")

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        render()
        while True:
            key = _raw_key(fd)
            if key in ("esc", "q"):
                break
            elif key in ("up", "k"):
                selected = (selected - 1) % len(options)
            elif key in ("down", "j"):
                selected = (selected + 1) % len(options)
            elif key == "enter":
                # Render this speaker's voice (skips if fully rendered)
                speaker = speakers[selected]
                rendered_count, total_count = stats[speaker]
                if rendered_count == total_count and rendered_count > 0:
                    render()
                    continue
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                sys.stdout.write(f"\r\n")
                print()
                render_speaker(filepath, speaker)
                speakers_menu(filepath)
                return
            elif key in ("d", "D"):
                # Delete this speaker's audio
                speaker = speakers[selected]
                rendered_count, _ = stats[speaker]
                if rendered_count == 0:
                    render()
                    continue
                # Show confirmation on legend line
                sys.stdout.write(f"\033[1A")
                sys.stdout.write(f"\r\033[2K  \033[1;31mDelete audio for '{speaker}'? Press d to confirm.\033[0m\r\n")
                sys.stdout.flush()
                confirm = _raw_key(fd)
                if confirm in ("d", "D"):
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                    adir = audio_dir_for(filepath)
                    manifest = ensure_manifest(filepath)
                    for s in manifest["sentences"]:
                        if s["speaker"] == speaker and s.get("file"):
                            wav_path = os.path.join(adir, s["file"])
                            if os.path.exists(wav_path):
                                os.remove(wav_path)
                            s["file"] = None
                    save_manifest(filepath, manifest)
                    print(f"\r\n\033[1mCleared audio for '{speaker}'.\033[0m")
                    speakers_menu(filepath)
                    return
                # Cancelled — redraw
                render()
                continue
            elif key in ("r", "R"):
                # Rename this speaker
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                old_name = speakers[selected]
                print(f"\r\n\n\033[1mRename '{old_name}' to:\033[0m ", end="", flush=True)
                try:
                    new_name = input().strip()
                except (EOFError, KeyboardInterrupt):
                    speakers_menu(filepath)
                    return
                if not new_name or new_name == old_name:
                    speakers_menu(filepath)
                    return
                # Update transcript file (handle both formats)
                fmt = _detect_format(filepath)
                with open(filepath, 'r', encoding='utf-8') as f:
                    content = f.read()
                if fmt == "bracketed":
                    content = re.sub(
                        r'^\[' + re.escape(old_name) + r'\]: ',
                        '[' + new_name + ']: ',
                        content,
                        flags=re.MULTILINE
                    )
                else:
                    content = re.sub(
                        r'^' + re.escape(old_name) + r': ',
                        new_name + ': ',
                        content,
                        flags=re.MULTILINE
                    )
                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write(content)
                # Update manifest
                old_manifest = load_manifest(filepath)
                if old_manifest:
                    for s in old_manifest["sentences"]:
                        if s["speaker"] == old_name:
                            s["speaker"] = new_name
                    save_manifest(filepath, old_manifest)
                # Update metadata sidecar
                meta = load_meta(filepath)
                if meta and "speakers" in meta and old_name in meta["speakers"]:
                    meta["speakers"][new_name] = meta["speakers"].pop(old_name)
                    meta_path = os.path.splitext(filepath)[0] + ".meta.json"
                    with open(meta_path, 'w', encoding='utf-8') as f:
                        json.dump(meta, f, indent=2)
                print(f"\033[1mRenamed '{old_name}' to '{new_name}'.\033[0m")
                speakers_menu(filepath)
                return
            render()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


# --- Resolve story by name ---

def resolve_story(name):
    """Find a story file by name (with or without .md, partial match)."""
    if os.path.isfile(name):
        return name

    exact = os.path.join(STORIES_DIR, name)
    if os.path.isfile(exact):
        return exact
    exact_md = os.path.join(STORIES_DIR, name + ".md")
    if os.path.isfile(exact_md):
        return exact_md

    if os.path.isdir(STORIES_DIR):
        lower = name.lower()
        for f in os.listdir(STORIES_DIR):
            if f.endswith('.md') and lower in f.lower():
                return os.path.join(STORIES_DIR, f)

    return None


# --- CLI main ---

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Replay a chat transcript through TTS.")
    parser.add_argument("file", nargs="?", default=None, help="Path to transcript file")
    parser.add_argument("--story", "-s", type=str, default=None, help="Story name (matches files in ~/notes/stories/)")
    parser.add_argument("--resume", "-r", action="store_true", help="Prompt for sentence number to resume from")
    parser.add_argument("--start", type=int, default=1, help="Turn number to start from (default: 1)")
    parser.add_argument("--line", type=int, default=None, help="Start from the sentence at/after this line number")
    parser.add_argument("--list", action="store_true", help="List all turns with numbers, don't play")
    parser.add_argument("--render", action="store_true", help="Pre-generate all audio to .audio/ directory")
    parser.add_argument("--convert", action="store_true", help="Copy most recent transcript to stories")
    args = parser.parse_args()

    # --convert: copy most recent transcript to stories
    if args.convert:
        transcripts_dir = os.path.expanduser("~/notes/transcripts")
        if not os.path.isdir(transcripts_dir):
            print("No transcripts found in ~/notes/transcripts/")
            sys.exit(1)
        transcripts = sorted(
            (f for f in os.listdir(transcripts_dir) if f.endswith('.md')),
            reverse=True
        )
        if not transcripts:
            print("No transcripts found in ~/notes/transcripts/")
            sys.exit(1)
        latest = os.path.join(transcripts_dir, transcripts[0])
        print(f"\033[2mMost recent transcript: {transcripts[0]}\033[0m")
        name = input("\033[1mStory name:\033[0m ").strip()
        if not name:
            print("No name provided.")
            sys.exit(1)
        os.makedirs(STORIES_DIR, exist_ok=True)
        dest = os.path.join(STORIES_DIR, name + ".md")
        shutil.copy2(latest, dest)
        # Copy sidecar metadata if it exists
        latest_meta = os.path.splitext(latest)[0] + ".meta.json"
        if os.path.exists(latest_meta):
            dest_meta = os.path.join(STORIES_DIR, name + ".meta.json")
            shutil.copy2(latest_meta, dest_meta)
        print(f"Copied to {dest}\n")
        interactive_menu()
        return

    # Resolve file from --story flag
    if args.story:
        filepath = resolve_story(args.story)
        if not filepath:
            print(f"Story not found: {args.story}")
            print(f"Stories are in {STORIES_DIR}/")
            if os.path.isdir(STORIES_DIR):
                available = [os.path.splitext(f)[0] for f in sorted(os.listdir(STORIES_DIR)) if f.endswith('.md')]
                if available:
                    print(f"Available: {', '.join(available)}")
            sys.exit(1)
        args.file = filepath

    # No file argument: interactive menu
    if args.file is None:
        interactive_menu()
        return

    turns = parse_transcript(args.file)
    if not turns:
        print("No turns found in transcript.")
        sys.exit(1)

    # --list
    if args.list:
        meta = load_meta(args.file)
        manifest = load_manifest(args.file)
        for i, (speaker, text, lineno) in enumerate(turns):
            preview = text[:100].replace('\n', ' ')
            if len(text) > 100:
                preview += '...'
            sc = speaker_ansi(speaker, meta)
            print(f"  {i+1:3d}. (line {lineno:3d}) {sc}{speaker}\033[0m: {preview}")
        stats_str = ""
        if manifest:
            stats = get_render_stats(manifest)
            parts = [f"{sp}: {r}/{t}" for sp, (r, t) in stats.items()]
            stats_str = f"  Rendered: {', '.join(parts)}"
        print(f"\n  {len(turns)} turn(s) total. {stats_str}")
        return

    # --render: go to story menu
    if args.render:
        story_menu(args.file)
        return

    # --resume or default play
    manifest = ensure_manifest(args.file)

    if args.resume:
        total = len(manifest["sentences"])
        print(f"\033[1mEnter sentence number to resume from (1-{total}, Enter for beginning):\033[0m ", end="", flush=True)
        try:
            num_input = input().strip()
        except EOFError:
            num_input = ""
        if not num_input:
            target = 1
        else:
            try:
                target = int(num_input)
            except ValueError:
                print("Invalid number.")
                sys.exit(1)
            if target < 1 or target > total:
                print(f"Must be between 1 and {total}.")
                sys.exit(1)
        print()
        play_prerendered(args.file, manifest, start_sentence=target - 1)
        return

    # Resolve start position
    entries = manifest["sentences"]
    start_sentence = 0
    if args.line is not None:
        for si, entry in enumerate(entries):
            if entry["line"] >= args.line:
                start_sentence = si
                break
    elif args.start > 1:
        target_turn = args.start
        for si, entry in enumerate(entries):
            if entry["turn"] >= target_turn:
                start_sentence = si
                break

    play_prerendered(args.file, manifest, start_sentence=start_sentence)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\n")
        sys.exit(0)
