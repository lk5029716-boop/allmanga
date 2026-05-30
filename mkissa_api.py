"""
mkissa.to / AllAnime real video URL scraper API
Returns DIRECT video URLs (mp4/m3u8) - not iframes.

Endpoints:
    GET /search?q=<query>&mode=sub|dub
    GET /episodes?showId=<id>
    GET /sources?showId=<id>&ep=<n>&mode=sub|dub
"""

import base64, hashlib, html, json, re, urllib.parse
import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import FastAPI, HTTPException, Request

API      = "https://api.allanime.day/api"
REFERER  = "https://mkissa.to/"
ORIGIN   = "https://mkissa.to"
UA       = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"
AES_KEY  = hashlib.sha256(b"Xot36i3lK3:v1").digest()

# Only episode hash still works for GET. Search/show use POST.
HASH_EPISODE = "d405d0edd690624b66baba3068e0edc3ac90f1597d898a1ec8db4e5c43c00fec"

def decrypt_b7(blob_b64: str) -> dict:
    raw = base64.b64decode(blob_b64)
    pt  = AESGCM(AES_KEY).decrypt(raw[1:13], raw[13:], None)
    return json.loads(pt.decode())

def decrypt_response(text: str) -> dict | None:
    try:
        raw = base64.b64decode(text.strip())
        pt  = AESGCM(AES_KEY).decrypt(raw[1:13], raw[13:], None)
        return json.loads(pt.decode())
    except Exception:
        return None

# GQL queries (POST — works without persisted hash)
GQL_SEARCH = """query($search:SearchInput,$limit:Int,$page:Int,$translationType:String,$countryOrigin:String){
  shows(search:$search,limit:$limit,page:$page,translationType:$translationType,countryOrigin:$countryOrigin){
    pageInfo{total}
    edges{_id name type thumbnail genres status availableEpisodesDetail description}
  }
}"""

GQL_SHOW = """query($_id:String!){
  show(_id:$_id){_id name type thumbnail genres status altNames availableEpisodesDetail description broadcastInterval isAdult}
}"""


async def gql_post(client: httpx.AsyncClient, query: str, variables: dict) -> dict:
    HDRS = {"Referer": REFERER, "Origin": ORIGIN, "User-Agent": UA, "Content-Type": "application/json"}
    r = await client.post(API, json={"query": query, "variables": variables}, headers=HDRS, timeout=20)
    return r.json().get("data", {})


def deobf_packed(text: str) -> str:
    pk = re.search(r"}\s*\(\s*'(.+?)'\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*'([^']*)'\.split", text, re.DOTALL)
    if not pk:
        return text
    payload, base, _c, syms = pk.group(1), int(pk.group(2)), int(pk.group(3)), pk.group(4).split("|")
    def repl(m):
        try: i = int(m.group(0), base)
        except ValueError: return m.group(0)
        return syms[i] if i < len(syms) and syms[i] else m.group(0)
    return re.sub(r"\b[0-9a-zA-Z]+\b", repl, payload)


async def http_get(client: httpx.AsyncClient, url: str, referer: str = None) -> str:
    h = {"User-Agent": UA}
    if referer: h["Referer"] = referer
    r = await client.get(url, headers=h, timeout=20, follow_redirects=True)
    return r.text


# ── Server resolvers ──

async def resolve_player(s: dict) -> list:
    return [{"quality": "auto", "url": s["sourceUrl"], "format": s.get("fileExtenstion", "mp4")}]

async def resolve_okru(client: httpx.AsyncClient, url: str) -> list:
    text = await http_get(client, url)
    m = re.search(r'data-options="([^"]+)"', text)
    if not m:
        return []
    opts = json.loads(html.unescape(m.group(1)))
    fv   = opts.get("flashvars", {})
    if "metadata" not in fv:
        return []
    md  = json.loads(fv["metadata"])
    out = []
    for v in md.get("videos", []):
        out.append({"quality": v.get("name", "auto"), "url": v.get("url", ""), "format": "mp4"})
    if md.get("hlsManifestUrl"):
        out.append({"quality": "auto", "url": md["hlsManifestUrl"], "format": "hls"})
    return out

async def resolve_mp4upload(client: httpx.AsyncClient, url: str) -> list:
    text = await http_get(client, url, "https://mp4upload.com/")
    body = deobf_packed(text) + "\n" + text
    cands = [u for u in re.findall(r'(https?://[^\"\'\s\\]+\.mp4[^\"\'\s\\]*)', body)
             if "/d/" in u or "video.mp4" in u]
    return [{"quality": "auto", "url": cands[0], "format": "mp4"}] if cands else []

async def resolve_filemoon(client: httpx.AsyncClient, url: str) -> list:
    text = await http_get(client, url, REFERER)
    body = deobf_packed(text) + "\n" + text
    urls = re.findall(r'(https?://[^\"\'\s\\]+\.m3u8[^\"\'\s\\]*)', body)
    return [{"quality": "auto", "url": urls[0], "format": "hls"}] if urls else []


async def resolve_allanime_hex(client: httpx.AsyncClient, source_url: str) -> list:
    """Resolve --hex blob sources by fetching from Allanime's internal API.
    The hex blob decodes (XOR 0x38) to a path like /apivtwo/clock?id=<id>.
    We try multiple URL patterns on allanime.day.
    If server-side resolution fails, return the Allanime embed URL as fallback.
    """
    hex_str = source_url.lstrip("-")
    try:
        raw_bytes = bytes.fromhex(hex_str)
    except ValueError:
        return []

    # XOR 0x38 decode
    decoded = "".join(chr(b ^ 0x38) for b in raw_bytes)

    # Build .json?id= path (matching Allanime frontend xx() function)
    path = decoded.replace("?id=", ".json?id=") if "?id=" in decoded else decoded

    # Try fetching from Allanime CDN with multiple patterns
    urls_to_try = [
        f"https://allanime.day{path}",
        f"https://allanime.day{decoded}",
        f"https://allanime.day/apivtwo/clock.json?id={decoded.split('id=')[1]}" if "id=" in decoded else "",
    ]
    urls_to_try = [u for u in urls_to_try if u]

    for url in urls_to_try:
        try:
            r = await client.get(url, headers={"User-Agent": UA, "Referer": "https://mkissa.to/"}, timeout=20)
            if r.status_code != 200:
                continue
            text = r.text.strip()
            # Try plain JSON
            try:
                data = json.loads(text)
                links = data.get("links", [])
                if links:
                    return [{"quality": l.get("resolutionStr", l.get("label", "auto")),
                             "url": l["link"],
                             "format": "hls" if l["link"].endswith(".m3u8") else "mp4"}
                            for l in links]
            except (json.JSONDecodeError, KeyError, TypeError):
                pass
            # Try base64 AES-GCM decrypt
            dec = decrypt_response(text)
            if dec:
                links = dec.get("links", [])
                if links:
                    return [{"quality": l.get("resolutionStr", l.get("label", "auto")),
                             "url": l["link"],
                             "format": "hls" if l["link"].endswith(".m3u8") else "mp4"}
                            for l in links]
        except httpx.TimeoutException:
            continue
        except Exception:
            continue

    # Server-side resolution failed. Return empty — these are Allanime internal
    # tokens that need browser-side resolution via the Allanime embed player.
    return []


async def resolve_one(client: httpx.AsyncClient, src: dict) -> dict:
    name = src.get("sourceName")
    url  = src.get("sourceUrl", "")
    typ  = src.get("type")
    base = {"server": name, "type": typ, "embed": url}
    try:
        if typ == "player":
            return {**base, "links": await resolve_player(src)}
        if "ok.ru" in url:
            return {**base, "links": await resolve_okru(client, url)}
        if "mp4upload" in url:
            return {**base, "links": await resolve_mp4upload(client, url)}
        if any(d in url for d in ("filemoon", "bysekoze", "kerapoxy", "vidnest")) or name in ("Fm-Hls", "Vn-Hls"):
            return {**base, "links": await resolve_filemoon(client, url)}
        # --hex blob sources (Default, Luf-Mp4, Uv-mp4)
        # Decode: XOR each byte with 0x38 to get URL path like /apivtwo/clock?id=<id>
        # Then fetch from allanime.day and decrypt AES-GCM response
        return {**base, "links": await resolve_allanime_hex(client, url)}
    except Exception as e:
        return {**base, "links": [], "error": str(e)}


app = FastAPI(title="mkissa scraper")

@app.get("/search")
async def search(q: str, mode: str = "sub", limit: int = 20):
    async with httpx.AsyncClient() as c:
        d = await gql_post(c, GQL_SEARCH, {
            "search": {"allowAdult": False, "allowUnknown": False, "query": q},
            "limit": limit, "page": 1,
            "translationType": mode, "countryOrigin": "ALL"
        })
        return d

@app.get("/episodes")
async def episodes(showId: str):
    async with httpx.AsyncClient() as c:
        d = await gql_post(c, GQL_SHOW, {"_id": showId})
        return d

@app.get("/sources")
async def sources(showId: str, ep: str, mode: str = "sub"):
    async with httpx.AsyncClient(timeout=60) as c:
        # GET with persisted hash (only episode hash still works)
        v = json.dumps({"showId": showId, "translationType": mode, "episodeString": str(ep)})
        e = json.dumps({"persistedQuery": {"version": 1, "sha256Hash": HASH_EPISODE}})
        url = API + "?variables=" + urllib.parse.quote(v) + "&extensions=" + urllib.parse.quote(e)
        r = await c.get(url, headers={"Referer": REFERER, "User-Agent": UA})
        d = r.json().get("data", {})
        if "tobeparsed" in d:
            d = decrypt_b7(d["tobeparsed"])

        srcs = d.get("episode", {}).get("sourceUrls", [])
        if not srcs:
            raise HTTPException(404, "no sources found")

        resolved = []
        for s in sorted(srcs, key=lambda x: x.get("priority", 0), reverse=True):
            resolved.append(await resolve_one(c, s))

        # Attach proxy URLs
        for r in resolved:
            for l in r.get("links", []):
                ref = l.get("referer", "")
                l["proxy"] = "/proxy?url=" + urllib.parse.quote(l["url"], safe="")
                if ref:
                    l["proxy"] += "&ref=" + urllib.parse.quote(ref, safe="")

        direct = [lnk for r in resolved for lnk in r.get("links", [])]
        return {
            "showId": showId, "episode": ep, "mode": mode,
            "servers": resolved,
            "best": direct[0] if direct else None,
        }

@app.get("/proxy")
async def proxy(url: str, ref: str = "", request: Request = None):
    """Stream video with proper Referer, bypassing CORS/hotlink protection."""
    h = {"User-Agent": UA}
    if ref:
        h["Referer"] = ref
    rng = request.headers.get("range") if request else None
    if rng:
        h["Range"] = rng

    client = httpx.AsyncClient(timeout=None, follow_redirects=True)
    req    = client.build_request("GET", url, headers=h)
    up     = await client.send(req, stream=True)

    async def gen():
        try:
            async for chunk in up.aiter_raw():
                yield chunk
        finally:
            await up.aclose()
            await client.aclose()

    passthrough = {k: v for k, v in up.headers.items()
                   if k.lower() in ("content-type", "content-length", "content-range", "accept-ranges")}
    passthrough["Access-Control-Allow-Origin"] = "*"
    if "content-type" not in {k.lower() for k in passthrough}:
        passthrough["Content-Type"] = "video/mp4"
    return StreamingResponse(gen(), status_code=up.status_code, headers=passthrough)

from fastapi.responses import StreamingResponse  # noqa: E402
