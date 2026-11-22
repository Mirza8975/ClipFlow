# ClipFlow

[![CI/CD](https://github.com/Mirza8975/ClipFlow/actions/workflows/ci.yml/badge.svg)](https://github.com/Mirza8975/ClipFlow/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED.svg?logo=docker&logoColor=white)](Dockerfile)

Turn long videos and live streams into captioned vertical clips, ready to post. Free and open source: run it on
your own computer or server, with your own AI key.

Paste a YouTube, Twitch or Kick link (or upload a file) and **ClipFlow** finds the moments worth clipping, cuts them to
vertical 9:16, adds word-by-word captions and an engaging hook, and can post them to your connected accounts. It can also record a
creator's live stream and clip the highlights while the stream is still running.

---

## What it does

- **Mobile-First Experience & Modern Dark UI.** Built with responsive layouts, Android-optimized navigation bar, and clean dark slate interface.
- **Clips from any video.** Transcribes the audio locally, asks an AI which moments would hold an audience's attention, and
  renders each one with animated captions in multiple stylish presets.
- **Social Safe Margins (Instagram Reels, TikTok & YouTube Shorts).** Automatic layout guides preventing captions and hooks from colliding with platform icons, search headers, or bottom titles.
- **Live clipping.** Records a stream into a rolling buffer and clips as it happens, using loudness, chat activity
  and the transcript.
- **Workspaces per creator.** Channels, clip settings, posting rules and an activity log for each creator you clip.
- **One-Click Auto-Posting.** Connect YouTube, Instagram and TikTok, then queue or auto-post finished clips with spacing and daily caps (see [DEPLOY.md](DEPLOY.md#connecting-youtube-instagram-and-tiktok)).
- **Bring Your Own AI.** First-class support for **Gemini**, **OpenAI**, **DeepSeek**, **Mistral**, **Grok (xAI)**, **Groq**, **OpenRouter**, **Claude**, **Ollama**, or any custom OpenAI-compatible endpoint. Clips can also be generated with no AI key at all using loudness detection.
- **Configurable Video & Export Quality.** Customizable vertical resolution (1080x1920), 30/60 FPS, CRF quality factors, and ffmpeg encoding speeds.
- **No sign-in on your own computer.** Opens straight into the dashboard. On a shared server, switch accounts on
  (`CLIPFLOW_ACCOUNTS=1`) for email or Google sign-in and an admin dashboard.

---

## Quick start

You need Python 3.10 or newer and `ffmpeg`.
- **Windows**: `winget install Gyan.FFmpeg`
- **macOS**: `brew install ffmpeg`
- **Linux / Ubuntu**: `sudo apt install ffmpeg`

ClipFlow detects `ffmpeg` automatically and informs you in the dashboard if it is missing.

### 1. Clone the repository

```bash
git clone https://github.com/Mirza8975/ClipFlow.git
cd ClipFlow
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Start the server

```bash
python -m studio.server
```

*(The first start automatically downloads the local speech recognition model, ~480 MB once).*

Open **http://127.0.0.1:8765**. It opens straight into the dashboard. Go to **Settings → AI**, choose
your preferred AI provider, paste your key, and press **Save and test**.

---

## Choose your AI Provider

ClipFlow uses chat models capable of returning structured JSON. Configure via the dashboard (**Settings → AI**) or in `.env`:

```ini
CLIPFLOW_AI_PROVIDER=gemini
CLIPFLOW_AI_MODELS=gemini-2.5-flash
CLIPFLOW_AI_KEY=your-api-key-here
```

| `CLIPFLOW_AI_PROVIDER` | Service | Where to get a key | Example Model |
|---|---|---|---|
| `gemini` | Google Gemini | https://aistudio.google.com/apikey | `gemini-2.5-flash` |
| `openai` | OpenAI | https://platform.openai.com/api-keys | `gpt-4o-mini` |
| `deepseek` | DeepSeek | https://platform.deepseek.com | `deepseek-chat` |
| `mistral` | Mistral AI | https://console.mistral.ai | `mistral-small-latest` |
| `xai` | Grok (xAI) | https://console.x.ai | `grok-4` |
| `groq` | Groq | https://console.groq.com/keys | `llama-3.3-70b-versatile` |
| `openrouter` | OpenRouter | https://openrouter.ai/keys | `google/gemini-2.5-flash` |
| `anthropic` | Anthropic Claude | https://console.anthropic.com | `claude-3-5-haiku-latest` |
| `ollama` | Local model (Ollama) | https://ollama.com | `llama3.1` |
| `custom` | Any OpenAI-compatible API | Set `CLIPFLOW_AI_BASE_URL` | e.g. `http://127.0.0.1:1234/v1` |

- `CLIPFLOW_AI_MODELS` accepts comma-separated model IDs for automatic fallback.
- Audio transcription runs 100% locally on your machine via `sherpa-onnx` — your private audio is never uploaded to the LLM.

---

## Caption Styles & Safe Margin Presets

ClipFlow includes built-in animated ASS caption presets and social media safe zones:
- **Styles**: `Bounce` (high-energy pop), `Hype` (bold impact), `Clean` (subtle and modern), `Neon` (vibrant cyber), `Minimal` (readable subtitle).
- **Safe Margins**:
  - `reels`: Instagram Reels (preserves right-side action buttons and bottom sound/caption metadata).
  - `tiktok`: TikTok (preserves search header and bottom sound metadata).
  - `shorts`: YouTube Shorts.
  - `none`: Full frame vertical layout.

---

## Docker Deployment

Run with Docker Compose:

```bash
cp .env.example .env
# Edit .env with your domain & settings
docker compose up -d --build
```

Container images are also automatically built and published via GitHub Actions to GitHub Container Registry:
```bash
docker pull ghcr.io/mirza8975/clipflow:latest
```

See [DEPLOY.md](DEPLOY.md) for full deployment instructions (Caddy HTTPS, Traefik, Nginx, GPU acceleration).

---

## Attribution & Acknowledgements

**ClipFlow** is an open-source project created and maintained by [Mirza8975](https://github.com/Mirza8975).

It is based upon the original **PeakCut** project by Haziq Ahmed Qasmi and PeakCut contributors, licensed under the MIT License. We gratefully acknowledge the architecture, pipeline design, and foundations established in the original project.

- Original Repository: [haziqahmedqasmi/PeakCut](https://github.com/haziqahmedqasmi/PeakCut)
- Speech recognition models provided by NVIDIA (Parakeet and FastConformer) under CC-BY-4.0.
- Bundled caption fonts (Anton, Archivo Black, Poppins) under SIL Open Font License.

---

## License

This project is licensed under the [MIT License](LICENSE).
