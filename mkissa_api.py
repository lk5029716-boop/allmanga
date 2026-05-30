"""
mkissa.to / AllAnime real video URL scraper API
Returns DIRECT video URLs (mp4/m3u8) - not iframes.

Run:
    pip install fastapi uvicorn httpx cryptography
    uvicorn mkissa_api:app --host 0.0.0.0 --port 8000

Endpoints:
    GET /search?q=<query>&mode=sub|dub
    GET /episodes?showId=<id>
    GET /sources?showId=<id>&ep=<n>&mode=sub|dub
        -> resolved real video URLs (.mp4/.m3u8) for every available server
"""

import base64, hashlib, html, json, re
import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import FastAPI, HTTPException

API  = "https://api.allanime.day/api"
REF  = "https://mkissa.to/"
ORIG = "https://mkissa.to"
UA   = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"

PERSISTED = {
    "episode": "d405d0edd690624b66baba3068e0edc3ac90f1597d898a1ec8db4e5c43c00fec",
    "shows":   "06327bc10dd682e1ee7e07b6db9c16e9ad2fd56c1b769e47513128cd5c9fc77a",
    "show":    "afef3d3fd64b73d8e944d792fc0d24ad6e25406b91bf90c5d9c0ec0233635c44",
}


def decrypt_b7(blob_b64: str) -> str:
    raw = base64.b64decode(blob_b64)
    if raw[0] != 1:
        raise ValueError(f"Unsupported _m version byte {raw[0]}")
    iv, ct = raw[1:13], raw[13:]
    key = hashlib.sha256(b"Xot36i3lK3:v1").digest()
    return AESGCM(key).decrypt(iv, ct, None).decode()


async def gql(client, variables, sha):
    params = {
        "variables":  json.dumps(variables, separators=(",", ":")),
        "extensions": json.dumps({"persistedQuery": {"version": 1, "sha256Hash": sha}},
                                 separators=(",", ":")),
    }
    r = await client.get(API, params=params, headers={
        "Referer": REF, "Origin": ORIG, "User-Agent": UA,
    }, timeout=20)
    r.raise_for_status()
    data = r.json().get("data") or {}
    if "tobeparsed" in data:
        data = json.loads(decrypt_b7(data["tobeparsed"]))
    return data


def deobf_packed(text):
    pk = re.search(r"\}\s*\(\s*\'(.+?)\'\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*\'([^\']*)\'\.split",
                   text, re.DOTALL)
    if not pk:
        return text
    payload, base, _c, syms = pk.group(1), int(pk.group(2)), int(pk.group(3)), pk.group(4).split("|")
    def repl(m):
        w = m.group(0)
        try: i = int(w, base)
        except ValueError: return w
        return syms[i] if i < len(syms) and syms[i] else w
    return re.sub(r"\b[0-9a-zA-Z]+\b", repl, payload)


async def http_get(client, url, referer=None):
    h = {"User-Agent": UA}
    if referer: h["Referer"] = referer
    r = await client.get(url, headers=h, timeout=20, follow_redirects=True)
    return r.text


async def resolve_mp4upload(client, url):
    text = await http_get(client, url, "https://mp4upload.com/")
    body = deobf_packed(text) + "\n" + text
    cands = [u for u in re.findall(r'(https?://[^"\'\s\\]+\.mp4[^"\'\s\\]*)', body)
             if "/d/" in u or "video.mp4" in u]
    return [{"quality": "auto", "url": cands[0], "format": "mp4"}] if cands else []


async def resolve_filemoon(client, url):
    text = await http_get(client, url, REF)
    body = deobf_packed(text) + "\n" + text
    urls = re.findall(r'(https?://[^"\'\s\\]+\.m3u8[^"\'\s\\]*)', body)
    return [{"quality": "auto", "url": urls[0], "format": "hls"}] if urls else []


async def resolve_okru(client, url):
    text = await http_get(client, url)
    m = re.search(r'data-options="([^"]+)"', text)
    if not m:
        return []
    opts = json.loads(html.unescape(m.group(1)))
    fv = opts.get("flashvars", {})
    out = []
    if "metadata" in fv:
        md = json.loads(fv["metadata"])
        for v in md.get("videos", []):
            out.append({"quality": v.get("name"), "url": v.get("url"), "format": "mp4"})
        if md.get("hlsManifestUrl"):
            out.append({"quality": "auto", "url": md["hlsManifestUrl"], "format": "hls"})
    return out


async def resolve_allanime_internal(client, source_url):
    sid = source_url.lstrip("-")
    pairs = {"01":"9","02":"0","03":"1","04":"2","05":"3","06":"4","07":"5","08":"6",
             "09":"7","0a":"8","0b":".","0c":"<","0d":">","0e":"/","0f":"?","00":":",
             "5c":"/","79":"H","7a":"I","7b":"J"}
    out_chars = []
    for i in range(0, len(sid), 2):
        seg = sid[i:i+2]
        if len(seg) < 2: break
        out_chars.append(pairs.get(seg, chr(int(seg, 16) ^ 0x37)))
    decoded = "".join(out_chars)
    path = decoded.replace("clock", "clock.json") if "clock" in decoded else decoded
    r = await client.get("https://allanime.day" + path,
                         headers={"Referer": "https://allanime.day/", "User-Agent": UA},
                         timeout=20)
    try:
        return [{"quality": l.get("resolutionStr", "auto"),
                 "url": l["link"],
                 "format": "hls" if l["link"].endswith(".m3u8") else "mp4"}
                for l in r.json().get("links", [])]
    except Exception:
        return []


async def resolve_one(client, src):
    name = src.get("sourceName"); url = src.get("sourceUrl", ""); typ = src.get("type")
    base = {"server": name, "type": typ, "embed": url}
    try:
        if typ == "player":
            return {**base, "links": [{"quality": "auto", "url": url,
                                        "format": src.get("fileExtenstion", "mp4")}]}
        if url.startswith("--"):
            return {**base, "links": await resolve_allanime_internal(client, url)}
        if "mp4upload" in url:
            return {**base, "links": await resolve_mp4upload(client, url)}
        if "ok.ru" in url:
            return {**base, "links": await resolve_okru(client, url)}
        if any(d in url for d in ("filemoon", "bysekoze", "kerapoxy")) or name in ("Fm-Hls", "Filemoon"):
            return {**base, "links": await resolve_filemoon(client, url)}
        return {**base, "links": [], "note": "resolver_not_implemented"}
    except Exception as e:
        return {**base, "links": [], "error": str(e)}


app = FastAPI(title="mkissa scraper")

@app.get("/search")
async def search(q: str, mode: str = "sub", limit: int = 20):
    async with httpx.AsyncClient(http2=False) as c:
        return await gql(c, {
            "search": {"allowAdult": False, "allowUnknown": False, "query": q},
            "limit": limit, "page": 1, "translationType": mode, "countryOrigin": "ALL"
        }, PERSISTED["shows"])

@app.get("/episodes")
async def episodes(showId: str):
    async with httpx.AsyncClient(http2=False) as c:
        return await gql(c, {"_id": showId}, PERSISTED["show"])

@app.get("/sources")
async def sources(showId: str, ep: str, mode: str = "sub"):
    async with httpx.AsyncClient(http2=False) as c:
        d = await gql(c, {"showId": showId, "translationType": mode, "episodeString": ep},
                      PERSISTED["episode"])
        srcs = (d.get("episode") or {}).get("sourceUrls", [])
        if not srcs:
            raise HTTPException(404, "no sources")
        resolved = []
        for s in sorted(srcs, key=lambda x: x.get("priority", 0), reverse=True):
            resolved.append(await resolve_one(c, s))
    direct = [l for r in resolved for l in r.get("links", [])]
    return {
        "showId": showId, "episode": ep, "mode": mode,
        "servers": resolved,
        "best": direct[0] if direct else None,
    }
