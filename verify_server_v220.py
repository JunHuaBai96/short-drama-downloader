# -*- coding: utf-8 -*-
"""v2.2.0 服务器端到端验证：dramawave 任务 + narto 片源定位 + 全链路"""
import os, sys, json, time, threading, urllib.request, shutil, re, subprocess

sys.path.insert(0, r"C:\Users\白俊华\Desktop\yc_drama_tool")
os.chdir(r"C:\Users\白俊华\Desktop\yc_drama_tool")
import yc_tool_server as S

S.PORT = 8771
S.webbrowser.open = lambda *a, **k: None
OUT = r"C:\Users\白俊华\Desktop\yc_drama_tool\_test_srv2"
shutil.rmtree(OUT, ignore_errors=True)
os.makedirs(OUT, exist_ok=True)

results = []
def ok(name, cond, ev):
    results.append((name, bool(cond), ev))
    print(f"[{'PASS' if cond else 'FAIL'}] {name} | {ev}")

def api(path, payload=None):
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{S.PORT}{path}", data=data,
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())

def wait_idle(timeout=180, expect_result=None):
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

# 1 播放（refresh-source 直链可访问：bibishort 源=直链 MP4，dramawave 源=站方代理 HLS）
sess = S.narto_session()
u = S.narto_play_url("love-games-with-my-dragon-boss", 1, sess)
r = sess.get(u, headers={"Range": "bytes=0-1023", "Accept": "video/*"}, timeout=60)
body_ok = r.content[:4] == b"#EXT" or r.content[4:8] == b"ftyp"
ok("1.播放（直链通道）", r.status_code in (200, 206) and body_ok,
   f"HTTP {r.status_code} {r.headers.get('Content-Type')} "
   f"{'代理HLS' if r.content[:4] == b'#EXT' else 'MP4直链'} {u[:70]}")

# 2 任务检索（北斗池实时拉取，含 dramawave 共 8 平台）
cands = S.TOOL.search_global("After the Sacrifice")
dw = [c for c in cands if c["app"] == "dramawave"]
ok("2.任务检索", len(cands) > 0 and len(dw) > 0,
   f"命中{len(cands)}条 dramawave池{len(dw)}条: {dw[0]['title'][:40] if dw else '-'}")

# 3 实时检索（narto /search?q= 免登录，目标剧）
api("/api/search", {"text": "Love Games with My Dragon Boss"})
st = wait_idle()
nc = [c for c in ((st.get("result") or {}).get("candidates") or [])
      if c.get("app") == "narto"]
ok("3.实时检索", len(nc) > 0 and nc[0]["ratio"] > 0.9,
   f"narto 首条:《{nc[0]['title']}》ratio={nc[0]['ratio']}" if nc else "无命中")

# 4 任务链接定位（dramawave task → narto 片源）
api("/api/search", {"text": "https://inbeidou.cn/task-detail?task_id=1062568559&app_id=dramawave"})
st = wait_idle()
cur = st.get("current") or {}
ok("4.任务链接定位", cur.get("title") == "Love Games with My Dragon Boss"
   and cur.get("official_eps") == 51,
   f"task 1062568559(dramawave) → 《{cur.get('title')}》{cur.get('official_eps')}集")

# 5 任务池刷新
api("/api/refresh")
st = wait_idle()
n_app = len(S.TOOL.cache)
ok("5.任务池刷新", "已刷新" in st["status"] and n_app == 8,
   st["status"] + f" 平台数={n_app} 合计={sum(len(v) for v in S.TOOL.cache.values())}条")

# 6 任务池清除
api("/api/clear_pool")
st = wait_idle()
ok("6.任务池清除", "已清除" in st["status"] and not os.path.exists(S.CACHE_FILE),
   st["status"] + f" cache文件存在={os.path.exists(S.CACHE_FILE)}")

# 7 下载（narto 播放页直连 → 截取 ep1/ep2/ep51 三集实测）
api("/api/search", {"text": "https://narto-drama.com/detail/watch/love-games-with-my-dragon-boss?lang=zh-TW"})
st = wait_idle()
cur = st.get("current") or {}
ok("7a.播放页定位", cur.get("official_eps") == 51,
   f"《{cur.get('title')}》{cur.get('official_eps')}集")
keep = {1: S.STATE.current["eps"][1], 2: S.STATE.current["eps"][2],
        51: S.STATE.current["eps"][51]}
S.STATE.current["eps"] = keep
api("/api/download", {"out": OUT})
st = wait_idle(600, expect_result="download")
res = st.get("result") or {}
ok("7b.下载", res.get("got") == 3 and not res.get("fails"),
   f"下载完成 {res.get('got')}/{res.get('official')} fails={res.get('fails')}")

# 8 1080p 无损合成
api("/api/merge")
st = wait_idle(600, expect_result="merged")
res = st.get("result") or {}
full = res.get("full") or ""
sz = os.path.getsize(full) // 1048576 if full and os.path.exists(full) else 0
ok("8.无损合成", res.get("type") == "merged" and sz > 1,
   f"FULL.mp4={sz}MB 时长={res.get('duration')}")

print("\n=== SUMMARY:", sum(1 for _, c, _ in results if c), "/", len(results), "PASS ===")
