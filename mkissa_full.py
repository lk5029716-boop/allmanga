"""
mkissa.to / AllAnime real video scraper â€” backend + test frontend in ONE file.

Run:
    pip install fastapi uvicorn httpx cryptography
    uvicorn mkissa_full:app --host 0.0.0.0 --port 8000

Open in browser:
    http://localhost:8000/                 -> test player UI
    http://localhost:8000/search?q=naruto
    http://localhost:8000/sources?showId=<id>&ep=<n>&mode=sub
    http://localhost:8000/proxy?url=<encoded>&ref=<encoded>   (streams video with proper Referer)
"""

import base64, hashlib, html as ihtml, json, re, urllib.parse
import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

API  = "https://api.allanime.day/api"
REF  = "https://mkissa.to/"
ORIG = "https://mkissa.to"
UA   = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"

PERSISTED = {
    "episode": "d405d0edd690624b66baba3068e0edc3ac90f1597d898a1ec8db4e5c43c00fec",
    "shows":   "06327bc10dd682e1ee7e07b6db9c16e9ad2fd56c1b769e47513128cd5c9fc77a",
    "show":    "afef3d3fd64b73d8e944d792fc0d24ad6e25406b91bf90c5d9c0ec0233635c44",
}

def decrypt_b7(blob_b64):
    raw = base64.b64decode(blob_b64)
    if raw[0] != 1:
        raise ValueError("bad version")
    iv, ct = raw[1:13], raw[13:]
    key = hashlib.sha256(b"Xot36i3lK3:v1").digest()
    return AESGCM(key).decrypt(iv, ct, None).decode()


async def gql(client, variables, sha):
    params = {
        "variables":  json.dumps(variables, separators=(",", ":")),
        "extensions": json.dumps({"persistedQuery":{"version":1,"sha256Hash":sha}}, separators=(",", ":")),
    }
    r = await client.get(API, params=params, headers={"Referer":REF,"Origin":ORIG,"User-Agent":UA}, timeout=20)
    r.raise_for_status()
    data = r.json().get("data") or {}
    if "tobeparsed" in data:
        data = json.loads(decrypt_b7(data["tobeparsed"]))
    return data


def deobf_packed(text):
    pk = re.search(r"\}\s*\(\s*'(.+?)'\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*'([^']*)'\.split", text, re.DOTALL)
    if not pk: return text
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
    return [{"quality":"auto","url":cands[0],"format":"mp4","referer":"https://mp4upload.com/"}] if cands else []


async def resolve_filemoon(client, url):
    text = await http_get(client, url, REF)
    body = deobf_packed(text) + "\n" + text
    urls = re.findall(r'(https?://[^"\'\s\\]+\.m3u8[^"\'\s\\]*)', body)
    return [{"quality":"auto","url":urls[0],"format":"hls","referer":url}] if urls else []


async def resolve_okru(client, url):
    text = await http_get(client, url)
    m = re.search(r'data-options="([^"]+)"', text)
    if not m: return []
    opts = json.loads(ihtml.unescape(m.group(1)))
    fv = opts.get("flashvars", {})
    out = []
    if "metadata" in fv:
        md = json.loads(fv["metadata"])
        for v in md.get("videos", []):
            out.append({"quality": v.get("name"), "url": v.get("url"), "format":"mp4","referer":"https://ok.ru/"})
        if md.get("hlsManifestUrl"):
            out.append({"quality":"auto","url":md["hlsManifestUrl"],"format":"hls","referer":"https://ok.ru/"})
    return out


async def resolve_allanime_internal(client, source_url):
    sid = source_url.lstrip("-")
    pairs = {"01":"9","02":"0","03":"1","04":"2","05":"3","06":"4","07":"5","08":"6","09":"7","0a":"8",
             "0b":".","0c":"<","0d":">","0e":"/","0f":"?","00":":","5c":"/","79":"H","7a":"I","7b":"J"}
    out = []
    for i in range(0, len(sid), 2):
        seg = sid[i:i+2]
        if len(seg) < 2: break
        out.append(pairs.get(seg, chr(int(seg, 16) ^ 0x37)))
    decoded = "".join(out)
    path = decoded.replace("clock", "clock.json") if "clock" in decoded else decoded
    r = await client.get("https://allanime.day"+path,
                         headers={"Referer":"https://allanime.day/","User-Agent":UA}, timeout=20)
    try:
        return [{"quality":l.get("resolutionStr","auto"),"url":l["link"],
                 "format":"hls" if l["link"].endswith(".m3u8") else "mp4",
                 "referer":"https://allanime.day/"} for l in r.json().get("links",[])]
    except Exception:
        return []


async def resolve_one(client, src):
    name = src.get("sourceName"); url = src.get("sourceUrl",""); typ = src.get("type")
    base = {"server":name,"type":typ,"embed":url}
    try:
        if typ == "player":
            return {**base, "links":[{"quality":"auto","url":url,"format":src.get("fileExtenstion","mp4"),"referer":REF}]}
        if url.startswith("--"):
            return {**base, "links": await resolve_allanime_internal(client, url)}
        if "mp4upload" in url:
            return {**base, "links": await resolve_mp4upload(client, url)}
        if "ok.ru" in url:
            return {**base, "links": await resolve_okru(client, url)}
        if any(d in url for d in ("filemoon","bysekoze","kerapoxy")) or name in ("Fm-Hls","Filemoon"):
            return {**base, "links": await resolve_filemoon(client, url)}
        return {**base, "links":[], "note":"resolver_not_implemented"}
    except Exception as e:
        return {**base, "links":[], "error":str(e)}


app = FastAPI(title="mkissa scraper")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/search")
async def search(q: str, mode: str = "sub", limit: int = 20):
    async with httpx.AsyncClient() as c:
        return await gql(c, {
            "search":{"allowAdult":False,"allowUnknown":False,"query":q},
            "limit":limit,"page":1,"translationType":mode,"countryOrigin":"ALL"
        }, PERSISTED["shows"])


@app.get("/episodes")
async def episodes(showId: str):
    async with httpx.AsyncClient() as c:
        return await gql(c, {"_id":showId}, PERSISTED["show"])


@app.get("/sources")
async def sources(showId: str, ep: str, mode: str = "sub"):
    async with httpx.AsyncClient() as c:
        d = await gql(c, {"showId":showId,"translationType":mode,"episodeString":ep}, PERSISTED["episode"])
        srcs = (d.get("episode") or {}).get("sourceUrls", [])
        if not srcs:
            raise HTTPException(404, "no sources")
        resolved = []
        for s in sorted(srcs, key=lambda x: x.get("priority",0), reverse=True):
            resolved.append(await resolve_one(c, s))
    direct = [l for r in resolved for l in r.get("links",[])]
    for r in resolved:
        for l in r.get("links", []):
            l["proxy"] = f"/proxy?url={urllib.parse.quote(l['url'], safe='')}&ref={urllib.parse.quote(l.get('referer',''), safe='')}"
    return {"showId":showId, "episode":ep, "mode":mode, "servers":resolved,
            "best": (direct[0] if direct else None)}


@app.get("/proxy")
async def proxy(url: str, ref: str = "", request: Request = None):
    """Stream video through this server with proper Referer so the browser can play it.
    Bypasses CORS / hotlink protection. Supports HTTP Range for seeking."""
    headers = {"User-Agent": UA}
    if ref: headers["Referer"] = ref
    rng = request.headers.get("range") if request else None
    if rng: headers["Range"] = rng

    client = httpx.AsyncClient(timeout=None, follow_redirects=True)
    req = client.build_request("GET", url, headers=headers)
    upstream = await client.send(req, stream=True)

    async def gen():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    passthrough = {k:v for k,v in upstream.headers.items()
                   if k.lower() in ("content-type","content-length","content-range","accept-ranges","cache-control")}
    passthrough["Access-Control-Allow-Origin"] = "*"
    if "content-type" not in {k.lower() for k in passthrough}:
        passthrough["Content-Type"] = "video/mp4"
    return StreamingResponse(gen(), status_code=upstream.status_code, headers=passthrough)


@app.get("/", response_class=HTMLResponse)
async def index():
    return INDEX_HTML


INDEX_HTML = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>mkissa scraper â€” test player</title>
<script src="https://cdn.jsdelivr.net/npm/hls.js@1"></script>
<style>
body{font-family:system-ui,sans-serif;max-width:920px;margin:24px auto;padding:0 16px;background:#111;color:#eee}
input,button,select{padding:8px;margin:4px 0;font-size:14px;background:#222;color:#eee;border:1px solid #444;border-radius:4px}
input{width:300px}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
video{width:100%;background:#000;border-radius:6px;margin-top:12px}
.srv{padding:6px 10px;margin:4px 4px 4px 0;background:#1e3a8a;border:none;color:#fff;border-radius:4px;cursor:pointer;font-size:13px}
.srv:hover{background:#2563eb}
.bad{background:#444!important;cursor:not-allowed}
pre{background:#000;padding:10px;border-radius:6px;font-size:11px;max-height:200px;overflow:auto}
h2{margin-top:24px}
a{color:#60a5fa}
</style></head><body>

<h1>mkissa scraper â€” test player</h1>

<h2>1. Search</h2>
<div class="row">
  <input id="q" placeholder="anime name (e.g. iruma)" value="iruma">
  <select id="mode"><option>sub</option><option>dub</option></select>
  <button onclick="doSearch()">Search</button>
</div>
<div id="results"></div>

<h2>2. Episodes</h2>
<div class="row">
  <input id="showId" placeholder="showId" value="6uHjY9KytQFCE4cvJ">
  <input id="ep" placeholder="ep #" value="9" style="width:80px">
  <button onclick="loadSources()">Load sources</button>
</div>

<h2>3. Servers (click to play)</h2>
<div id="servers"></div>

<video id="player" controls autoplay></video>
<p id="status"></p>

<h2>Raw API response</h2>
<pre id="raw"></pre>

<script>
const $ = id => document.getElementById(id);

async function doSearch() {
  const q = $('q').value, mode = $('mode').value;
  const r = await fetch(`/search?q=${encodeURIComponent(q)}&mode=${mode}`).then(r=>r.json());
  const shows = (r.shows && r.shows.edges) || [];
  $('results').innerHTML = shows.map(s =>
    `<div><button class="srv" onclick="$('showId').value='${s._id}';loadSources()">${s.name} (${s._id})</button></div>`
  ).join('') || '<em>no results</em>';
}

async function loadSources() {
  const showId = $('showId').value, ep = $('ep').value, mode = $('mode').value;
  $('status').textContent = 'Resolving real video URLs...';
  const r = await fetch(`/sources?showId=${showId}&ep=${ep}&mode=${mode}`).then(r=>r.json());
  $('raw').textContent = JSON.stringify(r, null, 2);
  const buttons = [];
  for (const srv of r.servers || []) {
    for (const l of srv.links || []) {
      buttons.push(`<button class="srv" onclick='play(${JSON.stringify(l).replace(/'/g,"&apos;")})'>${srv.server} Â· ${l.quality} Â· ${l.format}</button>`);
    }
    if (!srv.links || !srv.links.length)
      buttons.push(`<button class="srv bad" disabled>${srv.server} Â· (unresolved)</button>`);
  }
  $('servers').innerHTML = buttons.join(' ');
  $('status').textContent = `Found ${buttons.length} direct links. Click a server to play.`;
}

let hls = null;
function play(link) {
  const v = $('player');
  const src = link.proxy || link.url;
  $('status').textContent = 'Playing: ' + src;
  if (hls) { hls.destroy(); hls = null; }
  if (link.format === 'hls' && window.Hls && Hls.isSupported()) {
    hls = new Hls();
    hls.loadSource(src);
    hls.attachMedia(v);
  } else {
    v.src = src;
  }
  v.play().catch(e => $('status').textContent = 'Play error: ' + e.message);
}
</script>
</body></html>
"""
