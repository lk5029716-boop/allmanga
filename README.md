# allmanga — Anime Streaming Scraper API

FastAPI backend that extracts **real direct video URLs** (mp4/m3u8) from AllAnime/mkissa.to — no iframes.

## Quick Start

```bash
pip install fastapi uvicorn httpx cryptography
uvicorn mkissa_api:app --host 0.0.0.0 --port 8000
```

Or use the full version with built-in test player UI:

```bash
uvicorn mkissa_full:app --host 0.0.0.0 --port 8000
# open http://localhost:8000
```

## Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /search?q=<name>&mode=sub` | Search anime, returns showId |
| `GET /episodes?showId=<id>` | Episode list / metadata |
| `GET /sources?showId=<id>&ep=<n>&mode=sub` | Real direct video URLs per server |
| `GET /proxy?url=<encoded>&ref=<encoded>` | Proxy stream (bypasses CORS/hotlink) |

## Servers Supported

| Server | Type | Status |
|---|---|---|
| Yt-mp4 | direct mp4 | ✅ Working |
| Mp4 (mp4upload) | direct mp4 (P.A.C.K.E.R deobf) | ✅ Working |
| Ok (ok.ru) | direct mp4 (data-options JSON parse) | ✅ Working |
| Fm-Hls (Filemoon) | hls (SPA — requires JS runtime) | ❌ Unresolved |
| Uni (allanime.uns.bio) | custom — rare | ❌ Unresolved |

## How It Works

1. **GraphQL persisted query** to `api.allanime.day/api`
2. **AES-256-GCM decryption** of `tobeparsed` blob (key = SHA256 `"Xot36i3lK3:v1"`)
3. **Per-host resolvers** bypass each iframe to extract direct mp4/m3u8 URLs
4. Returns `best` field with the top direct link

## Test

```bash
curl "http://127.0.0.1:8000/sources?showId=6uHjY9KytQFCE4cvJ&ep=9&mode=sub"
```

Returns direct playable URLs for Yt-mp4, mp4upload, and ok.ru servers.
