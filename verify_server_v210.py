# -*- coding: utf-8 -*-
"""v2.1.0 服务器端到端验证：真实 HTTP 接口逐项测试"""
import os, sys, json, time, threading, urllib.request, shutil

sys.path.insert(0, r"C:\Users\白俊华\Desktop\yc_drama_tool")
os.chdir(r"C:\Users\白俊华\Desktop\yc_drama_tool")
import yc_tool_server as S

S.PORT = 8770
S.webbrowser.open = lambda *a, **k: None
OUT = r"C:\Users\白俊华\Desktop\yc_drama_tool\_test_srv"
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
    """先等到 busy=True（任务已启动），再等到空闲；可选要求 result.type 匹配"""
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

# 1 播放（refresh-source 直链可访问 = 在线播放通道）
sess = S.narto_session()
u = S.narto_play_url("shao-ye-pian-yao-wo-dong-xin", 1, sess)
r = sess.get(u, headers={"Range": "bytes=0-1023"}, timeout=30)
ok("1.播放（直链通道）", r.status_code == 206, f"HTTP {r.status_code} {u[:80]}")

# 2 任务链接定位（北斗 task_id → YC）
api("/api/search", {"text": "https://inbeidou.cn/task-detail?task_id=716518113&app_id=yourchannel_drama"})
st = wait_idle()
cur = st.get("current") or {}
ok("2.任务链接定位", st["busy"] is False and bool(cur.get("title")),
   f"task 716518113 → 《{cur.get('title')}》{cur.get('official_eps')}集")

# 3 实时检索（北斗 + Narto 多剧场，剧名入口）
api("/api/search", {"text": "落跑新娘"})
st = wait_idle()
cands = ((st.get("result") or {}).get("candidates")) or []
narto_c = [c for c in cands if str(c.get("app", "")).startswith("narto·")]
ok("3.实时检索", len(cands) > 0 and len(narto_c) > 0,
   f"候选{len(cands)}条，其中 Narto 片源{narto_c.__len__()}条: "
   f"{narto_c[0]['title'][:16]}@{narto_c[0]['app']}" if narto_c else f"候选{len(cands)}")

# 4 任务池刷新
api("/api/refresh")
st = wait_idle()
ok("4.任务池刷新", "已刷新" in st["status"], st["status"] +
   " 缓存=" + str(sum(len(v) for v in S.TOOL.cache.values())) + " 条")

# 5 任务池清除
api("/api/clear_pool")
st = wait_idle()
ok("5.任务池清除", "已清除" in st["status"] and os.path.exists(S.CACHE_FILE) is False,
   st["status"] + f" cache文件存在={os.path.exists(S.CACHE_FILE)}")

# 6 下载（Narto 播放页直连 → 截取 2 集实测）
api("/api/search", {"text": "https://narto-drama.com/detail/watch/shao-ye-pian-yao-wo-dong-xin?lang=zh-TW"})
st = wait_idle()
cur = st.get("current") or {}
ok("6a.播放页定位", cur.get("official_eps") == 52,
   f"《{cur.get('title')}》{cur.get('official_eps')}集")
keep = dict(list(S.STATE.current["eps"].items())[:2])
S.STATE.current["eps"] = keep
api("/api/download", {"out": OUT})
st = wait_idle(600, expect_result="download")
res = st.get("result") or {}
ok("6b.下载", res.get("type") == "download" and res.get("got") == 2 and not res.get("fails"),
   f"下载完成 {res.get('got')}/{res.get('official')} fails={res.get('fails')} 目录={res.get('out')}")

# 7 1080p 无损合成
api("/api/merge")
st = wait_idle(600, expect_result="merged")
res = st.get("result") or {}
full = res.get("full") or ""
sz = os.path.getsize(full) // 1048576 if full and os.path.exists(full) else 0
ok("7.无损合成", res.get("type") == "merged" and sz > 1,
   f"FULL.mp4={sz}MB 时长={res.get('duration')}")

print("\n=== SUMMARY:", sum(1 for _, c, _ in results if c), "/", len(results), "PASS ===")
