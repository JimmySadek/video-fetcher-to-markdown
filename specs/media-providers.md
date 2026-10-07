---
status: complete (local; not pushed)
---

# Media providers: Instagram, TikTok, X, Vimeo and other sites

## Approval View

- **Intent:** Capture what is said (and shown) in short videos from sites other than YouTube, and in YouTube videos
  that have no captions.
- **Decided by Jimmy on 2026-10-07:** a second script in the same skill (`scripts/fetch_media.py`); keep the name
  `youtube-fetcher` and widen the description; on a login wall use the agent's built-in browser fallback, and read
  browser cookies only when the user asks in that chat; frames (a contact sheet) by default for videos up to
  3 minutes.
- **Reverses** the v1.2 exclusions "audio downloads/transcription" and "browser cookie access" (cookies stay opt-in).
- **Not included:** playlists, paid transcription services, speaker identification, automatic dependency
  installation, changes to `fetch_transcript.py` behaviour.
- **Protected:** push, tag, release and public README publication need Jimmy's separate yes.

## How it works

```
URL ─▶ yt-dlp metadata ─┬─ login wall ─▶ exit 4 + browser fallback steps (agent) ─▶ local file ─┐
                        └─ ok ─▶ yt-dlp download (audio, or video when frames are wanted)        │
local file ◀────────────────────────────────────────────────────────────────────────────────────┘
   ─▶ ffprobe ─▶ ffmpeg 16 kHz mono WAV ─▶ Whisper CLI (mlx_whisper, else whisper) ─▶ segments
   ─▶ ffmpeg contact sheet (short videos) ─▶ Markdown / txt / json / srt / vtt
```

## Tasks

- [x] `scripts/fetch_media.py`: URL or local file; same output precedence, `--force`, exit 3 as `fetch_transcript.py`
- [x] Exit 4 for login or access walls, with the browser-fallback steps in the message
- [x] `--cookies-from-browser` passed to yt-dlp only when given
- [x] Whisper via CLI: `mlx_whisper` then `whisper`; `--hint` words as the initial prompt; `--lang`
- [x] Contact sheet with tile times listed in the note; auto for <= 180 s, `--frames` / `--no-frames`
- [x] Tests that need no network, no yt-dlp and no Whisper (subprocess replaced at the boundary)
- [x] SKILL.md: description, when to use which script, browser fallback recipe, cookie rule, frames
- [x] README: providers section, install of optional tools, limits
- [x] Bundle test and CI compile step include the new script
- [x] Live check: the Instagram reel `DeHP8DPsimI` via the browser fallback, plus one open site (Vimeo or TikTok)

## Verification (2026-10-07)

- 76 tests pass (61 existing + 15 new) on Python 3.12 and on Python 3.8 (uv, minimum
  supported `youtube-transcript-api` range). The new tests replace yt-dlp, ffmpeg and
  Whisper at the subprocess boundary, so CI needs none of them.
- `.scripts/verify-isolated-install.sh` passes with skills CLI 1.5.23 and now checks
  the two new files.
- Live: Instagram reel `DeHP8DPsimI` exits 4 through yt-dlp, then the browser
  fallback (`browser_media_links.js` in the built-in browser, curl, `--audio-file`)
  produces a note, a 24-tile contact sheet and a correct transcript; `--hint Claude`
  fixed "cloud" → "Claude". TikTok works directly (note + frames).
- yt-dlp updated 2026.03.17 → 2026.08.19 (Jimmy's yes). After the update the Instagram
  reel downloads directly, no browser needed. Vimeo `76979871` now reports "only works
  when logged-in" (a real Vimeo change), and its page shows "This video is
  processing" in a browser too, so it could not be captured either way.
- The browser snippet failed once: Instagram escapes `%` as `\u0025` in page HTML, so
  quality tags were unreadable and a random video rendition was picked. Fixed with a
  generic `\uXXXX` unescape; it now returns audio + q90 video from HTML alone. It also
  returns `.m3u8`/`.mpd` playlists as `stream`, which `fetch_media.py` accepts with
  `--source-url`, `--title`, `--creator` overrides (covered by a test).
- yt-dlp warns that Python 3.10 support is deprecated: its pyenv 3.10 install will stop
  receiving updates. Recommend `brew install yt-dlp`.
- Not tested live: X, Facebook, YouTube without captions, the `whisper` (non-mlx)
  engine, Windows.
