"""
AllManga — Anime Streaming Scraper API

HOW IT WORKS (reverse-engineered from mkissa.to / cdn.allanime.day):
1. Full GraphQL queries sent to https://api.allanime.day/api (NOT persisted queries)
2. Episode sourceUrls returned as AES-256-GCM encrypted blob (_m:"b7", field "tobeparsed")
3. Per-server resolvers extract direct video URLs (mp4/m3u8) from iframes
4. Proxy endpoint streams video with proper Referer to bypass hotlink/CORS

Endpoints:
  GET /search?q=naruto&mode=sub       — search anime
  GET /episodes?showId=xxx             — episode list
  GET /sources?showId=xxx&ep=1&mode=sub — real direct video URLs
  GET /proxy?url=xxx&ref=xxx           — stream video through proxy
"""

import base64, hashlib, html as ihtml, json, re, urllib.parse
import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

API_URL = "https://api.allanime.day/api"
REFERER = "https://allanime.to/"
ORIGIN  = "https://allanime.to"
UA      = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"

# ─── GraphQL query strings (extracted from mkissa.to SvelteKit JS bundle) ───

FRAGMENT_SHOW = """
_id
name
nameOnlyString
availableEpisodesDetail
thumbnail
type
isAdult
altNames
"""

FRAGMENT_SHOW_DETAILED = """
_id
name
nameOnlyString
availableEpisodesDetail
thumbnail
type
isAdult
altNames
description
broadcastInterval
banner
characters
countryOfOrigin
genres
tags
airedEnd
rating
status
"""

QUERY_SEARCH = """
query(
  $search: SearchInput
  $limit: Int
  $page: Int
  $translationType: VaildTranslationTypeEnumType
  $countryOrigin: VaildCountryOriginEnumType
) {
  shows(
    search: $search
    limit: $limit
    page: $page
    translationType: $translationType
    countryOrigin: $countryOrigin
  ) {
    pageInfo { total }
    edges {
""" + FRAGMENT_SHOW + """
    }
  }
}
"""

QUERY_SEARCH_DETAILED = """
query(
  $search: SearchInput
  $limit: Int
  $page: Int
  $translationType: VaildTranslationTypeEnumType
  $countryOrigin: VaildCountryOriginEnumType
) {
  shows(
    search: $search
    limit: $limit
    page: $page
    translationType: $translationType
    countryOrigin: $countryOrigin
  ) {
    pageInfo { total }
    edges {
""" + FRAGMENT_SHOW_DETAILED + """
    }
  }
}
"""

QUERY_EPISODE = """
query(
  $showId: String!
  $translationType: VaildTranslationTypeEnumType!
  $episodeString: String!
) {
  episode(
    showId: $showId
    translationType: $translationType
    episodeString: $episodeString
  ) {
    episodeString
    uploadDate
    sourceUrls
    thumbnail
    notes
    show {
""" + FRAGMENT_SHOW_DETAILED + """
    }
    episodeInfo {
      notes
      vidInforssub
      vidInforsdub
      description
    }
  }
}
"""

QUERY_SHOW = """
query(
  $_id: String!
) {
  show(
    _id: $_id
  ) {
""" + FRAGMENT_SHOW_DETAILED + """
    availableEpisodesDetail
  }
}
"""


# ─── AES-256-GCM decryption ───

def decrypt_b7(blob_b64: str) -> str:
    """Decrypt the _m:'b7' tobeparsed blob.
    Reversed from cdn.allanime.day/all/mk/_app/immutable/chunks/CDQBuZuQ.js
    Secret: Xot36i3lK3  |  Salt: :v1  |  Key = SHA256(secret + salt)
    Layout: [version(1B) | iv(12B) | ciphertext+tag]
    """
    raw = base64.b64decode(blob_b64)
    assert raw[0] == 1, f"unexpected version {raw[0]}"
    iv, ct = raw[1:13], raw[13:]
    key = hashlib.sha256(b"Xot36i3lK3:v1").digest()
    return AESGCM(key).decrypt(iv, ct, None).decode()


async def gql(client: httpx.AsyncClient, query: str, variables: dict) -> dict:
    r = await client.post(
        API_URL,
        json={"query": query, "variables": variables},
        headers={
            "Referer": REFERER,
            "Origin": ORIGIN,
            "User-Agent": UA,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
        },
        timeout=20,
    )
    r.raise_for_status()
    resp = r.json()
    # Handle Cloudflare captcha challenge on episode endpoint — fall back to persisted GET
    errors = resp.get("errors") or []
    if any(e.get("message") == "NEED_CAPTCHA" for e in errors):
        raise HTTPException(403, "captcha_required")
    data = resp.get("data") or {}
    # Some responses wrap the real data in an encrypted blob
    if "tobeparsed" in data:
        data = json.loads(decrypt_b7(data["tobeparsed"]))
    return data


async def gql_persisted_get(client: httpx.AsyncClient, variables: dict, sha: str) -> dict:
    """Fallback: GET with persisted query hash (bypasses Cloudflare). Uses mkissa.to referer."""
    params = {
        "variables": json.dumps(variables, separators=(",", ":")),
        "extensions": json.dumps({"persistedQuery": {"version": 1, "sha256Hash": sha}}, separators=(",", ":")),
    }
    r = await client.get(
        API_URL,
        params=params,
        headers={
            "Referer": "https://mkissa.to/",
            "Origin": "https://mkissa.to",
            "User-Agent": UA,
        },
        timeout=20,
    )
    r.raise_for_status()
    data = r.json().get("data") or {}
    if "tobeparsed" in data:
        data = json.loads(decrypt_b7(data["tobeparsed"]))
    return data


# ─── Server resolvers ───

def deobf_packed(text: str) -> str:
    """P.A.C.K.E.R deobfuscator — extracts eval payload from packed JS."""
    pk = re.search(r"}\s*\(\s*'(.+?)'\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*'([^']*)'\.split", text, re.DOTALL)
    if not pk:
        return text
    payload, radix, _count, syms = pk.group(1), int(pk.group(2)), int(pk.group(3)), pk.group(4).split("|")
    def repl(m):
        w = m.group(0)
        try:
            i = int(w, radix)
        except ValueError:
            return w
        return syms[i] if i < len(syms) and syms[i] else w
    return re.sub(r"\b[0-9a-zA-Z]+\b", repl, payload)


async def http_get(client, url, referer=None):
    h = {"User-Agent": UA}
    if referer:
        h["Referer"] = referer
    r = await client.get(url, headers=h, timeout=20, follow_redirects=True)
    return r.text


async def resolve_mp4upload(client, url):
    text = await http_get(client, url, "https://mp4upload.com/")
    body = deobf_packed(text) + "\n" + text
    cands = [u for u in re.findall(r'(https?://[^\"\'\s\\]+\.mp4[^\"\'\s\\]*)', body)
             if "/d/" in u or "video.mp4" in u]
    return [{"quality": "auto", "url": cands[0], "format": "mp4", "referer": "https://mp4upload.com/"}] if cands else []


async def resolve_filemoon(client, url):
    text = await http_get(client, url, REFERER)
    body = deobf_packed(text) + "\n" + text
    urls = re.findall(r'(https?://[^\"\'\s\\]+\.m3u8[^\"\'\s\\]*)', body)
    return [{"quality": "auto", "url": urls[0], "format": "hls", "referer": url}] if urls else []


async def resolve_okru(client, url):
    text = await http_get(client, url)
    m = re.search(r'data-options="([^"]+)"', text)
    if not m:
        return []
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


async def resolve_allanime_internal(client, source_url):
    """Resolve AllAnime internal (--xxx) source URLs via XOR-0x37 hex decode."""
    sid = source_url.lstrip("-")
    pairs = {
        "01": "9", "02": "0", "03": "1", "04": "2", "05": "3", "06": "4", "07": "5", "08": "6", "09": "7", "0a": "8",
        "0b": ".", "0c": "<", "0d": ">", "0e": "/", "0f": "?", "00": ":", "5c": "/", "79": "H", "7a": "I", "7b": "J",
    }
    out_chars = []
    for i in range(0, len(sid), 2):
        seg = sid[i:i+2]
        if len(seg) < 2:
            break
        out_chars.append(pairs.get(seg, chr(int(seg, 16) ^ 0x37)))
    decoded = "".join(out_chars)
    path = decoded.replace("clock", "clock.json") if "clock" in decoded else decoded
    r = await client.get(
        "https://allanime.day" + path,
        headers={"Referer": "https://allanime.day/", "User-Agent": UA},
        timeout=20,
    )
    try:
        return [
            {
                "quality": link.get("resolutionStr", "auto"),
                "url": link["link"],
                "format": "hls" if link["link"].endswith(".m3u8") else "mp4",
                "referer": "https://allanime.day/",
            }
            for link in r.json().get("links", [])
        ]
    except Exception:
        return []


async def resolve_one(client, src):
    name = src.get("sourceName")
    url = src.get("sourceUrl", "")
    typ = src.get("type")
    base = {"server": name, "type": typ, "embed": url}
    try:
        if typ == "player":
            return {**base, "links": [{"quality": "auto", "url": url, "format": src.get("fileExtenstion", "mp4"), "referer": REFERER}]}
        if url.startswith("--"):
            return {**base, "links": await resolve_allanime_internal(client, url)}
        if "mp4upload" in url:
            return {**base, "links": await resolve_mp4upload(client, url)}
        if "ok.ru" in url:
            return {**base, "links": await resolve_okru(client, url)}
        if any(d in url for d in ("filemoon", "bysekoze", "kerapoxy")) or name in ("Fm-Hls", "Filemoon"):
            return {**base, "links": await resolve_filemoon(client, url)}
        if "filemoon" in url or "fmhd" in url:
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
        query = QUERY_SEARCH_DETAILED if detailed else QUERY_SEARCH
        return await gql(c, query, {
            "search": {"allowAdult": False, "allowUnknown": False, "query": q},
            "limit": limit,
            "page": page,
            "translationType": mode,
            "countryOrigin": "ALL",
        })


@app.get("/episodes")
async def episodes(showId: str):
    async with httpx.AsyncClient() as c:
        return await gql(c, QUERY_SHOW, {"_id": showId})


@app.get("/sources")
async def sources(showId: str, ep: str, mode: str = "sub"):
    EPISODE_SHA = "d405d0edd690624b66baba3068e0edc3ac90f1597d898a1ec8db4e5c43c00fec"
    async with httpx.AsyncClient(timeout=60) as c:
        # Try POST first, fall back to persisted GET on captcha
        try:
            data = await gql(c, QUERY_EPISODE, {
                "showId": showId, "translationType": mode, "episodeString": ep,
            })
        except HTTPException:
            data = await gql_persisted_get(c, {
                "showId": showId, "translationType": mode, "episodeString": ep,
            }, EPISODE_SHA)
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
        "showId": showId,
        "episode": ep,
        "mode": mode,
        "episodeData": {
            "notes": episode_data.get("notes"),
            "thumbnail": episode_data.get("thumbnail"),
            "uploadDate": episode_data.get("uploadDate"),
        },
        "show": episode_data.get("show"),
        "servers": resolved,
        "best": direct[0] if direct else None,
        "total": len(direct),
    }


@app.get("/proxy")
async def proxy(url: str, ref: str = "", request: Request = None):
    """Stream video with proper Referer. Bypasses CORS + hotlink. Supports Range (seeking)."""
    headers = {"User-Agent": UA}
    if ref:
        headers["Referer"] = ref
    rng = request.headers.get("range") if request else None
    if rng:
        headers["Range"] = rng

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

    passthrough = {
        k: v for k, v in upstream.headers.items()
        if k.lower() in ("content-type", "content-length", "content-range", "accept-ranges", "cache-control")
    }
    passthrough["Access-Control-Allow-Origin"] = "*"
    if "content-type" not in {k.lower() for k in passthrough}:
        passthrough["Content-Type"] = "video/mp4"
    return StreamingResponse(gen(), status_code=upstream.status_code, headers=passthrough)


# ─── Frontend (polished dark-themed player UI) ───

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
.results-panel,.ep-panel,.info-panel{background:var(--surface);border:1px solid var(--border);border-radius:12px;overflow:hidden}
.results-panel h3,.ep-panel h3,.info-panel h3{padding:12px 14px;font-size:13px;color:var(--text-dim);border-bottom:1px solid var(--border);font-weight:600}
.result-item{padding:10px 14px;border-bottom:1px solid var(--border);cursor:pointer;transition:background .15s;font-size:13px;display:flex;justify-content:space-between;align-items:center;gap:8px}
.result-item:hover{background:var(--surface2)}.result-item:last-child{border-bottom:none}
.result-item .name{font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.result-item .id{font-size:10px;color:var(--text-dim);font-family:monospace;flex-shrink:0}
.ep-list{max-height:300px;overflow-y:auto}
.ep-item{padding:8px 14px;border-bottom:1px solid var(--border);cursor:pointer;font-size:13px;transition:background .15s;display:flex;align-items:center;gap:8px}
.ep-item:hover{background:var(--surface2)}.ep-item.active{background:rgba(124,58,237,.15);border-left:3px solid var(--accent)}
.ep-num{font-weight:600;min-width:28px}.ep-title{color:var(--text-dim);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.show-thumb{width:100%;aspect-ratio:3/4;object-fit:cover;background:var(--surface2)}
.show-info{padding:12px 14px;font-size:13px}
.show-info .show-name{font-size:16px;font-weight:700;margin-bottom:4px}
.show-info .show-alt{color:var(--text-dim);font-size:12px;margin-bottom:8px}
.show-info .show-desc{color:var(--text-dim);font-size:12px;line-height:1.5;max-height:80px;overflow:auto}
.raw-toggle{margin-top:12px;background:none;border:1px solid var(--border);color:var(--text-dim);padding:8px 14px;border-radius:8px;font-size:12px;cursor:pointer;width:100%;text-align:left}
.raw-toggle:hover{color:var(--text);border-color:var(--text-dim)}
#raw-json{display:none;margin-top:8px;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:12px;font-size:11px;font-family:'SF Mono','Fira Code',monospace;color:var(--text-dim);max-height:400px;overflow:auto;white-space:pre-wrap;word-break:break-all}
.page{padding:20px;text-align:center;color:var(--text-dim)}
.page a{color:var(--accent);margin:0 8px;text-decoration:none;font-weight:600}
@media(max-width:900px){.main{flex-direction:column;padding:0 12px 40px}.sidebar{width:100%;margin-left:0;margin-top:16px}#q{width:100%}}
::-webkit-scrollbar{width:6px}::-webkit-scrollbar-track{background:0 0}::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}
</style>
</head>
<body>
<div class="header">
  <h1>AllManga</h1>
  <div class="sub">Anime streaming — direct video URLs, no iframes</div>
</div>
<div class="search-row">
  <input id="q" placeholder="Search anime…" value="iruma" onkeydown="if(event.key==='Enter')doSearch()">
  <select id="mode"><option>sub</option><option>dub</option></select>
  <select id="page-sel" onchange="doSearch(+this.value)"><option value="1">Page 1</option><option value="2">Page 2</option><option value="3">Page 3</option></select>
  <button class="btn" onclick="doSearch()" id="searchBtn">Search</button>
  <span style="flex:1"></span>
  <input id="showId" placeholder="showId (auto-filled)" value="6uHjY9KytQFCE4cvJ" onkeydown="if(event.key==='Enter')loadSources()">
  <input id="ep" placeholder="Ep" value="9" type="number" min="1" onkeydown="if(event.key==='Enter')loadSources()">
  <button class="btn" onclick="loadSources()" id="srcBtn">▶ Load</button>
</div>
<div class="main">
  <div class="player-col">
    <div class="video-wrap"><video id="player" controls playsinline></video></div>
    <div class="status-bar"><div class="sdot" id="sdot"></div><span id="stext">Search anime → pick show → load episode → click server to play</span></div>
    <div class="servers" id="servers"></div>
    <button class="raw-toggle" onclick="toggleRaw()">{ } Raw API Response</button>
    <pre id="raw-json"></pre>
  </div>
  <div class="sidebar">
    <div class="results-panel" id="resultsPanel" style="display:none"><h3>🔍 Search Results</h3><div id="results"></div></div>
    <div class="info-panel" id="infoPanel" style="display:none"><div id="showInfo"></div></div>
    <div class="ep-panel" id="epPanel" style="display:none"><h3>📋 Episodes</h3><div class="ep-list" id="epList"></div></div>
  </div>
</div>
<script>
const $=s=>document.getElementById(s),video=$('player');
let currentHls=null;

function setStatus(text,state='idle'){
  $('stext').textContent=text;
  $('sdot').className='sdot '+(state==='loading'?'loading':state==='ok'?'ok':state==='err'?'err':'');
}

async function doSearch(page=1){
  const q=$('q').value.trim();if(!q)return;
  $('searchBtn').disabled=true;setStatus('Searching…','loading');
  $('results').innerHTML='';$('resultsPanel').style.display='';$('infoPanel').style.display='none';$('epPanel').style.display='none';
  $('servers').innerHTML='';$('raw-json').style.display='none';
  
  try{
    const r=await fetch(`/search?q=${encodeURIComponent(q)}&mode=${$('mode').value}&page=${page}&detailed=true`).then(r=>{if(!r.ok)throw new Error('HTTP '+r.status);return r.json()});
    const edges=(r.shows&&r.shows.edges)||[];
    const total=r.shows&&r.shows.pageInfo&&r.shows.pageInfo.total;
    
    if(!edges.length){
      $('results').innerHTML='<div class="page">No results found</div>';
      setStatus('No results','err');
    }else{
      // Build pagination
      let pgHtml='';
      if(total){
        const pages=Math.ceil(total/20);
        pgHtml=`<div class="page">Page ${page}/${pages} (${total} results) `;
        if(page>1)pgHtml+=`<a href="#"onclick="doSearch(${page-1});return false">‹ Prev</a> `;
        if(page<pages)pgHtml+=`<a href="#"onclick="doSearch(${page+1});return false">Next ›</a>`;
        pgHtml+='</div>';
      }
      
      $('results').innerHTML=edges.map(s=>`
        <div class="result-item"onclick="pickShow('${s._id}')">
          <span class="name">${s.name||s.nameOnlyString||'Unknown'}</span>
          <span class="id">${s._id.length>20?s._id.substring(0,18)+'…':s._id}</span>
        </div>`).join('')+pgHtml;
      setStatus(`Found ${total||edges.length} anime (page ${page})`,'ok');
      
      // Auto-pick first result
      if(page===1&&edges.length>0){
        pickShow(edges[0]._id);
      }
    }
  }catch(e){setStatus('Search failed: '+e.message,'err');console.error(e)}
  $('searchBtn').disabled=false;
}

async function pickShow(id){
  $('showId').value=id;
  $('infoPanel').style.display='';$('epPanel').style.display='';
  $('showInfo').innerHTML='<div class="page">Loading show info…</div>';
  $('epList').innerHTML='<div class="page">Loading episodes…</div>';
  
  try{
    const r=await fetch(`/episodes?showId=${encodeURIComponent(id)}`).then(r=>{if(!r.ok)throw new Error('HTTP '+r.status);return r.json()});
    const show=r.show||r;
    
    // Show info
    const thumb=show.thumbnail?`<img class="show-thumb"src="${show.thumbnail}"alt="">`:'<div class="show-thumb"></div>';
    const showDesc=show.description?show.description.replace(/<[^>]+>/g,'').substring(0,200):'';
    $('showInfo').innerHTML=`
      ${thumb}
      <div class="show-info">
        <div class="show-name">${show.name||show.nameOnlyString||''}</div>
        ${(show.altNames||[]).slice(0,3).map(n=>`<div class="show-alt">${n}</div>`).join('')}
        ${showDesc?`<div class="show-desc">${showDesc}${show.description&&show.description.length>200?'…':''}</div>`:''}
      </div>`;
    
    // Episodes
    const epDetail=show.availableEpisodesDetail||{};
    const eps=epDetail[$('mode').value]||epDetail.sub||[];
    if(eps.length){
      $('epList').innerHTML=eps.map(n=>`
        <div class="ep-item"onclick="pickEp('${n}')">
          <span class="ep-num">Ep ${n}</span>
        </div>`).join('');
    }else{
      $('epList').innerHTML='<div class="page">No episode list available</div>';
    }
  }catch(e){console.log('show info error',e)}
}

function pickEp(num){$('ep').value=num;loadSources()}

async function loadSources(){
  const showId=$('showId').value.trim(),ep=$('ep').value.trim(),mode=$('mode').value;
  if(!showId||!ep)return setStatus('Need showId + episode number','err');
  $('srcBtn').disabled=true;setStatus('Fetching direct video URLs…','loading');
  $('servers').innerHTML='';video.src='';if(currentHls){currentHls.destroy();currentHls=null}
  $('raw-json').style.display='none';
  
  try{
    const r=await fetch(`/sources?showId=${encodeURIComponent(showId)}&ep=${encodeURIComponent(ep)}&mode=${mode}`).then(r=>{if(!r.ok)throw new Error('HTTP '+r.status);return r.json()});
    const servers=r.servers||[];const buttons=[];
    for(const srv of servers){
      if(srv.links&&srv.links.length){
        for(const l of srv.links){
          const safe=JSON.stringify(l).replace(/'/g,"\\'");
          buttons.push(`<button class="srv-btn"onclick='play(${safe})'><span>${srv.server}</span><span class="tag">${l.format.toUpperCase()}</span><span class="tag">${l.quality}</span></button>`);
        }
      }else{
        buttons.push(`<button class="srv-btn unavail"disabled><span>${srv.server}</span><span class="tag">✗</span></button>`);
      }
    }
    $('servers').innerHTML=buttons.join('');
    $('raw-json').textContent=JSON.stringify(r,null,2);
    if(r.best){
      const ok=servers.filter(s=>s.links&&s.links.length).length;
      setStatus(`✅ ${ok}/${servers.length} servers ready — click to play`,'ok');
      play(r.best);
    }else{
      setStatus('No playable servers found','err');
    }
  }catch(e){setStatus('Error: '+e.message,'err');console.error(e)}
  $('srcBtn').disabled=false;
}

function play(link){
  const src=link.proxy||link.url;
  document.querySelectorAll('.srv-btn').forEach(b=>b.classList.remove('active'));
  event.target.closest('.srv-btn')?.classList.add('active');
  setStatus(`▶ ${link.server} ${link.format.toUpperCase()} ${link.quality}`,'ok');
  if(currentHls){currentHls.destroy();currentHls=null}
  if(link.format==='hls'&&window.Hls&&Hls.isSupported()){
    currentHls=new Hls({enableWorker:true});
    currentHls.loadSource(src);currentHls.attachMedia(video);
    currentHls.on(Hls.Events.MANIFEST_PARSED,()=>video.play().catch(()=>{}));
  }else{video.src=src;video.play().catch(()=>{})}
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
