"""
AllManga — Anime Streaming Scraper API

Endpoints:
  GET /search?q=naruto&mode=sub       — search anime
  GET /episodes?showId=xxx             — episode list
  GET /sources?showId=xxx&ep=1&mode=sub — real direct video URLs
  GET /proxy?url=xxx&ref=xxx           — stream video through proxy
  GET /                                — player UI
"""

import base64, hashlib, html as ihtml, json, re, urllib.parse
import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

API_URL = "https://api.allanime.day/api"
REFERER = "https://allanime.to/"
ORIGIN  = "https://allanime.to"
UA      = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"
AES_KEY = hashlib.sha256(b"Xot36i3lK3:v1").digest()

# ─── GraphQL queries ───

FRAGMENT_SHOW = """
_id name nameOnlyString availableEpisodesDetail thumbnail type isAdult altNames
"""

FRAGMENT_SHOW_DETAILED = """
_id name nameOnlyString availableEpisodesDetail thumbnail type isAdult altNames
description broadcastInterval banner characters countryOfOrigin genres tags
airedEnd rating status
"""

QUERY_SEARCH = """
query($search:SearchInput,$limit:Int,$page:Int,$translationType:VaildTranslationTypeEnumType,$countryOrigin:VaildCountryOriginEnumType){
  shows(search:$search,limit:$limit,page:$page,translationType:$translationType,countryOrigin:$countryOrigin){
    pageInfo{total} edges{""" + FRAGMENT_SHOW + """}
  }
}"""

QUERY_EPISODE = """
query($showId:String!,$translationType:VaildTranslationTypeEnumType!,$episodeString:String!){
  episode(showId:$showId,translationType:$translationType,episodeString:$episodeString){
    episodeString uploadDate sourceUrls thumbnail notes
    show{""" + FRAGMENT_SHOW_DETAILED + """}
    episodeInfo{notes vidInforssub vidInforsdub description}
  }
}"""

QUERY_SHOW = """
query($_id:String!){show(_id:$_id){""" + FRAGMENT_SHOW_DETAILED + """ availableEpisodesDetail}}"""


# ─── AES-256-GCM decryption ───

def decrypt_b7(blob_b64: str) -> str:
    raw = base64.b64decode(blob_b64)
    assert raw[0] == 1, f"unexpected version {raw[0]}"
    iv, ct = raw[1:13], raw[13:]
    return AESGCM(AES_KEY).decrypt(iv, ct, None).decode()


async def gql(client: httpx.AsyncClient, query: str, variables: dict) -> dict:
    r = await client.post(API_URL, json={"query": query, "variables": variables},
        headers={"Referer": REFERER, "Origin": ORIGIN, "User-Agent": UA,
                 "Content-Type": "application/json", "Accept": "application/json"},
        timeout=20)
    r.raise_for_status()
    resp = r.json()
    errors = resp.get("errors") or []
    if any(e.get("message") == "NEED_CAPTCHA" for e in errors):
        raise HTTPException(403, "captcha_required")
    data = resp.get("data") or {}
    if "tobeparsed" in data:
        data = json.loads(decrypt_b7(data["tobeparsed"]))
    return data


async def gql_persisted_get(client: httpx.AsyncClient, variables: dict, sha: str) -> dict:
    params = {
        "variables": json.dumps(variables, separators=(",", ":")),
        "extensions": json.dumps({"persistedQuery": {"version": 1, "sha256Hash": sha}}, separators=(",", ":")),
    }
    r = await client.get(API_URL, params=params,
        headers={"Referer": "https://mkissa.to/", "Origin": "https://mkissa.to", "User-Agent": UA},
        timeout=20)
    r.raise_for_status()
    data = r.json().get("data") or {}
    if "tobeparsed" in data:
        data = json.loads(decrypt_b7(data["tobeparsed"]))
    return data


# ─── Server resolvers ───

def deobf_packed(text: str) -> str:
    pk = re.search(r"}\s*\(\s*'(.+?)'\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*'([^']*)'\.split", text, re.DOTALL)
    if not pk:
        return text
    payload, radix, _count, syms = pk.group(1), int(pk.group(2)), int(pk.group(3)), pk.group(4).split("|")
    def repl(m):
        w = m.group(0)
        try: i = int(w, radix)
        except ValueError: return w
        return syms[i] if i < len(syms) and syms[i] else w
    return re.sub(r"\b[0-9a-zA-Z]+\b", repl, payload)


async def http_get(client, url, referer=None):
    h = {"User-Agent": UA}
    if referer: h["Referer"] = referer
    r = await client.get(url, headers=h, timeout=20, follow_redirects=True)
    return r.text


async def resolve_player(s):
    return [{"quality": "auto", "url": s["sourceUrl"], "format": s.get("fileExtenstion", "mp4"), "referer": REFERER}]


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
            out.append({"quality": v.get("name"), "url": v.get("url"), "format": "mp4", "referer": "https://ok.ru/"})
        if md.get("hlsManifestUrl"):
            out.append({"quality": "auto", "url": md["hlsManifestUrl"], "format": "hls", "referer": "https://ok.ru/"})
    return out


async def resolve_mp4upload(client, url):
    text = await http_get(client, url, "https://mp4upload.com/")
    body = deobf_packed(text) + "\n" + text
    cands = [u for u in re.findall(r'(https?://[^"\'\s\\]+\.mp4[^"\'\s\\]*)', body)
             if "/d/" in u or "video.mp4" in u]
    return [{"quality": "auto", "url": cands[0], "format": "mp4", "referer": "https://mp4upload.com/"}] if cands else []


async def resolve_vidnest(client, url):
    """Resolve vidnest.io embed by POSTing to /dl and extracting direct MP4 URLs."""
    m = re.search(r'/e/([a-zA-Z0-9_-]+)', url)
    if not m: return []
    file_code = m.group(1)
    try:
        r = await client.post("https://vidnest.io/dl",
            data={"op": "embed", "file_code": file_code, "referer": "", "auto": "1"},
            headers={"User-Agent": UA, "Referer": f"https://vidnest.io/e/{file_code}",
                     "Content-Type": "application/x-www-form-urlencoded"},
            timeout=15, follow_redirects=True)
        mp4_urls = re.findall(r'(https?://[^"\'\s<>]+\.mp4[^"\'\s<>]*)', r.text)
        seen, out = set(), []
        for u in mp4_urls:
            if u not in seen:
                seen.add(u)
                q = "1080p" if "_x/" in u else "720p" if "_o/" in u else "auto"
                out.append({"quality": q, "url": u, "format": "mp4"})
        return out
    except Exception:
        return []


async def resolve_filemoon(client, url):
    text = await http_get(client, url, REFERER)
    body = deobf_packed(text) + "\n" + text
    urls = re.findall(r'(https?://[^"\'\s\\]+\.m3u8[^"\'\s\\]*)', body)
    return [{"quality": "auto", "url": urls[0], "format": "hls", "referer": url}] if urls else []


async def resolve_bysekoze(client, url):
    """Resolve bysekoze.com via API. Stream requires JW Player + AES in browser."""
    m = re.search(r'/e/([a-zA-Z0-9_-]+)', url)
    if not m: return []
    code = m.group(1)
    try:
        r = await client.get(f"https://bysekoze.com/api/videos/{code}/embed/details",
            headers={"User-Agent": UA, "Referer": f"https://bysekoze.com/e/{code}",
                     "X-Embed-Origin": "https://allanime.to",
                     "X-Embed-Referer": "https://allanime.to/",
                     "X-Embed-Parent": "https://allanime.to"},
            timeout=10)
        if r.status_code == 200:
            data = r.json()
            embed_url = data.get("embed_frame_url", "")
            if embed_url:
                return [{"quality": "auto", "url": embed_url, "format": "iframe"}]
    except Exception:
        pass
    return []


async def resolve_unsbio(client, url):
    """Resolve uns.bio embed via their API. Response may need browser-side decoding."""
    m = re.search(r'#([a-zA-Z0-9_-]+)', url)
    if not m: return []
    hash_val = m.group(1)
    try:
        r = await client.get(f"https://allanime.uns.bio/api/v1/info?id={hash_val}",
            headers={"User-Agent": UA, "Referer": f"https://allanime.uns.bio/#{hash_val}"},
            timeout=10)
        if r.status_code == 200:
            text = r.text.strip()
            try:
                raw = bytes.fromhex(text)
                if raw[0] == 1:
                    pt = AESGCM(AES_KEY).decrypt(raw[1:13], raw[13:], None)
                    data = json.loads(pt.decode())
                    links = data.get("links", [])
                    if links:
                        return [{"quality": l.get("resolutionStr", "auto"),
                                 "url": l["link"],
                                 "format": "hls" if l["link"].endswith(".m3u8") else "mp4"}
                                for l in links]
            except Exception:
                pass
    except Exception:
        pass
    return []


async def resolve_allanime_internal(client, source_url):
    """Resolve AllAnime internal (--hex) source URLs via XOR-0x38 hex decode."""
    sid = source_url.lstrip("-")
    try:
        raw_bytes = bytes.fromhex(sid)
    except ValueError:
        return []
    decoded = "".join(chr(b ^ 0x38) for b in raw_bytes)
    path = decoded.replace("?id=", ".json?id=") if "?id=" in decoded else decoded

    urls_to_try = [f"https://allanime.day{path}", f"https://allanime.day{decoded}"]
    if "id=" in decoded:
        urls_to_try.append(f"https://allanime.day/apivtwo/clock.json?id={decoded.split('id=')[1]}")

    for url in urls_to_try:
        try:
            r = await client.get(url, headers={"Referer": "https://mkissa.to/", "User-Agent": UA}, timeout=15)
            if r.status_code == 200:
                text = r.text.strip()
                try:
                    data = json.loads(text)
                    links = data.get("links", [])
                except (json.JSONDecodeError, TypeError):
                    links = []
                if not links:
                    try:
                        raw = base64.b64decode(text)
                        pt = AESGCM(AES_KEY).decrypt(raw[1:13], raw[13:], None)
                        data = json.loads(pt.decode())
                        links = data.get("links", [])
                    except Exception:
                        links = []
                if links:
                    return [{"quality": l.get("resolutionStr", l.get("label", "auto")),
                             "url": l["link"],
                             "format": "hls" if l["link"].endswith(".m3u8") else "mp4",
                             "referer": "https://allanime.day/"}
                            for l in links]
        except Exception:
            continue
    return []


async def resolve_one(client, src):
    name = src.get("sourceName")
    url  = src.get("sourceUrl", "")
    typ  = src.get("type")
    base = {"server": name, "type": typ, "embed": url}
    try:
        if typ == "player":
            return {**base, "links": await resolve_player(src)}
        if url.startswith("--"):
            return {**base, "links": await resolve_allanime_internal(client, url)}
        if "ok.ru" in url:
            return {**base, "links": await resolve_okru(client, url)}
        if "mp4upload" in url:
            return {**base, "links": await resolve_mp4upload(client, url)}
        if "vidnest" in url or name == "Vn-Hls":
            return {**base, "links": await resolve_vidnest(client, url)}
        if "bysekoze" in url or "filemoon" in url or name == "Fm-Hls":
            return {**base, "links": await resolve_bysekoze(client, url)}
        if "uns.bio" in url or name == "Uni":
            return {**base, "links": await resolve_unsbio(client, url)}
        if any(d in url for d in ("filemoon", "kerapoxy")):
            return {**base, "links": await resolve_filemoon(client, url)}
        return {**base, "links": [], "note": "resolver_not_implemented"}
    except Exception as e:
        return {**base, "links": [], "error": str(e)}


# ─── FastAPI app ───

app = FastAPI(title="AllManga API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/search")
async def search(q: str, mode: str = "sub", limit: int = 20, page: int = 1, detailed: bool = False):
    async with httpx.AsyncClient() as c:
        return await gql(c, QUERY_SEARCH if not detailed else QUERY_SEARCH, {
            "search": {"allowAdult": False, "allowUnknown": False, "query": q},
            "limit": limit, "page": page, "translationType": mode, "countryOrigin": "ALL"})


@app.get("/episodes")
async def episodes(showId: str):
    async with httpx.AsyncClient() as c:
        return await gql(c, QUERY_SHOW, {"_id": showId})


@app.get("/sources")
async def sources(showId: str, ep: str, mode: str = "sub"):
    EPISODE_SHA = "d405d0edd690624b66baba3068e0edc3ac90f1597d898a1ec8db4e5c43c00fec"
    async with httpx.AsyncClient(timeout=60) as c:
        try:
            data = await gql(c, QUERY_EPISODE,
                {"showId": showId, "translationType": mode, "episodeString": ep})
        except HTTPException:
            data = await gql_persisted_get(c,
                {"showId": showId, "translationType": mode, "episodeString": ep}, EPISODE_SHA)
        episode_data = data.get("episode") or {}
        srcs = episode_data.get("sourceUrls", [])
        if not srcs:
            raise HTTPException(404, "no sources found for this episode")

        resolved = []
        for s in sorted(srcs, key=lambda x: x.get("priority", 0), reverse=True):
            resolved.append(await resolve_one(c, s))

    direct = [link for r in resolved for link in r.get("links", [])]
    for r in resolved:
        for link in r.get("links", []):
            link["proxy"] = (
                f"/proxy?url={urllib.parse.quote(link['url'], safe='')}"
                f"&ref={urllib.parse.quote(link.get('referer', ''), safe='')}"
            )

    return {
        "showId": showId, "episode": ep, "mode": mode,
        "episodeData": {"notes": episode_data.get("notes"),
                        "thumbnail": episode_data.get("thumbnail"),
                        "uploadDate": episode_data.get("uploadDate")},
        "show": episode_data.get("show"),
        "servers": resolved,
        "best": direct[0] if direct else None,
        "total": len(direct),
    }


@app.get("/proxy")
async def proxy(url: str, ref: str = "", request: Request = None):
    """Stream video with proper Referer. Bypasses CORS + hotlink. Supports Range (seeking)."""
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

    passthrough = {k: v for k, v in upstream.headers.items()
                   if k.lower() in ("content-type", "content-length", "content-range", "accept-ranges")}
    passthrough["Access-Control-Allow-Origin"] = "*"
    if "content-type" not in {k.lower() for k in passthrough}:
        passthrough["Content-Type"] = "video/mp4"
    return StreamingResponse(gen(), status_code=upstream.status_code, headers=passthrough)


# ─── Frontend ───

INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AllManga — Anime Player</title>
<script src="https://cdn.jsdelivr.net/npm/hls.js@1.5"></script>
<style>
:root{--bg:#0a0a0f;--surface:#141420;--surface2:#1c1c2e;--border:#2a2a40;--accent:#7c3aed;--accent-hover:#6d28d9;--green:#22c55e;--red:#ef4444;--text:#e2e2f0;--text-dim:#8888aa}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--text);min-height:100vh}
.header{background:var(--surface);border-bottom:1px solid var(--border);padding:12px 20px;display:flex;align-items:center;gap:16px;position:sticky;top:0;z-index:100}
.header h1{font-size:20px;font-weight:700;background:linear-gradient(135deg,#7c3aed,#a78bfa);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.header .sub{font-size:11px;color:var(--text-dim)}
.search-row{padding:16px 20px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
input,select{background:var(--surface);border:1px solid var(--border);color:var(--text);padding:10px 14px;border-radius:8px;font-size:14px;outline:none;transition:border-color .2s}
input:focus{border-color:var(--accent)}input::placeholder{color:var(--text-dim)}
#q{width:240px}#showId{width:200px}#ep{width:64px}select{cursor:pointer}
.btn{background:var(--accent);color:#fff;border:none;padding:10px 18px;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer;transition:background .2s,transform .1s}
.btn:hover{background:var(--accent-hover)}.btn:active{transform:scale(.97)}.btn:disabled{opacity:.5;cursor:not-allowed}
.main{display:flex;gap:0;max-width:1400px;margin:0 auto;padding:0 20px 40px}
.player-col{flex:1;min-width:0}
.video-wrap{position:relative;background:#000;border-radius:12px;overflow:hidden;aspect-ratio:16/9;box-shadow:0 8px 32px rgba(0,0,0,.5)}
#player{width:100%;height:100%;display:block;object-fit:contain;background:#000}
.status-bar{margin-top:12px;padding:10px 14px;background:var(--surface);border:1px solid var(--border);border-radius:8px;font-size:13px;color:var(--text-dim);display:flex;align-items:center;gap:8px;min-height:40px}
.sdot{width:8px;height:8px;border-radius:50%;background:var(--text-dim);flex-shrink:0}
.sdot.loading{background:#f59e0b;animation:pulse 1s infinite}.sdot.ok{background:var(--green)}.sdot.err{background:var(--red)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.servers{margin-top:12px;display:flex;flex-wrap:wrap;gap:8px}
.srv-btn{background:var(--surface2);border:1px solid var(--border);color:var(--text);padding:8px 14px;border-radius:8px;font-size:13px;cursor:pointer;transition:all .2s;display:flex;align-items:center;gap:6px}
.srv-btn:hover{border-color:var(--accent);background:var(--surface)}.srv-btn.active{background:var(--accent);border-color:var(--accent);color:#fff}
.srv-btn .tag{font-size:10px;padding:2px 5px;border-radius:4px;background:rgba(255,255,255,.15);font-weight:600}.srv-btn.active .tag{background:rgba(0,0,0,.25)}
.srv-btn.unavail{opacity:.4;cursor:not-allowed}.srv-btn.unavail:hover{border-color:var(--border)}
.sidebar{width:300px;flex-shrink:0;margin-left:20px;display:flex;flex-direction:column;gap:12px}
.results-panel,.ep-panel{background:var(--surface);border:1px solid var(--border);border-radius:12px;overflow:hidden}
.results-panel h3,.ep-panel h3{padding:12px 14px;font-size:13px;color:var(--text-dim);border-bottom:1px solid var(--border);font-weight:600}
.result-item{padding:10px 14px;border-bottom:1px solid var(--border);cursor:pointer;transition:background .15s;font-size:13px;display:flex;justify-content:space-between;align-items:center;gap:8px}
.result-item:hover{background:var(--surface2)}.result-item:last-child{border-bottom:none}
.result-item .name{font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.result-item .id{font-size:10px;color:var(--text-dim);font-family:monospace;flex-shrink:0}
.ep-list{max-height:300px;overflow-y:auto}
.ep-item{padding:8px 14px;border-bottom:1px solid var(--border);cursor:pointer;font-size:13px;transition:background .15s;display:flex;align-items:center;gap:8px}
.ep-item:hover{background:var(--surface2)}.ep-item.active{background:rgba(124,58,237,.15);border-left:3px solid var(--accent)}
.ep-num{font-weight:600;min-width:28px}
.raw-toggle{margin-top:12px;background:none;border:1px solid var(--border);color:var(--text-dim);padding:8px 14px;border-radius:8px;font-size:12px;cursor:pointer;width:100%;text-align:left}
.raw-toggle:hover{color:var(--text);border-color:var(--text-dim)}
#raw-json{display:none;margin-top:8px;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:12px;font-size:11px;font-family:'SF Mono','Fira Code',monospace;color:var(--text-dim);max-height:400px;overflow:auto;white-space:pre-wrap;word-break:break-all}
</style>
</head>
<body>
<div class="header">
  <h1>AllManga</h1>
  <div class="sub">Anime Streaming Scraper</div>
</div>
<div class="search-row">
  <input id="q" placeholder="Search anime..." onkeydown="if(event.key==='Enter')search()">
  <select id="mode"><option value="sub">SUB</option><option value="dub">DUB</option></select>
  <button class="btn" onclick="search()">Search</button>
  <input id="showId" placeholder="Show ID (e.g. 6uHjY9KytQFCE4cvJ)" onkeydown="if(event.key==='Enter')loadEps()">
  <input id="ep" placeholder="Ep" value="1" onkeydown="if(event.key==='Enter')loadSources()">
  <button class="btn" onclick="loadSources()">Load</button>
</div>
<div class="main">
  <div class="player-col">
    <div class="video-wrap"><video id="player" controls playsinline></video></div>
    <div class="status-bar"><div class="sdot" id="sdot"></div><span id="status">Ready</span></div>
    <div class="servers" id="servers"></div>
    <button class="raw-toggle" onclick="toggleRaw()">Toggle Raw JSON</button>
    <pre id="raw-json"></pre>
  </div>
  <div class="sidebar">
    <div class="results-panel">
      <h3>Search Results</h3>
      <div id="results"></div>
    </div>
    <div class="ep-panel">
      <h3>Episodes</h3>
      <div class="ep-list" id="episodes"></div>
    </div>
  </div>
</div>
<script>
const $=id=>document.getElementById(id);
let currentServer=null;

function setStatus(text,type){
  $('status').textContent=text;
  const d=$('sdot');
  d.className='sdot '+(type||'');
}

async function search(){
  const q=$('q').value.trim();
  if(!q)return;
  setStatus('Searching...','loading');
  try{
    const r=await fetch(`/search?q=${encodeURIComponent(q)}&mode=${$('mode').value}&detailed=true`);
    const d=await r.json();
    const edges=(d?.shows?.edges)||[];
    $('results').innerHTML=edges.map(e=>`
      <div class="result-item" onclick="pickShow('${e._id}','${(e.name||'').replace(/'/g,"\\'")}')">
        <span class="name">${e.name||e._id}</span>
        <span class="id">${e._id}</span>
      </div>`).join('');
    setStatus(`Found ${edges.length} results`,'ok');
  }catch(e){setStatus('Search failed: '+e,'err')}
}

function pickShow(id,name){
  $('showId').value=id;
  loadEps();
}

async function loadEps(){
  const id=$('showId').value.trim();
  if(!id)return;
  setStatus('Loading episodes...','loading');
  try{
    const r=await fetch(`/episodes?showId=${encodeURIComponent(id)}`);
    const d=await r.json();
    const detail=(d?.availableEpisodesDetail)||{};
    const modes=Object.keys(detail)||[];
    let html='';
    for(const mode of modes){
      (detail[mode]||[]).forEach(ep=>{
        html+=`<div class="ep-item" onclick="playEp('${ep}','${mode}')">
          <span class="ep-num">Ep ${ep}</span>
          <span style="font-size:10px;color:var(--text-dim);text-transform:uppercase">${mode}</span>
        </div>`;
      });
    }
    $('episodes').innerHTML=html;
    setStatus('Episodes loaded','ok');
  }catch(e){setStatus('Failed: '+e,'err')}
}

function playEp(ep,mode){
  $('ep').value=ep;
  $('mode').value=mode;
  loadSources();
}

async function loadSources(){
  const showId=$('showId').value.trim();
  const ep=$('ep').value.trim();
  const mode=$('mode').value;
  if(!showId||!ep)return;
  setStatus('Loading sources...','loading');
  $('servers').innerHTML='';
  $('raw-json').style.display='none';
  currentServer=null;

  try{
    const r=await fetch(`/sources?showId=${encodeURIComponent(showId)}&ep=${encodeURIComponent(ep)}&mode=${mode}`);
    const d=await r.json();
    $('raw-json').textContent=JSON.stringify(d,null,2);

    const servers=d.servers||[];
    if(!servers.length){setStatus('No sources found','err');return}

    $('servers').innerHTML=servers.map((s,i)=>{
      const hasLinks=(s.links||[]).length>0;
      const isIframe=s.links&&s.links[0]&&s.links[0].format==='iframe';
      const unavail=!hasLinks&&!isIframe;
      return `<button class="srv-btn ${unavail?'unavail':''}" onclick="pickServer(${i})" id="srv${i}">
        <span>${s.server}</span>
        <span class="tag">${s.type||''}</span>
        ${hasLinks?'<span class="tag" style="background:rgba(34,197,94,.2)">'+(s.links.length)+'</span>':''}
        ${isIframe?'<span class="tag" style="background:rgba(245,158,11,.2)">EMB</span>':''}
      </button>`;
    }).join('');

    // Auto-pick first available
    const first=servers.findIndex(s=>(s.links||[]).length>0);
    if(first>=0)pickServer(first);
    setStatus(`${servers.length} servers`,'ok');
  }catch(e){setStatus('Failed: '+e,'err')}
}

function pickServer(idx){
  document.querySelectorAll('.srv-btn').forEach(b=>b.classList.remove('active'));
  $('srv'+idx)?.classList.add('active');
  // This function needs access to the data - we'll use a global
  playServer(window._lastServers?.[idx]);
}

window._lastServers=null;

async function playServer(server){
  if(!server||!server.links||!server.links.length)return;
  const link=server.links[0];
  const video=$('player');
  currentServer=server;

  // If it's an iframe embed, load it in an iframe instead
  if(link.format==='iframe'){
    const wrap=document.querySelector('.video-wrap');
    wrap.innerHTML=`<iframe src="${link.url}" style="width:100%;height:100%;border:none" allowfullscreen allow="autoplay; encrypted-media"></iframe>`;
    setStatus(`${server.server} (embedded)`,'ok');
    return;
  }

  // Restore video element if needed
  const wrap=document.querySelector('.video-wrap');
  if(!wrap.querySelector('video')){
    wrap.innerHTML='<video id="player" controls playsinline style="width:100%;height:100%;display:block;object-fit:contain;background:#000"></video>';
  }

  const v=$('player');
  const url=link.proxy||link.url;
  const isHls=url.endsWith('.m3u8')||link.format==='hls';

  setStatus(`Loading ${server.server}...`,'loading');

  if(isHls&&Hls.isSupported()){
    const hls=new Hls;
    hls.loadSource(url);
    hls.attachMedia(v);
    hls.on(Hls.Events.MANIFEST_PARSED,()=>{v.play();setStatus(server.server+' (HLS)','ok')});
    hls.on(Hls.Events.ERROR,(e,d)=>{if(d.fatal)setStatus('HLS error','err')});
  }else if(isHls&&v.canPlayType('application/vnd.apple.mpegurl')){
    v.src=url;
    v.play().catch(()=>{});
    setStatus(server.server+' (HLS native)','ok');
  }else{
    v.src=url;
    v.load();
    v.play().catch(()=>{});
    setStatus(server.server+' ('+link.format+')','ok');
  }

  // Add quality links
  if(server.links.length>1){
    server.links.forEach((l,i)=>{
      const existing=document.getElementById('srv'+(document.querySelectorAll('.srv-btn').length-1));
    });
  }
}

// Override loadSources to store servers
const origLoadSources=window.loadSources;
window.loadSources=async function(){
  const showId=$('showId').value.trim();
  const ep=$('ep').value.trim();
  const mode=$('mode').value;
  if(!showId||!ep)return;
  setStatus('Loading sources...','loading');
  $('servers').innerHTML='';
  $('raw-json').style.display='none';

  try{
    const r=await fetch(`/sources?showId=${encodeURIComponent(showId)}&ep=${encodeURIComponent(ep)}&mode=${mode}`);
    const d=await r.json();
    window._lastServers=d.servers;
    $('raw-json').textContent=JSON.stringify(d,null,2);
    const servers=d.servers||[];
    if(!servers.length){setStatus('No sources found','err');return}
    $('servers').innerHTML=servers.map((s,i)=>{
      const hasLinks=(s.links||[]).length>0;
      const isIframe=s.links&&s.links[0]&&s.links[0].format==='iframe';
      const unavail=!hasLinks&&!isIframe;
      return `<button class="srv-btn ${unavail?'unavail':''}" onclick="playServer(window._lastServers[${i}])" id="srv${i}">
        <span>${s.server}</span>
        <span class="tag">${s.type||''}</span>
        ${hasLinks?'<span class="tag" style="background:rgba(34,197,94,.2)">'+s.links.length+'</span>':''}
        ${isIframe?'<span class="tag" style="background:rgba(245,158,11,.2)">EMB</span>':''}
      </button>`;
    }).join('');
    const first=servers.findIndex(s=>(s.links||[]).length>0);
    if(first>=0)playServer(servers[first]);
    setStatus(`${servers.length} servers`,'ok');
  }catch(e){setStatus('Failed: '+e,'err')}
}

function toggleRaw(){
  const el=$('raw-json');
  el.style.display=el.style.display==='none'?'block':'none';
}
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return INDEX_HTML
