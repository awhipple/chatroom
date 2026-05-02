# Chatroom

A multi-speaker chat client for local LLMs with optional TTS, plus a story reader/player.

## Scripts

- **`chat.py`** — Interactive multi-speaker chat client. Supports user-controlled and LLM-driven speakers, round-robin or orchestrator turn order, configurable system prompts, per-speaker TTS, and save/load configurations.
- **`tts_server.py`** — Flask-based TTS server using [Chatterbox](https://github.com/resemble-ai/chatterbox). Provides a simple HTTP API for speech synthesis with optional voice cloning.
- **`replay_transcript.py`** — Story reader/player that replays saved conversation transcripts with TTS.

## Requirements

- Python 3.10+
- A local LLM server serving an Anthropic-compatible API on port 8080. [llama.cpp](https://github.com/ggerganov/llama.cpp) works well — build `llama-server` and run it with `--host 0.0.0.0 -ngl 999` and a GGUF model. The server must support the Anthropic `/v1/messages` endpoint with streaming (llama.cpp does this natively).
- `requests` (`pip install requests`)

### TTS (optional)

TTS requires a CUDA GPU and the Chatterbox model. The chat client works fine without it — TTS features are silently skipped if the server isn't running.

#### Chatterbox setup

1. Clone the Chatterbox repo:
   ```bash
   git clone https://github.com/resemble-ai/chatterbox.git ~/projects/chatterbox
   ```

2. Create a conda environment (or use an existing one with PyTorch + CUDA):
   ```bash
   conda create -n chatterbox python=3.10
   conda activate chatterbox
   ```

3. Install PyTorch with CUDA support:
   ```bash
   pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121
   ```

4. Install Chatterbox:
   ```bash
   cd ~/projects/chatterbox
   pip install -e .
   ```

5. Install Flask for the server:
   ```bash
   pip install flask
   ```

6. Run the TTS server:
   ```bash
   python tts_server.py
   ```
   The server runs on port 8070. On first request it downloads and loads the model (~1.5 GB).

#### TTS API

- `POST /tts` with JSON `{"text": "Hello world"}` — generate speech, returns WAV audio
- `POST /tts` with `{"text": "...", "play": false}` — generate without playing locally
- `POST /tts` with `{"text": "...", "ref": "/path/to/voice.wav"}` — voice cloning
- `GET /health` — server status

#### Voice samples

Voice samples for cloning are stored in `~/voices/`. The TTS server lets you pick or record a voice on startup.

## Usage

### Chat client

```bash
python chat.py
```

Opens a curses menu where you can configure speakers, turn order, initial prompts, and more. Press "Start Chat" to begin.

### Keyboard controls (during chat)

- **Esc** — quit (during input or LLM streaming)
- **Space** — pause/unpause (during LLM turn, takes effect between turns)
- **q** — quit (during user input)
- **clear** — reset conversation history

### Configurations

Chat configurations (speakers, order, initial prompt) are saved as JSON in `~/notes/chat_configs/`.

### Transcripts

Conversations are automatically saved to `~/notes/transcripts/` on exit.
