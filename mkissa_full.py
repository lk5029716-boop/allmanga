"""
AllManga — Anime Streaming Scraper API + Player UI

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

FRAGMENT_SHOW = "_id name nameOnlyString availableEpisodesDetail thumbnail type isAdult altNames"
FRAGMENT_SHOW_DETAILED = FRAGMENT_SHOW + " description broadcastInterval banner characters countryOfOrigin genres tags airedEnd rating status"

QUERY_SEARCH = f"""query($search:SearchInput,$limit:Int,$page:Int,$translationType:VaildTranslationTypeEnumType,$countryOrigin:VaildCountryOriginEnumType){{shows(search:$search,limit:$limit,page:$page,translationType:$translationType,countryOrigin:$countryOrigin){{pageInfo{{total}} edges{{{FRAGMENT_SHOW}}}}}}}"""
QUERY_SEARCH_DETAILED = f"""query($search:SearchInput,$limit:Int,$page:Int,$translationType:VaildTranslationTypeEnumType,$countryOrigin:VaildCountryOriginEnumType){{shows(search:$search,limit:$limit,page:$page,translationType:$translationType,countryOrigin:$countryOrigin){{pageInfo{{total}} edges{{{FRAGMENT_SHOW_DETAILED}}}}}}}"""
QUERY_EPISODE = f"""query($showId:String!,$translationType:VaildTranslationTypeEnumType!,$episodeString:String!){{episode(showId:$showId,translationType:$translationType,episodeString:$episodeString){{episodeString uploadDate sourceUrls thumbnail notes show{{{FRAGMENT_SHOW_DETAILED}}} episodeInfo{{notes vidInforssub vidInforsdub description}}}}}}"""
QUERY_SHOW = f"""query($_id:String!){{show(_id:$_id){{{FRAGMENT_SHOW_DETAILED}}} availableEpisodesDetail}}}}"""


def decrypt_b7(blob_b64):
    raw = base64.b64decode(blob_b64)
    return AESGCM(AES_KEY).decrypt(raw[1:13], raw[13:], None).decode()


async def gql(client, query, variables):
    r = await client.post(API_URL, json={"query": query, "variables": variables},
        headers={"Referer": REFERER, "Origin": ORIGIN, "User-Agent": UA, "Content-Type": "application/json"}, timeout=20)
    r.raise_for_status()
    resp = r.json()
    errors = resp.get("errors") or []
    if any(e.get("message") == "NEED_CAPTCHA" for e in errors):
        raise HTTPException(403, "captcha_required")
    data = resp.get("data") or {}
    if "tobeparsed" in data:
        data = json.loads(decrypt_b7(data["tobeparsed"]))
    return data


async def gql_persisted_get(client, variables, sha):
    params = {"variables": json.dumps(variables, separators=(",", ":")),
              "extensions": json.dumps({"persistedQuery": {"version": 1, "sha256Hash": sha}}, separators=(",", ":"))}
    r = await client.get(API_URL, params=params, headers={"Referer": "https://mkissa.to/", "Origin": "https://mkissa.to", "User-Agent": UA}, timeout=20)
    data = r.json().get("data") or {}
    if "tobeparsed" in data:
        data = json.loads(decrypt_b7(data["tobeparsed"]))
    return data


def deobf_packed(text):
    pk = re.search(r"}\s*\(\s*'(.+?)'\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*'([^']*)'\.split", text, re.DOTALL)
    if not pk: return text
    payload, radix, syms = pk.group(1), int(pk.group(2)), pk.group(4).split("|")
    def repl(m):
        try: i = int(m.group(0), radix)
        except ValueError: return m.group(0)
        return syms[i] if i < len(syms) and syms[i] else m.group(0)
    return re.sub(r"\b[0-9a-zA-Z]+\b", repl, payload)


async def http_get(client, url, referer=None):
    h = {"User-Agent": UA}
    if referer: h["Referer"] = referer
    return (await client.get(url, headers=h, timeout=15, follow_redirects=True)).text


def decrypt_response(text):
    try:
        raw = base64.b64decode(text.strip())
        pt = AESGCM(AES_KEY).decrypt(raw[1:13], raw[13:], None)
        return json.loads(pt.decode())
    except Exception: return None


# ── Server resolvers ──

async def resolve_player(s):
    return [{"quality": "auto", "url": s["sourceUrl"], "format": s.get("fileExtenstion", "mp4")}]

async def resolve_okru(client, url):
    text = await http_get(client, url)
    m = re.search(r'data-options="([^"]+)"', text)
    if not m: return []
    opts = json.loads(ihtml.unescape(m.group(1)))
    fv = opts.get("flashvars", {})
    if "metadata" not in fv: return []
    md = json.loads(fv["metadata"])
    out = [{"quality": v.get("name","auto"), "url": v.get("url",""), "format": "mp4"} for v in md.get("videos",[])]
    if md.get("hlsManifestUrl"):
        out.append({"quality": "auto", "url": md["hlsManifestUrl"], "format": "hls"})
    return out

async def resolve_mp4upload(client, url):
    text = await http_get(client, url, "https://mp4upload.com/")
    body = deobf_packed(text) + "\n" + text
    cands = [u for u in re.findall(r'(https?://[^"\'\s\\]+\.mp4[^"\'\s\\]*)', body) if "/d/" in u or "video.mp4" in u]
    return [{"quality": "auto", "url": cands[0], "format": "mp4"}] if cands else []

async def resolve_vidnest(client, url):
    m = re.search(r'/e/([a-zA-Z0-9_-]+)', url)
    if not m: return []
    fc = m.group(1)
    try:
        r = await client.post("https://vidnest.io/dl",
            data={"op":"embed","file_code":fc,"referer":"","auto":"1"},
            headers={"User-Agent":UA,"Referer":f"https://vidnest.io/e/{fc}","Content-Type":"application/x-www-form-urlencoded"},
            timeout=15, follow_redirects=True)
        mp4s = re.findall(r'(https?://[^"\'\s<>]+\.mp4[^"\'\s<>]*)', r.text)
        seen, out = set(), []
        for u in mp4s:
            if u not in seen:
                seen.add(u)
                q = "1080p" if "_x/" in u else "720p" if "_o/" in u else "auto"
                out.append({"quality": q, "url": u, "format": "mp4"})
        return out
    except Exception: return []

async def resolve_filemoon(client, url):
    text = await http_get(client, url, REFERER)
    body = deobf_packed(text) + "\n" + text
    urls = re.findall(r'(https?://[^"\'\s\\]+\.m3u8[^"\'\s\\]*)', body)
    return [{"quality": "auto", "url": urls[0], "format": "hls"}] if urls else []

async def resolve_bysekoze(client, url):
    m = re.search(r'/e/([a-zA-Z0-9_-]+)', url)
    if not m: return []
    code = m.group(1)
    try:
        r = await client.get(f"https://bysekoze.com/api/videos/{code}/embed/details",
            headers={"User-Agent":UA,"Referer":f"https://bysekoze.com/e/{code}",
                     "X-Embed-Origin":"https://allanime.to","X-Embed-Referer":"https://allanime.to/","X-Embed-Parent":"https://allanime.to"},
            timeout=10)
        if r.status_code == 200:
            embed_url = r.json().get("embed_frame_url", "")
            if embed_url: return [{"quality": "auto", "url": embed_url, "format": "iframe"}]
    except Exception: pass
    return []

async def resolve_unsbio(client, url):
    m = re.search(r'#([a-zA-Z0-9_-]+)', url)
    if not m: return []
    hv = m.group(1)
    try:
        r = await client.get(f"https://allanime.uns.bio/api/v1/info?id={hv}",
            headers={"User-Agent":UA,"Referer":f"https://allanime.uns.bio/#{hv}"}, timeout=10)
        if r.status_code == 200:
            try:
                raw = bytes.fromhex(r.text.strip())
                if raw[0] == 1:
                    pt = AESGCM(AES_KEY).decrypt(raw[1:13], raw[13:], None)
                    links = json.loads(pt.decode()).get("links", [])
                    if links:
                        return [{"quality": l.get("resolutionStr","auto"), "url": l["link"],
                                 "format": "hls" if l["link"].endswith(".m3u8") else "mp4"} for l in links]
            except Exception: pass
    except Exception: pass
    return []

async def resolve_allanime_hex(client, source_url):
    try: raw_bytes = bytes.fromhex(source_url.lstrip("-"))
    except ValueError: return []
    decoded = "".join(chr(b ^ 0x38) for b in raw_bytes)
    path = decoded.replace("?id=", ".json?id=") if "?id=" in decoded else decoded
    urls = [f"https://allanime.day{path}", f"https://allanime.day{decoded}"]
    if "id=" in decoded: urls.append(f"https://allanime.day/apivtwo/clock.json?id={decoded.split('id=')[1]}")
    for url in urls:
        try:
            r = await client.get(url, headers={"User-Agent":UA,"Referer":"https://mkissa.to/"}, timeout=10)
            if r.status_code != 200: continue
            text = r.text.strip()
            try:
                links = json.loads(text).get("links", [])
            except: links = []
            if not links:
                dec = decrypt_response(text)
                links = dec.get("links", []) if dec else []
            if links:
                return [{"quality": l.get("resolutionStr",l.get("label","auto")), "url": l["link"],
                         "format": "hls" if l["link"].endswith(".m3u8") else "mp4"} for l in links]
        except Exception: continue
    return []


async def resolve_one(client, src):
    name, url, typ = src.get("sourceName"), src.get("sourceUrl", ""), src.get("type")
    base = {"server": name, "type": typ, "embed": url}
    try:
        if typ == "player": return {**base, "links": await resolve_player(src)}
        if url.startswith("--"): return {**base, "links": await resolve_allanime_hex(client, url)}
        if "ok.ru" in url: return {**base, "links": await resolve_okru(client, url)}
        if "mp4upload" in url: return {**base, "links": await resolve_mp4upload(client, url)}
        if "vidnest" in url or name == "Vn-Hls": return {**base, "links": await resolve_vidnest(client, url)}
        if "bysekoze" in url or "filemoon" in url or name == "Fm-Hls": return {**base, "links": await resolve_bysekoze(client, url)}
        if "uns.bio" in url or name == "Uni": return {**base, "links": await resolve_unsbio(client, url)}
        if any(d in url for d in ("filemoon","kerapoxy")): return {**base, "links": await resolve_filemoon(client, url)}
        return {**base, "links": []}
    except Exception as e:
        return {**base, "links": [], "error": str(e)}


# ── FastAPI ──

app = FastAPI(title="AllManga API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

EPISODE_SHA = "d405d0edd690624b66baba3068e0edc3ac90f1597d898a1ec8db4e5c43c00fec"

@app.get("/search")
async def search(q: str, mode: str = "sub", limit: int = 20, page: int = 1, detailed: bool = False):
    async with httpx.AsyncClient() as c:
        query = QUERY_SEARCH_DETAILED if detailed else QUERY_SEARCH
        return await gql(c, query, {"search": {"allowAdult": False, "allowUnknown": False, "query": q},
                                     "limit": limit, "page": page, "translationType": mode, "countryOrigin": "ALL"})

@app.get("/episodes")
async def episodes(showId: str):
    async with httpx.AsyncClient() as c:
        return await gql(c, QUERY_SHOW, {"_id": showId})

@app.get("/sources")
async def sources(showId: str, ep: str, mode: str = "sub"):
    async with httpx.AsyncClient(timeout=60) as c:
        try:
            data = await gql(c, QUERY_EPISODE, {"showId": showId, "translationType": mode, "episodeString": ep})
        except HTTPException:
            data = await gql_persisted_get(c, {"showId": showId, "translationType": mode, "episodeString": ep}, EPISODE_SHA)
        ed = data.get("episode") or {}
        srcs = ed.get("sourceUrls", [])
        if not srcs: raise HTTPException(404, "no sources found")
        resolved = [await resolve_one(c, s) for s in sorted(srcs, key=lambda x: x.get("priority", 0), reverse=True)]
    direct = [lnk for r in resolved for lnk in r.get("links", [])]
    for r in resolved:
        for l in r.get("links", []):
            l["proxy"] = "/proxy?url=" + urllib.parse.quote(l["url"], safe="")
            if l.get("referer"): l["proxy"] += "&ref=" + urllib.parse.quote(l["referer"], safe="")
    return {"showId": showId, "episode": ep, "mode": mode,
            "episodeData": {"notes": ed.get("notes"), "thumbnail": ed.get("thumbnail"), "uploadDate": ed.get("uploadDate")},
            "show": ed.get("show"), "servers": resolved, "best": direct[0] if direct else None, "total": len(direct)}

@app.get("/proxy")
async def proxy(url: str, ref: str = "", request: Request = None):
    headers = {"User-Agent": UA}
    if ref: headers["Referer"] = ref
    rng = request.headers.get("range") if request else None
    if rng: headers["Range"] = rng
    cli = httpx.AsyncClient(timeout=None, follow_redirects=True)
    up = await cli.send(cli.build_request("GET", url, headers=headers), stream=True)
    async def gen():
        try:
            async for chunk in up.aiter_raw(): yield chunk
        finally:
            await up.aclose(); await cli.aclose()
    pt = {k: v for k, v in up.headers.items() if k.lower() in ("content-type","content-length","content-range","accept-ranges")}
    pt["Access-Control-Allow-Origin"] = "*"
    if "content-type" not in {k.lower() for k in pt}: pt["Content-Type"] = "video/mp4"
    return StreamingResponse(gen(), status_code=up.status_code, headers=pt)


# ── Frontend ──

@app.get("/", response_class=HTMLResponse)
async def index():
    return """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>AllManga — Anime Player</title><script src="https://cdn.jsdelivr.net/npm/hls.js@1.5"></script>
<style>
:root{--bg:#0a0a0f;--srf:#141420;--srf2:#1c1c2e;--brd:#2a2a40;--acc:#7c3aed;--acc-h:#6d28d9;--grn:#22c55e;--red:#ef4444;--txt:#e2e2f0;--dim:#8888aa}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--txt);min-height:100vh}
.hd{background:var(--srf);border-bottom:1px solid var(--brd);padding:12px 20px;display:flex;align-items:center;gap:16px;position:sticky;top:0;z-index:100}
.hd h1{font-size:20px;font-weight:700;background:linear-gradient(135deg,#7c3aed,#a78bfa);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.hd .sub{font-size:11px;color:var(--dim)}
.sr{padding:16px 20px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
input,select{background:var(--srf);border:1px solid var(--brd);color:var(--txt);padding:10px 14px;border-radius:8px;font-size:14px;outline:none}
input:focus{border-color:var(--acc)}input::placeholder{color:var(--dim)}
#q{width:240px}#sid{width:200px}#ep{width:64px}select{cursor:pointer}
.btn{background:var(--acc);color:#fff;border:none;padding:10px 18px;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer;transition:background .2s}
.btn:hover{background:var(--acc-h)}.btn:disabled{opacity:.5;cursor:not-allowed}
.main{display:flex;max-width:1400px;margin:0 auto;padding:0 20px 40px;gap:20px}
.pc{flex:1;min-width:0}
.vw{position:relative;background:#000;border-radius:12px;overflow:hidden;aspect-ratio:16/9;box-shadow:0 8px 32px rgba(0,0,0,.5)}
#ply{width:100%;height:100%;display:block;object-fit:contain;background:#000}
.sb{margin-top:12px;padding:10px 14px;background:var(--srf);border:1px solid var(--brd);border-radius:8px;font-size:13px;color:var(--dim);display:flex;align-items:center;gap:8px;min-height:40px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--dim);flex-shrink:0}
.dot.ld{background:#f59e0b;animation:p 1s infinite}.dot.ok{background:var(--grn)}.dot.er{background:var(--red)}
@keyframes p{0%,100%{opacity:1}50%{opacity:.4}}
.srv{margin-top:12px;display:flex;flex-wrap:wrap;gap:8px}
.srvb{background:var(--srf2);border:1px solid var(--brd);color:var(--txt);padding:8px 14px;border-radius:8px;font-size:13px;cursor:pointer;transition:all .2s;display:flex;align-items:center;gap:6px}
.srvb:hover{border-color:var(--acc);background:var(--srf)}.srvb.ac{background:var(--acc);border-color:var(--acc);color:#fff}
.srvb .tg{font-size:10px;padding:2px 5px;border-radius:4px;background:rgba(255,255,255,.15);font-weight:600}.srvb.ac .tg{background:rgba(0,0,0,.25)}
.srvb.off{opacity:.4;cursor:not-allowed}
.side{width:300px;flex-shrink:0;display:flex;flex-direction:column;gap:12px}
.rp,.ep{background:var(--srf);border:1px solid var(--brd);border-radius:12px;overflow:hidden}
.rp h3,.ep h3{padding:12px 14px;font-size:13px;color:var(--dim);border-bottom:1px solid var(--brd);font-weight:600}
.ri{padding:10px 14px;border-bottom:1px solid var(--brd);cursor:pointer;font-size:13px;display:flex;justify-content:space-between;align-items:center;gap:8px}
.ri:hover{background:var(--srf2)}.ri:last-child{border-bottom:none}
.ri .nm{font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ri .id{font-size:10px;color:var(--dim);font-family:monospace;flex-shrink:0}
.el{max-height:300px;overflow-y:auto}
.ei{padding:8px 14px;border-bottom:1px solid var(--brd);cursor:pointer;font-size:13px;display:flex;align-items:center;gap:8px}
.ei:hover{background:var(--srf2)}.ei.ac{background:rgba(124,58,237,.15);border-left:3px solid var(--acc)}
.en{font-weight:600;min-width:28px}
.rt{margin-top:12px;background:none;border:1px solid var(--brd);color:var(--dim);padding:8px 14px;border-radius:8px;font-size:12px;cursor:pointer;width:100%;text-align:left}
.rt:hover{color:var(--txt)}
#raw{display:none;margin-top:8px;background:var(--srf);border:1px solid var(--brd);border-radius:8px;padding:12px;font-size:11px;font-family:monospace;color:var(--dim);max-height:400px;overflow:auto;white-space:pre-wrap;word-break:break-all}
@media(max-width:768px){.main{flex-direction:column}.side{width:100%;margin:0}}
</style></head><body>
<div class="hd"><h1>AllManga</h1><div class="sub">Anime Streaming Scraper</div></div>
<div class="sr">
<input id="q" placeholder="Search anime..." onkeydown="if(event.key==='Enter')search()">
<select id="md"><option value="sub">SUB</option><option value="dub">DUB</option></select>
<button class="btn" onclick="search()">Search</button>
<input id="sid" placeholder="Show ID" onkeydown="if(event.key==='Enter')loadEps()">
<input id="ep" placeholder="Ep" value="1" onkeydown="if(event.key==='Enter')loadSrc()">
<button class="btn" onclick="loadSrc()">Load</button>
</div>
<div class="main">
<div class="pc">
<div class="vw"><video id="ply" controls playsinline></video></div>
<div class="sb"><div class="dot" id="dot"></div><span id="st">Ready</span></div>
<div class="srv" id="srv"></div>
<button class="rt" onclick="document.getElementById('raw').style.display=document.getElementById('raw').style.display==='none'?'block':'none'">Toggle Raw JSON</button>
<pre id="raw"></pre>
</div>
<div class="side">
<div class="rp"><h3>Search Results</h3><div id="res"></div></div>
<div class="ep"><h3>Episodes</h3><div class="el" id="eps"></div></div>
</div>
</div>
<script>
const $=id=>document.getElementById(id),H=Hls;
let _srv=[];
function st(t,x){$('st').textContent=t;$('dot').className='dot '+(x||'')}
async function search(){
  const q=$('q').value.trim();if(!q)return;
  st('Searching...','ld');
  try{const d=await(await fetch(`/search?q=${encodeURIComponent(q)}&mode=${$('md').value}&detailed=true`)).json();
  const e=(d?.shows?.edges)||[];
  $('res').innerHTML=e.map(x=>`<div class="ri" onclick="pick('${x._id}')"><span class="nm">${x.name||x._id}</span><span class="id">${x._id}</span></div>`).join('');
  st(`Found ${e.length}`,'ok')}catch(e){st('Error: '+e,'er')}}
function pick(id){$('sid').value=id;loadEps()}
async function loadEps(){
  const id=$('sid').value.trim();if(!id)return;
  st('Loading eps...','ld');
  try{const d=await(await fetch(`/episodes?showId=${encodeURIComponent(id)}`)).json();
  const dt=(d?.availableEpisodesDetail)||{};
  $('eps').innerHTML=Object.entries(dt).flatMap(([m,arr])=>arr.map(e=>`<div class="ei" onclick="playEp('${e}','${m}')"><span class="en">Ep ${e}</span><span style="font-size:10px;color:var(--dim);text-transform:uppercase">${m}</span></div>`)).join('');
  st('Episodes loaded','ok')}catch(e){st('Error: '+e,'er')}}
function playEp(ep,mode){$('ep').value=ep;$('md').value=mode;loadSrc()}
async function loadSrc(){
  const id=$('sid').value.trim(),ep=$('ep').value.trim(),md=$('md').value;
  if(!id||!ep)return;
  st('Loading sources...','ld');$('srv').innerHTML='';_hls=null;
  try{const d=await(await fetch(`/sources?showId=${encodeURIComponent(id)}&ep=${encodeURIComponent(ep)}&mode=${md}`)).json();
  _srv=d.servers||[];$('raw').textContent=JSON.stringify(d,null,2);
  if(!_srv.length){st('No sources','er');return}
  $('srv').innerHTML=_srv.map((s,i)=>{
    const hl=(s.links||[]).length>0,ifr=s.links&&s.links[0]&&s.links[0].format==='iframe';
    return `<button class="srvb ${!hl&&!ifr?'off':''}" data-idx="${i}" onclick="play(${i})"><span>${s.server}</span><span class="tg">${s.type||''}</span>`+
           (hl?`<span class="tg" style="background:rgba(34,197,94,.2)">${s.links.length}</span>`:``)+
           (ifr?`<span class="tg" style="background:rgba(245,158,11,.2)">EMB</span>`:``)+`</button>`}).join('');
  const f=_srv.findIndex(s=>(s.links||[]).length>0);if(f>=0)play(f);
  st(`${_srv.length} servers`,'ok')}catch(e){st('Error: '+e,'er')}}
let _hls=null;
function play(idx){
  document.querySelectorAll('.srvb').forEach(b=>b.classList.remove('ac'));
  const btn=document.querySelector(`.srvb[data-idx="${idx}"]`);if(btn)btn.classList.add('ac');
  const srv=_srv[idx];if(!srv)return;
  const link=srv.links[0];
  if(link.format==='iframe'){
    document.querySelector('.vw').innerHTML=`<iframe src="${link.url}" style="width:100%;height:100%;border:none" allowfullscreen allow="autoplay;encrypted-media"></iframe>`;
    st(`${srv.server} (embedded)`,'ok');return}
  if(_hls){_hls.destroy();_hls=null;}
  const w=document.querySelector('.vw');if(!w.querySelector('video'))w.innerHTML='<video id="ply" controls playsinline style="width:100%;height:100%;display:block;object-fit:contain;background:#000"></video>';
  const v=$('ply'),u=link.proxy||link.url,hls=u.endsWith('.m3u8')||link.format==='hls';
  st(`Loading ${srv.server}...`,'ld');
  if(hls&&Hls.isSupported()){_hls=new Hls;_hls.loadSource(u);_hls.attachMedia(v);
    _hls.on(Hls.Events.MANIFEST_PARSED,()=>{v.play().catch(()=>{});st(srv.server+' (HLS)','ok')});
    _hls.on(Hls.Events.ERROR,(e,d)=>{if(d.fatal){st('HLS error','er');_hls.destroy();_hls=null}})}
  else if(hls&&v.canPlayType('application/vnd.apple.mpegurl')){v.src=u;v.play().catch(()=>{});st(srv.server+' (HLS native)','ok')}
  else{v.src=u;v.load();v.play().catch(()=>{});st(srv.server+' ('+link.format+')','ok')}}
</script></body></html>"""
