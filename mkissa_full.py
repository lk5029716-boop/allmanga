"""
mkissa.to / AllAnime real video scraper — backend + test frontend in ONE file.

Run:
    pip install fastapi uvicorn httpx cryptography
    uvicorn mkissa_full:app --host 0.0.0.0 --port 8000

Open in browser:
    http://localhost:8000/                 -> test player UI
    http://localhost:8000/search?q=naruto
    http://localhost:8000/sources?showId=<id>&ep=<n>&mode=sub
    http://localhost:8000/proxy?url=<encoded>&ref=<encoded>
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
    pk = re.search(r"}\s*\(\s*'(.+?)'\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*'([^']*)'\.split", text, re.DOTALL)
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
    cands = [u for u in re.findall(r'(https?://[^\"\'\s\\]+\.mp4[^\"\'\s\\]*)', body)
             if "/d/" in u or "video.mp4" in u]
    return [{"quality":"auto","url":cands[0],"format":"mp4","referer":"https://mp4upload.com/"}] if cands else []


async def resolve_filemoon(client, url):
    text = await http_get(client, url, REF)
    body = deobf_packed(text) + "\n" + text
    urls = re.findall(r'(https?://[^\"\'\s\\]+\.m3u8[^\"\'\s\\]*)', body)
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


app = FastAPI(title="AllManga scraper")
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


INDEX_HTML = """<!doctype html>
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
#q{width:260px}#showId{width:220px}#ep{width:70px}select{cursor:pointer}
button{background:var(--accent);color:#fff;border:none;padding:10px 18px;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer;transition:background .2s,transform .1s}
button:hover{background:var(--accent-hover)}button:active{transform:scale(.97)}button:disabled{opacity:.5;cursor:not-allowed}
.main{display:flex;gap:0;max-width:1400px;margin:0 auto;padding:0 20px 40px}
.player-col{flex:1;min-width:0}
.video-wrap{position:relative;background:#000;border-radius:12px;overflow:hidden;aspect-ratio:16/9;box-shadow:0 8px 32px rgba(0,0,0,.5)}
#player{width:100%;height:100%;display:block;object-fit:contain;background:#000}
.status-bar{margin-top:12px;padding:10px 14px;background:var(--surface);border:1px solid var(--border);border-radius:8px;font-size:13px;color:var(--text-dim);display:flex;align-items:center;gap:8px;min-height:40px}
.status-dot{width:8px;height:8px;border-radius:50%;background:var(--text-dim);flex-shrink:0}
.status-dot.loading{background:#f59e0b;animation:pulse 1s infinite}.status-dot.ok{background:var(--green)}.status-dot.err{background:var(--red)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.servers{margin-top:12px;display:flex;flex-wrap:wrap;gap:8px}
.srv-btn{background:var(--surface2);border:1px solid var(--border);color:var(--text);padding:8px 14px;border-radius:8px;font-size:13px;cursor:pointer;transition:all .2s;display:flex;align-items:center;gap:6px}
.srv-btn:hover{border-color:var(--accent);background:var(--surface)}.srv-btn.active{background:var(--accent);border-color:var(--accent);color:#fff}
.srv-btn .tag{font-size:10px;padding:2px 5px;border-radius:4px;background:rgba(255,255,255,.15);font-weight:600}.srv-btn.active .tag{background:rgba(0,0,0,.25)}
.srv-btn.unavail{opacity:.4;cursor:not-allowed}.srv-btn.unavail:hover{border-color:var(--border)}
.sidebar{width:300px;flex-shrink:0;margin-left:20px;display:flex;flex-direction:column;gap:12px}
.results-panel,.ep-panel{background:var(--surface);border:1px solid var(--border);border-radius:12px;overflow:hidden}
.results-panel h3,.ep-panel h3{padding:12px 14px;font-size:13px;color:var(--text-dim);border-bottom:1px solid var(--border);font-weight:600}
.result-item{padding:10px 14px;border-bottom:1px solid var(--border);cursor:pointer;transition:background .15s;font-size:13px;display:flex;justify-content:space-between;align-items:center}
.result-item:hover{background:var(--surface2)}.result-item:last-child{border-bottom:none}
.result-item .name{font-weight:500}.result-item .id{font-size:11px;color:var(--text-dim);font-family:monospace}
.ep-list{max-height:300px;overflow-y:auto}
.ep-item{padding:8px 14px;border-bottom:1px solid var(--border);cursor:pointer;font-size:13px;transition:background .15s;display:flex;align-items:center;gap:8px}
.ep-item:hover{background:var(--surface2)}.ep-item.active{background:rgba(124,58,237,.15);border-left:3px solid var(--accent)}
.ep-num{font-weight:600;min-width:28px}.ep-title{color:var(--text-dim);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.raw-toggle{margin-top:12px;background:none;border:1px solid var(--border);color:var(--text-dim);padding:8px 14px;border-radius:8px;font-size:12px;cursor:pointer;width:100%;text-align:left}
.raw-toggle:hover{color:var(--text);border-color:var(--text-dim)}
#raw-json{display:none;margin-top:8px;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:12px;font-size:11px;font-family:'SF Mono','Fira Code',monospace;color:var(--text-dim);max-height:300px;overflow:auto;white-space:pre-wrap;word-break:break-all}
@media(max-width:900px){.main{flex-direction:column;padding:0 12px 40px}.sidebar{width:100%;margin-left:0;margin-top:16px}#q{width:100%}}
::-webkit-scrollbar{width:6px}::-webkit-scrollbar-track{background:0 0}::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}
</style>
</head>
<body>
<div class="header"><h1>AllManga</h1><div class="sub">Anime streaming — direct video URLs, no iframes</div></div>
<div class="search-row">
<input id="q" placeholder="Search anime…" value="iruma">
<select id="mode"><option>sub</option><option>dub</option></select>
<button onclick="doSearch()" id="searchBtn">Search</button>
<span style="flex:1"></span>
<input id="showId" placeholder="showId" value="6uHjY9KytQFCE4cvJ">
<input id="ep" placeholder="ep" value="9" type="number" min="1">
<button onclick="loadSources()" id="srcBtn">▶ Load</button>
</div>
<div class="main">
<div class="player-col">
<div class="video-wrap"><video id="player" controls playsinline></video></div>
<div class="status-bar" id="status"><div class="status-dot" id="sdot"></div><span id="stext">Click "Load" to fetch sources and play video</span></div>
<div class="servers" id="servers"></div>
<button class="raw-toggle" onclick="toggleRaw()">{ } Raw API Response</button>
<pre id="raw-json"></pre>
</div>
<div class="sidebar">
<div class="results-panel" id="resultsPanel" style="display:none"><h3>Search Results</h3><div id="results"></div></div>
<div class="ep-panel" id="epPanel" style="display:none"><h3>Episodes</h3><div class="ep-list" id="epList"></div></div>
</div>
</div>
<script>
const $=s=>document.getElementById(s),video=$('player');
let currentHls=null,lastSources=null;
function setStatus(text,state='idle'){
  $('stext').textContent=text;
  $('sdot').className='status-dot '+(state==='loading'?'loading':state==='ok'?'ok':state==='err'?'err':'');
}
async function doSearch(){
  const q=$('q').value.trim();if(!q)return;
  $('searchBtn').disabled=true;setStatus('Searching…','loading');
  try{
    const r=await fetch(`/search?q=${encodeURIComponent(q)}&mode=${$('mode').value}`).then(r=>r.json());
    const edges=(r.shows&&r.shows.edges)||[];
    if(!edges.length){setStatus('No results','err');$('resultsPanel').style.display='none'}
    else{
      $('resultsPanel').style.display='';
      $('results').innerHTML=edges.map(s=>`<div class="result-item"onclick="pickShow('${s._id}')"><span class="name">${s.name}</span><span class="id">${s._id}</span></div>`).join('');
      setStatus(`Found ${edges.length} results`,'ok');
    }
  }catch(e){setStatus('Search failed: '+e.message,'err')}
  $('searchBtn').disabled=false;
}
function pickShow(id){$('showId').value=id;loadEpisodes()}
async function loadEpisodes(){
  const showId=$('showId').value.trim();if(!showId)return;
  try{
    const r=await fetch(`/episodes?showId=${encodeURIComponent(showId)}`).then(r=>r.json());
    const episodes=r.episode||r.episodes||[];const list=Array.isArray(episodes)?episodes:[];
    if(list.length){
      $('epPanel').style.display='';
      $('epList').innerHTML=list.map((e,i)=>`<div class="ep-item"onclick="pickEp('${e.episodeString||i+1}')"><span class="ep-num">Ep ${e.episodeString||i+1}</span><span class="ep-title">${e.title||''}</span></div>`).join('');
    }
  }catch(e){}
}
function pickEp(num){$('ep').value=num}
async function loadSources(){
  const showId=$('showId').value.trim(),ep=$('ep').value.trim(),mode=$('mode').value;
  if(!showId||!ep)return setStatus('Enter showId and ep','err');
  $('srcBtn').disabled=true;setStatus('Fetching real video URLs…','loading');
  $('servers').innerHTML='';$('raw-json').style.display='none';
  try{
    const r=await fetch(`/sources?showId=${encodeURIComponent(showId)}&ep=${encodeURIComponent(ep)}&mode=${mode}`).then(r=>{if(!r.ok)throw new Error('HTTP '+r.status);return r.json()});
    lastSources=r;const servers=r.servers||[];const buttons=[];
    for(const srv of servers){
      if(srv.links&&srv.links.length){
        for(const l of srv.links){
          buttons.push(`<button class="srv-btn"onclick='play(${JSON.stringify(l).replace(/'/g,"\\'")})'><span>${srv.server}</span><span class="tag">${l.format.toUpperCase()}</span><span class="tag">${l.quality}</span></button>`);
        }
      }else{
        buttons.push(`<button class="srv-btn unavail"disabled title="${srv.error||srv.note||'unresolved'}"><span>${srv.server}</span><span class="tag">✗</span></button>`);
      }
    }
    $('servers').innerHTML=buttons.join('');
    $('raw-json').textContent=JSON.stringify(r,null,2);
    if(r.best){setStatus(`✅ ${servers.filter(s=>s.links&&s.links.length).length}/${servers.length} servers ready`,'ok');play(r.best)}
    else{setStatus('No direct video URLs found','err')}
  }catch(e){setStatus('Error: '+e.message,'err');console.error(e)}
  $('srcBtn').disabled=false;
}
function play(link){
  const src=link.proxy||link.url;
  document.querySelectorAll('.srv-btn').forEach(b=>b.classList.remove('active'));
  event.target.closest('.srv-btn')?.classList.add('active');
  setStatus(`▶ Playing: ${link.server} ${link.quality}`,'ok');
  if(currentHls){currentHls.destroy();currentHls=null}
  if(link.format==='hls'&&window.Hls&&Hls.isSupported()){
    currentHls=new Hls({enableWorker:true,lowLatencyMode:true});
    currentHls.loadSource(src);currentHls.attachMedia(video);
    currentHls.on(Hls.Events.MANIFEST_PARSED,()=>video.play().catch(()=>{}));
    currentHls.on(Hls.Events.ERROR,(ev,data)=>{if(data.fatal){setStatus('HLS error — try another server','err')}});
  }else{video.src=src;video.play().catch(()=>setStatus('Ready — press play','ok'))}
}
function toggleRaw(){const el=$('raw-json');el.style.display=el.style.display==='none'?'block':'none'}
$('q').addEventListener('keydown',e=>{if(e.key==='Enter')doSearch()});
</script>
</body>
html"""

@app.get("/", response_class=HTMLResponse)
async def index():
    return INDEX_HTML
