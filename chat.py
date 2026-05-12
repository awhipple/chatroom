"""
Interactive multi-speaker chat client for local LLM.
Supports multiple speakers (user-controlled or LLM-driven) with round-robin turns.
"""

import curses
import json
import os
import select
import sys
import termios
import threading
import time
import tty
import requests

LLM_URL = "http://localhost:8080/v1/messages"
TRANSCRIPTS_DIR = os.path.expanduser("~/notes/transcripts")
CONFIGS_DIR = os.path.expanduser("~/notes/chat_configs")

# ─── TTS ─────────────────────────────────────────────────────────────────────

TTS_URL = "http://localhost:8070/tts"
BUFFER_SENTENCES = 3


def clean_for_tts(text):
    """Strip markdown and non-speech content."""
    import re
    text = re.sub(r'```[\s\S]*?```', '', text)
    text = re.sub(r'`[^`]*`', '', text)
    text = re.sub(r'\*+([^*]*)\*+', r'\1', text)
    text = re.sub(r'#{1,6}\s*', '', text)
    text = re.sub(r'^\s*[-*•]\s*', '', text, flags=re.MULTILINE)
    text = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', text)
    text = text.replace('\u2018', "'").replace('\u2019', "'")
    text = re.sub(r'\u2026|\.{3}', ',', text)
    text = re.sub(r'[\u2014\u2013]', ', ', text)
    text = re.sub(r'[^\w\s,.!?\'-]', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def split_sentences(text):
    """Split text into sentences on .!? boundaries."""
    import re
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


def tts_generate_worker(text_q, audio_q, ready_event):
    """Generates audio from text queue, signals when buffer is ready."""
    count = 0
    while True:
        text = text_q.get()
        if text is None:
            audio_q.put(None)
            ready_event.set()
            break
        text = clean_for_tts(text)
        if not text:
            continue
        try:
            resp = requests.post(TTS_URL, json={"text": text, "play": False}, timeout=120)
            if resp.ok:
                audio_q.put(resp.content)
                count += 1
                if count >= BUFFER_SENTENCES:
                    ready_event.set()
        except Exception:
            pass


def tts_play_worker(audio_q, ready_event):
    """Waits for buffer, then plays wav data sequentially."""
    import subprocess, tempfile
    ready_event.wait()
    while True:
        data = audio_q.get()
        if data is None:
            break
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                f.write(data)
                tmp_path = f.name
            subprocess.run(["paplay", tmp_path], stderr=subprocess.DEVNULL)
            os.unlink(tmp_path)
            time.sleep(0.3)
        except Exception:
            pass


def speak_text(text):
    """Send text to TTS. Silently does nothing if TTS is unavailable."""
    if not text or not text.strip():
        return
    sentences = split_sentences(text)
    if not sentences:
        return

    import queue
    text_q = queue.Queue()
    audio_q = queue.Queue()
    ready_event = threading.Event()

    gen_thread = threading.Thread(
        target=tts_generate_worker, args=(text_q, audio_q, ready_event), daemon=True
    )
    play_thread = threading.Thread(
        target=tts_play_worker, args=(audio_q, ready_event), daemon=True
    )
    gen_thread.start()
    play_thread.start()

    for s in sentences:
        text_q.put(s)
    text_q.put(None)

    try:
        play_thread.join()
    except KeyboardInterrupt:
        pass


# --- Resume (commented out — needs rework for multi-speaker configs) ---
# def load_transcript(filepath):
#     ...
#
# def pick_transcript():
#     ...


# --- Undo (commented out — needs rework from the ground up) ---
# def undo_last():
#     ...


# ─── Speaker / Config Data ───────────────────────────────────────────────────

# Available speaker colors: (name, ANSI code for terminal, curses color constant)
SPEAKER_COLORS = [
    ("Red", "\033[1;31m", curses.COLOR_RED),
    ("Green", "\033[1;32m", curses.COLOR_GREEN),
    ("Yellow", "\033[1;33m", curses.COLOR_YELLOW),
    ("Blue", "\033[1;34m", curses.COLOR_BLUE),
    ("Magenta", "\033[1;35m", curses.COLOR_MAGENTA),
    ("Cyan", "\033[1;36m", curses.COLOR_CYAN),
    ("White", "\033[1;37m", curses.COLOR_WHITE),
    ("Bright Red", "\033[1;91m", 9),
    ("Bright Green", "\033[1;92m", 10),
    ("Bright Blue", "\033[1;94m", 12),
]

import random

def random_color():
    return random.choice(SPEAKER_COLORS)[0]

def color_ansi(color_name):
    for name, ansi, _ in SPEAKER_COLORS:
        if name == color_name:
            return ansi
    return "\033[1;37m"

def default_speakers():
    return [
        {"name": "You", "controller": "user", "system_prompt": "", "color": "Yellow", "tts": False},
        {"name": "AI", "controller": "llm", "system_prompt": "", "color": "Cyan", "tts": True},
    ]


def save_config(name, speakers, order, initial_prompt=""):
    os.makedirs(CONFIGS_DIR, exist_ok=True)
    path = os.path.join(CONFIGS_DIR, f"{name}.json")
    with open(path, "w") as f:
        json.dump({"speakers": speakers, "order": order, "initial_prompt": initial_prompt}, f, indent=2)
    return path


def load_config(name):
    path = os.path.join(CONFIGS_DIR, f"{name}.json")
    with open(path) as f:
        data = json.load(f)
    order = data.get("order", "round_robin")
    if order not in ORDER_LABELS:
        order = "round_robin"
    return data["speakers"], order, data.get("initial_prompt", "")


def list_configs():
    if not os.path.isdir(CONFIGS_DIR):
        return []
    return sorted(
        os.path.splitext(f)[0]
        for f in os.listdir(CONFIGS_DIR)
        if f.endswith(".json")
    )


def delete_config(name):
    path = os.path.join(CONFIGS_DIR, f"{name}.json")
    if os.path.exists(path):
        os.unlink(path)


# ─── Curses Menu Helpers ─────────────────────────────────────────────────────

def _curses_color_for_name(color_name):
    """Get curses color constant for a speaker color name."""
    for name, _, cc in SPEAKER_COLORS:
        if name == color_name:
            return cc
    return curses.COLOR_WHITE


def curses_menu(stdscr, title, items, hotkeys=None, allow_esc=True, start_idx=0,
                item_colors=None, title_color=None):
    """Generic curses menu. Returns (selected_index, key_pressed).
    hotkeys is a dict of {key_char: action_name} for extra actions.
    item_colors: optional list of curses color constants per item (len must match items).
    title_color: optional curses color constant for the title.
    """
    curses.curs_set(0)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN, -1)
    curses.init_pair(2, curses.COLOR_YELLOW, -1)
    curses.init_pair(3, curses.COLOR_WHITE, curses.COLOR_BLUE)
    # Set up per-item color pairs (20+)
    if item_colors:
        for i, cc in enumerate(item_colors):
            if cc is not None:
                curses.init_pair(20 + i, cc, -1)
    if title_color is not None:
        curses.init_pair(4, title_color, -1)
    if hotkeys is None:
        hotkeys = {}
    # Build lookup from hotkeys, plus unshifted aliases
    hotkey_lookup = {}
    for k in hotkeys:
        hotkey_lookup[k] = k
    # Allow unshifted equivalents
    if "+" in hotkeys:
        hotkey_lookup["="] = "+"
    if "-" in hotkeys:
        hotkey_lookup["_"] = "-"
    idx = min(start_idx, len(items) - 1) if items else 0

    while True:
        stdscr.clear()
        h, w = stdscr.getmaxyx()
        # Title
        title_pair = 4 if title_color is not None else 1
        stdscr.attron(curses.color_pair(title_pair) | curses.A_BOLD)
        stdscr.addnstr(1, 2, title, w - 4)
        stdscr.attroff(curses.color_pair(title_pair) | curses.A_BOLD)

        # Items
        for i, item in enumerate(items):
            y = 3 + i
            if y >= h - 2:
                break
            # Rich item: list of (text, curses_color|None) segments
            is_rich = isinstance(item, list)
            if i == idx:
                flat = "".join(seg[0] for seg in item) if is_rich else item
                stdscr.attron(curses.color_pair(3) | curses.A_BOLD)
                stdscr.addnstr(y, 4, f" {flat} ", w - 6)
                stdscr.attroff(curses.color_pair(3) | curses.A_BOLD)
            elif is_rich:
                col = 5
                for si, (text, cc) in enumerate(item):
                    if col >= w - 2:
                        break
                    if cc is not None:
                        pair_id = 30 + i * 10 + si
                        curses.init_pair(pair_id, cc, -1)
                        stdscr.attron(curses.color_pair(pair_id) | curses.A_BOLD)
                        stdscr.addnstr(y, col, text, w - col - 2)
                        stdscr.attroff(curses.color_pair(pair_id) | curses.A_BOLD)
                    else:
                        stdscr.addnstr(y, col, text, w - col - 2)
                    col += len(text)
            elif item_colors and i < len(item_colors) and item_colors[i] is not None:
                stdscr.attron(curses.color_pair(20 + i) | curses.A_BOLD)
                stdscr.addnstr(y, 4, f" {item}", w - 6)
                stdscr.attroff(curses.color_pair(20 + i) | curses.A_BOLD)
            else:
                stdscr.addnstr(y, 4, f" {item}", w - 6)

        # Hotkey legend
        if hotkeys:
            legend_parts = []
            for key, action in hotkeys.items():
                legend_parts.append(f"{key} - {action}")
            if allow_esc:
                legend_parts.append("esc - Go Back")
            legend = "  ".join(legend_parts)
            legend_y = max(3 + len(items) + 1, h - 2)
            if legend_y < h:
                stdscr.attron(curses.A_DIM)
                stdscr.addnstr(legend_y, 4, legend, w - 6)
                stdscr.attroff(curses.A_DIM)

        stdscr.refresh()

        key = stdscr.getch()
        if key == curses.KEY_UP and idx > 0:
            idx -= 1
        elif key == curses.KEY_DOWN and idx < len(items) - 1:
            idx += 1
        elif key in (curses.KEY_ENTER, 10, 13):
            return idx, "enter"
        elif key == 27 and allow_esc:
            return idx, "esc"
        else:
            ch = chr(key) if 0 <= key < 256 else ""
            if ch in hotkey_lookup:
                return idx, hotkey_lookup[ch]


def curses_input(stdscr, prompt, initial=""):
    """Simple curses text input. Returns the string or None on Esc."""
    curses.curs_set(1)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN, -1)
    stdscr.clear()
    h, w = stdscr.getmaxyx()

    stdscr.attron(curses.color_pair(1) | curses.A_BOLD)
    stdscr.addnstr(1, 2, prompt, w - 4)
    stdscr.attroff(curses.color_pair(1) | curses.A_BOLD)

    stdscr.addnstr(3, 4, "(Enter to confirm, Esc to cancel)", w - 6)

    buf = list(initial)
    cursor = len(buf)

    while True:
        # Draw input line
        display = "".join(buf)
        stdscr.move(5, 4)
        stdscr.clrtoeol()
        stdscr.addnstr(5, 4, display, w - 6)
        stdscr.move(5, 4 + min(cursor, w - 6))
        stdscr.refresh()

        key = stdscr.getch()
        if key == 27:
            curses.curs_set(0)
            return None
        elif key in (curses.KEY_ENTER, 10, 13):
            curses.curs_set(0)
            return "".join(buf)
        elif key in (curses.KEY_BACKSPACE, 127, 8):
            if cursor > 0:
                buf.pop(cursor - 1)
                cursor -= 1
        elif key == curses.KEY_DC:
            if cursor < len(buf):
                buf.pop(cursor)
        elif key == curses.KEY_LEFT:
            if cursor > 0:
                cursor -= 1
        elif key == curses.KEY_RIGHT:
            if cursor < len(buf):
                cursor += 1
        elif key == curses.KEY_HOME:
            cursor = 0
        elif key == curses.KEY_END:
            cursor = len(buf)
        elif 32 <= key < 127:
            buf.insert(cursor, chr(key))
            cursor += 1


def curses_text_editor(stdscr, title, initial=""):
    """Multi-line text editor for system prompts. Returns string or None on Esc."""
    curses.curs_set(1)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN, -1)

    lines = initial.split("\n") if initial else [""]
    cy, cx = len(lines) - 1, len(lines[-1])

    while True:
        stdscr.clear()
        h, w = stdscr.getmaxyx()

        stdscr.attron(curses.color_pair(1) | curses.A_BOLD)
        stdscr.addnstr(0, 2, title, w - 4)
        stdscr.attroff(curses.color_pair(1) | curses.A_BOLD)
        stdscr.addnstr(1, 2, "Esc to save and go back", w - 4)

        # Draw text
        for i, line in enumerate(lines):
            row = 3 + i
            if row >= h - 1:
                break
            stdscr.addnstr(row, 2, line, w - 4)

        # Position cursor
        screen_cy = 3 + cy
        screen_cx = 2 + cx
        if screen_cy < h and screen_cx < w:
            stdscr.move(screen_cy, screen_cx)
        stdscr.refresh()

        key = stdscr.getch()
        if key == 27:
            curses.curs_set(0)
            return "\n".join(lines)
        elif key in (curses.KEY_ENTER, 10, 13):
            # Split line at cursor
            rest = lines[cy][cx:]
            lines[cy] = lines[cy][:cx]
            lines.insert(cy + 1, rest)
            cy += 1
            cx = 0
        elif key in (curses.KEY_BACKSPACE, 127, 8):
            if cx > 0:
                lines[cy] = lines[cy][:cx-1] + lines[cy][cx:]
                cx -= 1
            elif cy > 0:
                cx = len(lines[cy - 1])
                lines[cy - 1] += lines[cy]
                lines.pop(cy)
                cy -= 1
        elif key == curses.KEY_DC:
            if cx < len(lines[cy]):
                lines[cy] = lines[cy][:cx] + lines[cy][cx+1:]
            elif cy < len(lines) - 1:
                lines[cy] += lines[cy + 1]
                lines.pop(cy + 1)
        elif key == curses.KEY_UP:
            if cy > 0:
                cy -= 1
                cx = min(cx, len(lines[cy]))
        elif key == curses.KEY_DOWN:
            if cy < len(lines) - 1:
                cy += 1
                cx = min(cx, len(lines[cy]))
        elif key == curses.KEY_LEFT:
            if cx > 0:
                cx -= 1
            elif cy > 0:
                cy -= 1
                cx = len(lines[cy])
        elif key == curses.KEY_RIGHT:
            if cx < len(lines[cy]):
                cx += 1
            elif cy < len(lines) - 1:
                cy += 1
                cx = 0
        elif key == curses.KEY_HOME:
            cx = 0
        elif key == curses.KEY_END:
            cx = len(lines[cy])
        elif 32 <= key < 127:
            lines[cy] = lines[cy][:cx] + chr(key) + lines[cy][cx:]
            cx += 1


# ─── Menu Screens ────────────────────────────────────────────────────────────

def speakers_menu(stdscr, speakers):
    """Speaker list menu. Mutates speakers in place. Returns when user presses Esc."""
    sel = 0
    while True:
        ctrl_labels = {"user": "User", "llm": "Local LLM"}
        items = [f"{s['name']} ({ctrl_labels[s['controller']]})" for s in speakers] + ["-New Speaker-"]
        colors = [_curses_color_for_name(s.get("color", "White")) for s in speakers] + [None]
        hotkeys = {
            "Enter": "Select Speaker",
            "d": "Delete",
            "r": "Rename",
            "+": "Move Down",
            "-": "Move Up",
        }
        idx, action = curses_menu(stdscr, "Speakers", items, hotkeys, start_idx=sel, item_colors=colors)

        if action == "esc":
            return

        sel = idx

        if action == "enter":
            if idx == len(speakers):
                # New speaker
                name = curses_input(stdscr, "New speaker name:")
                if name and name.strip():
                    new_speaker = {
                        "name": name.strip(),
                        "controller": "llm",
                        "system_prompt": "",
                        "color": random_color(),
                        "tts": False,
                    }
                    speakers.append(new_speaker)
                    sel = len(speakers) - 1
                    edit_speaker(stdscr, new_speaker)
            else:
                edit_speaker(stdscr, speakers[idx])

        elif action == "d":
            if idx < len(speakers) and len(speakers) > 1:
                speakers.pop(idx)
                sel = min(idx, len(speakers) - 1)

        elif action == "r":
            if idx < len(speakers):
                new_name = curses_input(stdscr, f"Rename '{speakers[idx]['name']}' to:", speakers[idx]["name"])
                if new_name and new_name.strip():
                    speakers[idx]["name"] = new_name.strip()

        elif action == "+":
            if idx < len(speakers) - 1:
                speakers[idx], speakers[idx + 1] = speakers[idx + 1], speakers[idx]
                sel = idx + 1

        elif action == "-":
            if idx > 0 and idx < len(speakers):
                speakers[idx], speakers[idx - 1] = speakers[idx - 1], speakers[idx]
                sel = idx - 1


def color_picker(stdscr, speaker_name, start_idx=0):
    """Show color options with colored preview. Returns (index, action)."""
    curses.curs_set(0)
    curses.start_color()
    curses.use_default_colors()
    # Set up color pairs for preview (pairs 10-19)
    for i, (_, _, curses_color) in enumerate(SPEAKER_COLORS):
        curses.init_pair(10 + i, curses_color, -1)

    idx = start_idx
    while True:
        stdscr.clear()
        h, w = stdscr.getmaxyx()

        curses.init_pair(1, curses.COLOR_CYAN, -1)
        curses.init_pair(3, curses.COLOR_WHITE, curses.COLOR_BLUE)
        stdscr.attron(curses.color_pair(1) | curses.A_BOLD)
        stdscr.addnstr(1, 2, f"Color for {speaker_name}", w - 4)
        stdscr.attroff(curses.color_pair(1) | curses.A_BOLD)

        for i, (name, _, _) in enumerate(SPEAKER_COLORS):
            y = 3 + i
            if y >= h - 2:
                break
            if i == idx:
                stdscr.attron(curses.color_pair(3) | curses.A_BOLD)
                stdscr.addnstr(y, 4, f" {name} ", w - 6)
                stdscr.attroff(curses.color_pair(3) | curses.A_BOLD)
                # Preview next to selection
                preview = f"  {speaker_name}: Hello, world!"
                stdscr.attron(curses.color_pair(10 + i) | curses.A_BOLD)
                stdscr.addnstr(y, 4 + len(name) + 3, preview, w - len(name) - 10)
                stdscr.attroff(curses.color_pair(10 + i) | curses.A_BOLD)
            else:
                stdscr.attron(curses.color_pair(10 + i) | curses.A_BOLD)
                stdscr.addnstr(y, 4, f" {name}", w - 6)
                stdscr.attroff(curses.color_pair(10 + i) | curses.A_BOLD)

        legend_y = max(3 + len(SPEAKER_COLORS) + 1, h - 2)
        if legend_y < h:
            stdscr.attron(curses.A_DIM)
            stdscr.addnstr(legend_y, 4, "Enter - Select  esc - Go Back", w - 6)
            stdscr.attroff(curses.A_DIM)

        stdscr.refresh()

        key = stdscr.getch()
        if key == curses.KEY_UP and idx > 0:
            idx -= 1
        elif key == curses.KEY_DOWN and idx < len(SPEAKER_COLORS) - 1:
            idx += 1
        elif key in (curses.KEY_ENTER, 10, 13):
            return idx, "enter"
        elif key == 27:
            return idx, "esc"


def edit_speaker(stdscr, speaker):
    """Edit an individual speaker's settings."""
    while True:
        ctrl_label = "User" if speaker["controller"] == "user" else "LocalLLM"
        prompt_preview = speaker["system_prompt"][:40].replace("\n", " ") if speaker["system_prompt"] else "(empty)"
        color_name = speaker.get("color", "White")
        speaker_cc = _curses_color_for_name(color_name)
        tts_label = "On" if speaker.get("tts", False) else "Off"
        items = [
            f"Controller ({ctrl_label})",
            f"Color: {color_name}",
            f"TTS: {tts_label}",
            f"System Prompt: {prompt_preview}",
        ]
        item_colors = [None, speaker_cc, None, None]
        idx, action = curses_menu(stdscr, f"Editing speaker: {speaker['name']}", items,
                                  title_color=speaker_cc, item_colors=item_colors)

        if action == "esc":
            return

        if action == "enter":
            if idx == 0:
                # Toggle controller
                if speaker["controller"] == "user":
                    speaker["controller"] = "llm"
                else:
                    speaker["controller"] = "user"
            elif idx == 1:
                # Pick color
                color_names = [c[0] for c in SPEAKER_COLORS]
                # Start on current color
                cur = 0
                for i, cn in enumerate(color_names):
                    if cn == color_name:
                        cur = i
                        break
                ci, ca = color_picker(stdscr, speaker["name"], cur)
                if ca == "enter":
                    speaker["color"] = color_names[ci]
            elif idx == 2:
                # Toggle TTS
                speaker["tts"] = not speaker.get("tts", False)
            elif idx == 3:
                # Edit system prompt
                result = curses_text_editor(
                    stdscr,
                    f"System Prompt for {speaker['name']}",
                    speaker["system_prompt"],
                )
                if result is not None:
                    speaker["system_prompt"] = result


def save_config_menu(stdscr, speakers, order, initial_prompt=""):
    """Prompt for a config name and save."""
    name = curses_input(stdscr, "Configuration name:")
    if name and name.strip():
        save_config(name.strip(), speakers, order, initial_prompt)
        return name.strip()
    return None


def load_config_menu(stdscr):
    """Show saved configs, let user pick or delete. Returns (speakers, order, initial_prompt, name) or None."""
    while True:
        configs = list_configs()
        if not configs:
            curses_menu(stdscr, "Load Configuration", ["(no saved configurations)"])
            return None

        hotkeys = {"Enter": "Load", "d": "Delete"}
        idx, action = curses_menu(stdscr, "Load Configuration", configs, hotkeys)

        if action == "esc":
            return None
        elif action == "enter":
            speakers, order, initial_prompt = load_config(configs[idx])
            return speakers, order, initial_prompt, configs[idx]
        elif action == "d":
            delete_config(configs[idx])


# ─── Main Menu ───────────────────────────────────────────────────────────────

ORDER_MODES = ["round_robin", "orchestrator_llm", "orchestrator_user"]
ORDER_LABELS = {
    "round_robin": "Round Robin",
    "orchestrator_llm": "Orchestrator (LLM)",
    "orchestrator_user": "Orchestrator (User)",
}

def main_menu(stdscr):
    """Top-level menu. Returns (speakers, order) when 'Start Chat' is selected, or None to quit."""
    speakers = default_speakers()
    order = "round_robin"
    initial_prompt = ""
    config_name = None
    main_sel = 0

    while True:
        if len(speakers) <= 5:
            # Rich item with colored names
            speaker_item = [("Speakers (", None)]
            for si, s in enumerate(speakers):
                if si > 0:
                    speaker_item.append((", ", None))
                speaker_item.append((s["name"], _curses_color_for_name(s.get("color", "White"))))
            speaker_item.append((")", None))
        else:
            n_user = sum(1 for s in speakers if s["controller"] == "user")
            n_llm = sum(1 for s in speakers if s["controller"] == "llm")
            parts = []
            if n_user:
                parts.append(f"{n_user} {'User' if n_user == 1 else 'Users'}")
            if n_llm:
                parts.append(f"{n_llm} {'LLM' if n_llm == 1 else 'LLMs'}")
            speaker_item = f"Speakers ({', '.join(parts) if parts else 'none'})"
        prompt_preview = initial_prompt[:40].replace("\n", " ") if initial_prompt else "(empty)"
        items = [
            "Start Chat",
            speaker_item,
            f"Order: {ORDER_LABELS[order]}",
            f"Initial Prompt: {prompt_preview}",
            f"Save Configuration ({config_name})" if config_name else "Save Configuration",
            "Load Configuration",
        ]
        idx, action = curses_menu(stdscr, "Chat Client", items, allow_esc=True, start_idx=main_sel)

        if action == "esc":
            return None

        main_sel = idx

        if action == "enter":
            if idx == 0:
                return speakers, order, initial_prompt
            elif idx == 1:
                speakers_menu(stdscr, speakers)
            elif idx == 2:
                # Cycle order mode
                ci = ORDER_MODES.index(order)
                order = ORDER_MODES[(ci + 1) % len(ORDER_MODES)]
            elif idx == 3:
                # Edit initial prompt
                result = curses_text_editor(stdscr, "Initial Prompt", initial_prompt)
                if result is not None:
                    initial_prompt = result
            elif idx == 4:
                if config_name:
                    save_config(config_name, speakers, order, initial_prompt)
                else:
                    name = save_config_menu(stdscr, speakers, order, initial_prompt)
                    if name:
                        config_name = name
            elif idx == 5:
                result = load_config_menu(stdscr)
                if result:
                    speakers, order, initial_prompt = result[0], result[1], result[2]
                    config_name = result[3]


# ─── Input Watcher (Escape + Pause) ──────────────────────────────────────────

# Module-level pause flag — persists between watcher start/stop cycles
_pause_flag = threading.Event()


def _show_pause_bar(visible=True):
    """Show or hide the pause indicator at the bottom of the terminal."""
    if visible:
        sys.stdout.write(f"\033[s\033[999;1H\033[2K\033[1;33m  ⏸  PAUSED — press Space to resume, Esc to quit\033[0m\033[u")
    else:
        sys.stdout.write(f"\033[s\033[999;1H\033[2K\033[u")
    sys.stdout.flush()


def _wait_for_unpause():
    """Block until Space is pressed. Shows pause bar, reads stdin directly.
    Returns True if Escape was pressed (quit), False if unpaused normally."""
    _show_pause_bar(True)
    old_settings = None
    escaped = False
    try:
        old_settings = termios.tcgetattr(sys.stdin)
        tty.setcbreak(sys.stdin.fileno())
    except termios.error:
        pass

    try:
        while _pause_flag.is_set():
            if select.select([sys.stdin], [], [], 0.1)[0]:
                ch = sys.stdin.read(1)
                if ch == ' ':
                    _pause_flag.clear()
                elif ch == '\x1b':
                    _pause_flag.clear()
                    escaped = True
    finally:
        _show_pause_bar(False)
        if old_settings:
            try:
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
            except termios.error:
                pass
    return escaped


class InputWatcher:
    """Watches stdin for Escape (quit) and Space (pause) during LLM streaming/TTS."""

    def __init__(self):
        self.escaped = threading.Event()
        self._thread = None
        self._stop = threading.Event()
        self._old_settings = None

    def start(self):
        """Start watching. Puts terminal in cbreak mode."""
        self.escaped.clear()
        self._stop.clear()
        try:
            self._old_settings = termios.tcgetattr(sys.stdin)
            tty.setcbreak(sys.stdin.fileno())
        except termios.error:
            self._old_settings = None
        self._thread = threading.Thread(target=self._watch, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop watching and restore terminal."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=0.5)
            self._thread = None
        if self._old_settings:
            try:
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self._old_settings)
            except termios.error:
                pass
            self._old_settings = None

    def _watch(self):
        while not self._stop.is_set():
            if select.select([sys.stdin], [], [], 0.05)[0]:
                ch = sys.stdin.read(1)
                if ch == '\x1b':
                    self.escaped.set()
                    return
                elif ch == ' ':
                    if _pause_flag.is_set():
                        _pause_flag.clear()
                        _show_pause_bar(False)
                    else:
                        _pause_flag.set()
                        _show_pause_bar(True)


# ─── LLM Streaming ──────────────────────────────────────────────────────────

def spinner(stop_event, message="Thinking", ansi_color="\033[1;35m"):
    """Show a spinner while waiting for the LLM to respond."""
    chars = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    i = 0
    while not stop_event.is_set():
        sys.stdout.write(f"\r{ansi_color}{chars[i % len(chars)]} {message}...\033[0m")
        sys.stdout.flush()
        i += 1
        time.sleep(0.1)
    sys.stdout.write("\r" + " " * (len(message) + 10) + "\r")
    sys.stdout.flush()


def stream_llm_response(speaker, transcript, all_speakers, input_watcher=None):
    """Stream a response from the LLM for the given speaker.
    Builds message history from transcript, mapping roles relative to this speaker.
    Returns the generated text, or "ESCAPE" sentinel if Escape was pressed.
    """
    # Build messages
    messages = []

    # Identity instruction — always present so the LLM knows who it is
    # Build identity preamble — skip for default "AI" speaker with no system prompt
    identity = ""
    if speaker["name"] != "AI":
        identity = f"You are {speaker['name']}."
    if speaker["system_prompt"]:
        identity += f" {speaker['system_prompt']}" if identity else speaker["system_prompt"]
    if len(all_speakers) > 2 or speaker["name"] != "AI":
        identity += (
            f"\n\nIMPORTANT: Respond ONLY as {speaker['name']}. "
            f"Do NOT write dialogue for other speakers. "
            f"Do NOT prefix your response with your name or anyone else's name."
        )
    if identity.strip():
        messages.append({"role": "user", "content": identity})
        messages.append({"role": "assistant", "content": "Understood."})

    for entry_speaker, text in transcript:
        if entry_speaker == speaker["name"]:
            messages.append({"role": "assistant", "content": text})
        else:
            messages.append({"role": "user", "content": f"{entry_speaker}: {text}"})

    # If no transcript yet or last message was from this speaker, add a nudge
    if not messages or messages[-1]["role"] == "assistant":
        messages.append({"role": "user", "content": f"(It's your turn to respond as {speaker['name']}. Do not prefix with your name.)"})

    stop_spinner = threading.Event()
    spinner_name = f"{speaker['name']} is thinking"
    speaker_color = color_ansi(speaker.get("color", "White"))
    spinner_thread = threading.Thread(target=spinner, args=(stop_spinner, spinner_name, speaker_color), daemon=True)
    spinner_thread.start()

    try:
        resp = requests.post(
            LLM_URL,
            headers={"x-api-key": "sk-no-key", "content-type": "application/json"},
            json={
                "model": "local",
                "messages": messages,
                "max_tokens": 4096,
                "stream": True,
            },
            stream=True,
        )
        resp.encoding = "utf-8"
    except requests.ConnectionError:
        stop_spinner.set()
        spinner_thread.join()
        print(f"\033[1;31mError: Can't connect to LLM at localhost:8080. Is a model running?\033[0m\n")
        return None

    full_text = ""
    first_token = True

    escaped = False
    for line in resp.iter_lines(decode_unicode=True):
        if input_watcher and input_watcher.escaped.is_set():
            escaped = True
            break
        if not line or not line.startswith("data: "):
            continue
        data = line[6:]
        if data.strip() == "[DONE]":
            break
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            continue

        if event.get("type") == "message_stop":
            break
        if event.get("type") == "content_block_delta":
            delta = event.get("delta", {})
            # Skip thinking tokens
            if delta.get("type") == "thinking_delta":
                continue
            text = delta.get("text", "")
            if text:
                if first_token:
                    stop_spinner.set()
                    spinner_thread.join()
                    print(f"{color_ansi(speaker.get('color', 'White'))}{speaker['name']}:\033[0m ", end="", flush=True)
                    first_token = False
                print(text, end="", flush=True)
                full_text += text

    if first_token:
        stop_spinner.set()
        spinner_thread.join()
        if not escaped:
            print(f"{color_ansi(speaker.get('color', 'White'))}{speaker['name']}:\033[0m ", end="", flush=True)

    if escaped:
        resp.close()
        print()
        return "ESCAPE"

    print()
    return full_text


# ─── Orchestrator ─────────────────────────────────────────────────────────────

def orchestrator_pick_next(speakers, transcript):
    """Ask the LLM to decide which speaker should go next based on the conversation."""
    speaker_descriptions = []
    for s in speakers:
        ctrl = "user-controlled" if s["controller"] == "user" else "AI-driven"
        desc = f"- {s['name']} ({ctrl})"
        if s["system_prompt"]:
            desc += f": {s['system_prompt'][:200]}"
        speaker_descriptions.append(desc)

    speaker_list = "\n".join(speaker_descriptions)
    speaker_names = [s["name"] for s in speakers]

    convo_lines = []
    for name, text in transcript:
        convo_lines.append(f"{name}: {text}")
    convo_text = "\n".join(convo_lines) if convo_lines else "(No conversation yet)"

    system_prompt = f"""You are a conversation orchestrator. Your job is to decide which speaker should talk next in a multi-party conversation.

Here are the speakers:
{speaker_list}

Rules:
- Read the conversation and decide who should speak next based on context and natural flow.
- If someone is addressed directly by name, they will usually respond next — but not always. Another speaker might interject.
- Consider each speaker's personality (from their description) when deciding who would naturally speak next.
- Try to keep the conversation flowing naturally. Don't let any speaker be left out for too long.
- You MUST respond with ONLY the exact name of the next speaker. Nothing else. No punctuation, no explanation.

Valid speaker names: {', '.join(speaker_names)}"""

    messages = [
        {"role": "user", "content": f"{system_prompt}\n\nConversation so far:\n{convo_text}\n\nWho speaks next?"},
    ]

    try:
        resp = requests.post(
            LLM_URL,
            headers={"x-api-key": "sk-no-key", "content-type": "application/json"},
            json={
                "model": "local",
                "messages": messages,
                "max_tokens": 32,
                "stream": False,
            },
        )
        data = resp.json()
        # Parse response — handle both Anthropic and OpenAI response formats
        if "content" in data and isinstance(data["content"], list):
            choice = data["content"][0].get("text", "").strip()
        elif "choices" in data:
            choice = data["choices"][0]["message"]["content"].strip()
        else:
            choice = ""
    except Exception:
        return None

    # Match to a valid speaker name (fuzzy: strip whitespace/punctuation, case-insensitive)
    choice_clean = choice.strip().strip(".-!?,;:'\"").strip()
    for s in speakers:
        if s["name"].lower() == choice_clean.lower():
            return s
    # Partial match fallback
    for s in speakers:
        if s["name"].lower() in choice_clean.lower():
            return s
    return None


# ─── Chat Loop ───────────────────────────────────────────────────────────────

def save_transcript(transcript):
    """Save the conversation transcript to ~/notes/transcripts/."""
    if not transcript:
        return
    os.makedirs(TRANSCRIPTS_DIR, exist_ok=True)
    timestamp = time.strftime("%Y-%m-%d_%H%M%S")
    filepath = os.path.join(TRANSCRIPTS_DIR, f"{timestamp}.md")
    with open(filepath, "w", encoding="utf-8") as f:
        for speaker_name, text in transcript:
            f.write(f"{speaker_name}: {text}\n")
    print(f"\033[2mTranscript saved to {filepath}\033[0m")


def _next_speaker_round_robin(speakers, turn):
    """Return the next speaker in round-robin order."""
    return speakers[turn % len(speakers)]


def _pick_speaker_user(speakers):
    """Let the user pick the next speaker from a numbered list."""
    print("\033[2mWho speaks next?\033[0m")
    for i, s in enumerate(speakers):
        ctrl = "User" if s["controller"] == "user" else "LLM"
        print(f"  \033[1m{i + 1}\033[0m. {color_ansi(s.get('color', 'White'))}{s['name']}\033[0m ({ctrl})")
    while True:
        try:
            choice = input("\033[2m>\033[0m ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if choice.lower() in ("q", "quit", "esc"):
            return None
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(speakers):
                return speakers[idx]
        except ValueError:
            # Try matching by name
            for s in speakers:
                if s["name"].lower() == choice.lower():
                    return s
        print("\033[2mInvalid choice. Enter a number or name.\033[0m")


def _next_speaker_orchestrator(speakers, transcript):
    """Ask the orchestrator LLM who speaks next. Falls back to round-robin on failure."""
    if not transcript:
        return speakers[0]

    pick = orchestrator_pick_next(speakers, transcript)

    if pick is None:
        # Fallback: pick someone who didn't just speak
        last_name = transcript[-1][0] if transcript else None
        for s in speakers:
            if s["name"] != last_name:
                return s
        return speakers[0]
    return pick


def run_chat(speakers, order, initial_prompt=""):
    """Main chat loop with round-robin or orchestrator speaker turns."""
    import readline
    readline.parse_and_bind("set keyseq-timeout 50")
    # Escape during user input: clear line and insert sentinel + enter
    readline.parse_and_bind(r'"\e": "\C-a\C-kESC_QUIT\C-m"')

    os.system("clear")

    transcript = []
    has_user_speaker = any(s["controller"] == "user" for s in speakers)
    watcher = InputWatcher()

    print("\nType 'q' to quit, 'clear' to reset. Esc to exit.\n")

    # Display and inject initial prompt as narrator context
    if initial_prompt:
        print(f"\033[2;3m{initial_prompt}\033[0m\n")
        transcript.append(("Narrator", initial_prompt))

    turn = 0
    while True:
        # Pick next speaker based on order mode
        if order == "orchestrator_llm":
            speaker = _next_speaker_orchestrator(speakers, transcript)
        elif order == "orchestrator_user":
            speaker = _pick_speaker_user(speakers)
            if speaker is None:
                break
        else:
            speaker = _next_speaker_round_robin(speakers, turn)

        # Blank line between turns for readability
        if transcript:
            print()

        # Check if paused between turns
        if _pause_flag.is_set():
            if _wait_for_unpause():
                break

        if speaker["controller"] == "user":
            # User input
            try:
                user_input = input(f"\001{color_ansi(speaker.get('color', 'White'))}\002{speaker['name']}:\001\033[0m\002 ")
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if user_input.strip() == "ESC_QUIT":
                break
            if not user_input.strip():
                continue
            if user_input.strip().lower() in ("q", "/q", "quit", "/quit"):
                break
            if user_input.strip().lower() in ("clear", "/clear"):
                transcript.clear()
                print("History cleared.\n")
                turn = 0
                continue

            transcript.append((speaker["name"], user_input))
            turn += 1

        else:
            # LLM turn — watch for Escape and Space (pause)
            watcher.start()

            # Probe TTS availability concurrently with LLM streaming
            tts_available = threading.Event()
            if speaker.get("tts", False):
                def _probe_tts():
                    try:
                        requests.head(TTS_URL, timeout=2)
                        tts_available.set()
                    except Exception:
                        pass
                threading.Thread(target=_probe_tts, daemon=True).start()

            try:
                text = stream_llm_response(speaker, transcript, speakers, watcher)
            except KeyboardInterrupt:
                watcher.stop()
                print("\n")
                if not has_user_speaker:
                    break
                turn += 1
                continue

            if text is None:
                watcher.stop()
                break
            if text == "ESCAPE":
                watcher.stop()
                break

            transcript.append((speaker["name"], text))
            turn += 1

            # TTS only if probe confirmed it's up (watcher still active)
            if tts_available.is_set() and text.strip():
                if not watcher.escaped.is_set():
                    try:
                        speak_text(text)
                    except Exception:
                        pass

            watcher.stop()

            # If escaped during TTS
            if watcher.escaped.is_set():
                break

            # If no user speakers, small delay between LLM turns
            if not has_user_speaker:
                try:
                    time.sleep(0.5)
                except KeyboardInterrupt:
                    print()
                    break

    save_transcript(transcript)


# ─── Entry Point ─────────────────────────────────────────────────────────────

def main():
    result = curses.wrapper(main_menu)
    if result is None:
        return
    speakers, order, initial_prompt = result
    run_chat(speakers, order, initial_prompt)


if __name__ == "__main__":
    main()
