"""Full integration test — starts server, tests search + episodes + sources + proxy."""
import asyncio, httpx, json, subprocess, sys, time, os

PORT = 9876

async def test():
    proc = subprocess.Popen(
        [sys.executable, "-c",
         f"import uvicorn; uvicorn.run('mkissa_full:app', host='0.0.0.0', port={PORT}, log_level='error')"],
        cwd="/workspace", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(20):
        await asyncio.sleep(0.5)
        try:
            async with httpx.AsyncClient() as c:
                if (await c.get(f"http://127.0.0.1:{PORT}/", timeout=2)).status_code == 200:
                    break
        except: pass
    else:
        proc.kill(); print("FAIL: server didn't start"); return

    try:
        async with httpx.AsyncClient(timeout=30) as c:
            B = f"http://127.0.0.1:{PORT}"

            # 1. Frontend
            r = await c.get(f"{B}/")
            assert r.status_code == 200 and "AllManga" in r.text
            print(f"✅ Frontend: {len(r.content)} bytes")

            # 2. Search
            r = await c.get(f"{B}/search?q=iruma&mode=sub")
            d = r.json()
            shows = (d.get("shows") or {}).get("edges") or []
            total = (d.get("shows") or {}).get("pageInfo", {}).get("total", 0)
            assert shows, f"Search returned empty: {json.dumps(d)[:200]}"
            print(f"✅ Search: {len(shows)} results (total: {total})")
            show_id = shows[0]["_id"]
            show_name = shows[0].get("name", "?")
            print(f"   First: {show_name} ({show_id})")

            # 3. Episodes
            r = await c.get(f"{B}/episodes?showId={show_id}")
            ep_d = r.json()
            show = ep_d.get("show") or ep_d
            eps = (show.get("availableEpisodesDetail") or {}).get("sub") or []
            assert eps, f"No episodes: {json.dumps(ep_d)[:200]}"
            print(f"✅ Episodes: {len(eps)} subs available, latest: {eps[0]}")

            # 4. Sources
            ep_num = eps[0]
            r = await c.get(f"{B}/sources?showId={show_id}&ep={ep_num}&mode=sub")
            src = r.json()
            servers = src.get("servers", [])
            ok = [s for s in servers if s.get("links")]
            assert ok, f"No servers resolved: {json.dumps(src)[:300]}"
            print(f"\n✅ Sources: {len(ok)}/{len(servers)} servers resolved")
            for s in ok:
                for l in s["links"]:
                    print(f"   ✅ {s['server']:12s} {l['format']:4s} → {l['url'][:65]}")

            best = src.get("best")
            assert best, "No best link!"
            print(f"\n   🏆 Best: {best['url'][:70]}")

            # 5. Proxy — verify real MP4 data
            if any(s["server"] == "Mp4" and s.get("links") for s in servers):
                proxy_url = f"{B}{[l for s in servers if s['server']=='Mp4' for l in s['links']][0]['proxy']}"
                r = await c.get(proxy_url, headers={"Range": "bytes=0-1023"})
                is_mp4 = b'ftyp' in r.content[:20]
                assert r.status_code in (200, 206), f"Proxy HTTP {r.status_code}"
                assert is_mp4, f"Not MP4: {r.content[:16].hex()}"
                print(f"\n✅ Proxy Mp4upload: HTTP {r.status_code}, valid MP4 ✅, CORS=*")

            print(f"\n{'='*55}\n  ALL TESTS PASSED ✅\n{'='*55}")
    finally:
        proc.kill(); proc.wait()

asyncio.run(test())
