# -*- coding: utf-8 -*-
"""v2.3.0 服务器端到端验证：
回归 dramawave 链路（Love Games, 51 集）+ 本轮修复 bibishort/hakunaymatata
直链 428 → force_edge 边缘代理降级（The Backup Bride's Revenge, 20 集）。"""
import os, sys, json, time, threading, urllib.request, shutil, re, subprocess

sys.path.insert(0, r"C:\Users\白俊华\Desktop\yc_drama_tool")
os.chdir(r"C:\Users\白俊华\Desktop\yc_drama_tool")
import yc_tool_server as S

S.PORT = 8772
S.webbrowser.open = lambda *a, **k: None
OUT = r"C:\Users\白俊华\Desktop\yc_drama_tool\_test_srv23"
shutil.rmtree(OUT, ignore_errors=True)
os.makedirs(OUT, exist_ok=True)

results = []
def ok(name, cond, ev):
    results.append((name, bool(cond), ev))
    print(f"[{'PASS' if cond else 'FAIL'}] {name} | {ev}", flush=True)

def api(path, payload=None):
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{S.PORT}{path}", data=data,
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())

def wait_idle(timeout=240, expect_result=None):
    end = time.time() + timeout
    saw_busy = False
    while time.time() < end:
        st = json.loads(urllib.request.urlopen(
            f"http://127.0.0.1:{S.PORT}/api/status", timeout=10).read())
        if st["busy"]:
            saw_busy = True
        elif saw_busy or not expect_result:
            if not expect_result or (st.get("result") or {}).get("type") == expect_result:
                return st
        time.sleep(1)
    raise TimeoutError(f"等待空闲超时 expect={expect_result}")

th = threading.Thread(target=S.main, daemon=True)
th.start()
time.sleep(1.5)

# 1 播放：bibishort 直链 CDN（hakunaymatata/CloudFront）428 → force_edge 边缘流可用
sess = S.narto_session()
slug_bb = "the-backup-bride-s-revenge"
u_direct = S.narto_play_url(slug_bb, 1, sess)
direct_probe = S.narto_probe(u_direct, sess)
u_edge = S.narto_play_url(slug_bb, 1, sess, force_edge=True)
edge_probe = S.narto_probe(u_edge, sess)
import imageio_ffmpeg
F = imageio_ffmpeg.get_ffmpeg_exe()
_ff = subprocess.run([F, "-y", "-hide_banner", "-loglevel", "error",
    "-headers", f"Referer: {S.NARTO_MAIN}/\r\nUser-Agent: Mozilla/5.0\r\n",
    "-i", u_edge, "-t", "6", "-c", "copy", "-bsf:a", "aac_adtstoasc",
    "-movflags", "+faststart", "_v230_play.mp4"], capture_output=True, text=True,
    timeout=240)
_ffok = _ff.returncode == 0 and os.path.exists("_v230_play.mp4") \
    and os.path.getsize("_v230_play.mp4") > 300_000
ok("1.播放（直链探测+边缘降级）", edge_probe and _ffok,
   f"直链({u_direct.split('/')[2]})探测={direct_probe} → 边缘({u_edge.split('/')[2]})"
   f"探测={edge_probe} ffmpeg实测6秒={os.path.getsize('_v230_play.mp4')//1024}KB")

# 2 任务检索（北斗池 8 平台回归）
cands = S.TOOL.search_global("After the Sacrifice")
dw = [c for c in cands if c["app"] == "dramawave"]
ok("2.任务检索", len(cands) > 0 and len(dw) > 0,
   f"命中{len(cands)}条 dramawave池{len(dw)}条")

# 3 实时检索（用户日志原始查询词）
api("/api/search", {"text": "The Backup Girlfriend's Revenge"})
st = wait_idle()
nc = [c for c in ((st.get("result") or {}).get("candidates") or [])
      if c.get("app") == "narto"]
hit = next((c for c in nc if "Backup Bride's Revenge" in c["title"]), None)
ok("3.实时检索", hit is not None,
   f"narto 命中{len(nc)}条, 含《{hit['title']}》(ratio={hit['ratio']}, id={hit['playlet_id']})"
   if hit else f"narto 命中{len(nc)}条但无目标")

# 4 任务链接定位（dramawave 回归）
api("/api/search", {"text": "https://inbeidou.cn/task-detail?task_id=1062568559&app_id=dramawave"})
st = wait_idle()
cur = st.get("current") or {}
ok("4.任务链接定位", cur.get("title") == "Love Games with My Dragon Boss"
   and cur.get("official_eps") == 51,
   f"task 1062568559(dramawave) → 《{cur.get('title')}》{cur.get('official_eps')}集")

# 5 任务池刷新
api("/api/refresh")
st = wait_idle()
ok("5.任务池刷新", "已刷新" in st["status"] and len(S.TOOL.cache) == 8,
   st["status"] + f" 平台数={len(S.TOOL.cache)} 合计={sum(len(v) for v in S.TOOL.cache.values())}条")

# 6 任务池清除
api("/api/clear_pool")
st = wait_idle()
ok("6.任务池清除", "已清除" in st["status"] and not os.path.exists(S.CACHE_FILE),
   st["status"] + f" cache文件存在={os.path.exists(S.CACHE_FILE)}")

# 7 下载（bibishort 播放页直连，边缘降级路径，前 6 集实测）
api("/api/search", {"text": f"https://narto-drama.com/detail/watch/{slug_bb}?lang=zh-TW"})
st = wait_idle()
cur = st.get("current") or {}
ok("7a.播放页定位", cur.get("official_eps") == 20,
   f"《{cur.get('title')}》{cur.get('official_eps')}集")
keep = {i: S.STATE.current["eps"][i] for i in range(1, 7)}
S.STATE.current["eps"] = keep
api("/api/download", {"out": OUT})
st = wait_idle(600, expect_result="download")
res = st.get("result") or {}
ok("7b.下载", res.get("got") == 6 and not res.get("fails"),
   f"下载 {res.get('got')}/6 fails={res.get('fails')}")

# 8 无损合成
api("/api/merge")
st = wait_idle(600, expect_result="merged")
res = st.get("result") or {}
full = res.get("full") or ""
sz = os.path.getsize(full) // 1048576 if full and os.path.exists(full) else 0
ok("8.无损合成", res.get("type") == "merged" and sz > 1,
   f"FULL.mp4={sz}MB 时长={res.get('duration')}")

print("\n=== SUMMARY:", sum(1 for _, c, _ in results if c), "/", len(results), "PASS ===",
      flush=True)
