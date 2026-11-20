# -*- coding: utf-8 -*-
"""v2.4.0 服务器端到端验证：多任务并行/排队下载 + 八项回归。
场景：
  A.《The Backup Bride's Revenge (2024)》20 集（task_add 播放页直连，全流程）
  E.《The Backup Boyfriend》35 集（与 A 并行下载，结束后取消于阶段边界）
  C. 假链接（失败隔离 → failed，不影响 A/E）
  D. A 重复提交（去重拒绝）
  Q. dramawave 任务链接 51 集（第 4 个任务，应排队；排队态取消）"""
import os, sys, json, time, threading, urllib.request, shutil, re, subprocess

sys.path.insert(0, r"C:\Users\白俊华\Desktop\yc_drama_tool")
os.chdir(r"C:\Users\白俊华\Desktop\yc_drama_tool")
import yc_tool_server as S

S.PORT = 8773
S.webbrowser.open = lambda *a, **k: None
ROOT = r"C:\Users\白俊华\Desktop\yc_drama_tool\_test_srv24"
shutil.rmtree(ROOT, ignore_errors=True)
os.makedirs(ROOT, exist_ok=True)

results = []
def ok(name, cond, ev):
    results.append((name, bool(cond), ev))
    print(f"[{'PASS' if cond else 'FAIL'}] {name} | {ev}", flush=True)

def api(path, payload=None):
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{S.PORT}{path}", data=data,
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())

def get_tasks():
    return json.loads(urllib.request.urlopen(
        f"http://127.0.0.1:{S.PORT}/api/status", timeout=10).read())["tasks"]

def task_by(tid):
    for t in get_tasks():
        if t["id"] == tid:
            return t
    return None

def wait_task(tid, status, timeout=900):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        last = task_by(tid)
        if last and (last["status"] == status or
                     last["status"] in ("done", "failed", "canceled")):
            return last
        time.sleep(1.5)
    raise TimeoutError(f"任务#{tid} 等待 {status} 超时，最后状态={last}")

th = threading.Thread(target=S.main, daemon=True)
th.start()
time.sleep(1.5)

# ---------- 1 播放：边缘流 ffmpeg 实拉 ----------
import imageio_ffmpeg
F = imageio_ffmpeg.get_ffmpeg_exe()
sess = S.narto_session()
u = S.narto_play_url("the-backup-bride-s-revenge", 1, sess)
probe = S.narto_probe(u, sess)
_ff = subprocess.run([F, "-y", "-hide_banner", "-loglevel", "error",
    "-headers", f"Referer: {S.NARTO_MAIN}/\r\nUser-Agent: Mozilla/5.0\r\n",
    "-i", u, "-t", "6", "-c", "copy", "-bsf:a", "aac_adtstoasc",
    "-movflags", "+faststart", os.path.join(ROOT, "play.mp4")],
    capture_output=True, text=True, timeout=240)
_play_ok = _ff.returncode == 0 and os.path.exists(
    os.path.join(ROOT, "play.mp4")) and os.path.getsize(
    os.path.join(ROOT, "play.mp4")) > 300_000
ok("1.播放", probe and _play_ok,
   f"边缘流探测={probe} ffmpeg实拉6秒={os.path.getsize(os.path.join(ROOT,'play.mp4'))//1024}KB")

# ---------- 2 任务检索（北斗池回归） ----------
cands = S.TOOL.search_global("After the Sacrifice")
dw = [c for c in cands if c["app"] == "dramawave"]
ok("2.任务检索", len(cands) > 0 and len(dw) > 0,
   f"北斗池命中{len(cands)}条 dramawave池{len(dw)}条")

# ---------- 3 实时检索 ----------
api("/api/search", {"text": "The Backup Girlfriend's Revenge"})
end = time.time() + 120
while time.time() < end:
    st = json.loads(urllib.request.urlopen(
        f"http://127.0.0.1:{S.PORT}/api/status", timeout=10).read())
    if not st["legacy_busy"]:
        break
    time.sleep(1)
nc = [c for c in ((st.get("result") or {}).get("candidates") or [])
      if c.get("app") == "narto"]
ok("3.实时检索", any("Backup Bride's Revenge" in c["title"] for c in nc),
   f"narto 命中{len(nc)}条，含目标剧" if nc else "无命中")

# ---------- 5 任务池刷新 ----------
api("/api/refresh")
end = time.time() + 120
while time.time() < end:
    st = json.loads(urllib.request.urlopen(
        f"http://127.0.0.1:{S.PORT}/api/status", timeout=10).read())
    if not st["legacy_busy"]:
        break
    time.sleep(1)
ok("5.任务池刷新", "已刷新" in st["status"] and len(S.TOOL.cache) == 8,
   st["status"] + f" 平台数={len(S.TOOL.cache)} 合计={sum(len(v) for v in S.TOOL.cache.values())}条")

# ---------- 6 任务池清除 ----------
api("/api/clear_pool")
time.sleep(2)
ok("6.任务池清除", "已清除" in S.STATE.status_msg and not os.path.exists(S.CACHE_FILE),
   S.STATE.status_msg + f" cache文件存在={os.path.exists(S.CACHE_FILE)}")

# ---------- 7 下载：多任务并行/排队/失败隔离/去重 ----------
ids0 = {t["id"] for t in get_tasks()}
def new_task_id():
    time.sleep(1.5)
    return max(t["id"] for t in get_tasks() if t["id"] not in ids0)

# A（20集）+ E（20集，另一部剧）同时入队：两者平行解析→平行下载，
# 用于观测真实的并行窗口（并行上限 2）
api("/api/task_add", {"text": "https://narto-drama.com/detail/watch/the-backup-bride-s-revenge?lang=zh-TW",
                      "out": ROOT})
tidA = new_task_id()
api("/api/task_add", {"text": "https://narto-drama.com/detail/watch/99-divorces-then-my-husband-died-for-me?lang=zh-TW",
                      "out": ROOT})
tidE = new_task_id()
# 去重：重复提交 A（应被拒，无新任务）
r2 = api("/api/task_add", {"text": "https://narto-drama.com/detail/watch/the-backup-bride-s-revenge?lang=zh-TW",
                           "out": ROOT})
# Q：第 4 个任务（dramawave 51 集）——应排队
api("/api/task_add", {"text": "https://inbeidou.cn/task-detail?task_id=1062568559&app_id=dramawave",
                      "out": ROOT})
tidQ = new_task_id()

# 7a 排队证据：Q 为 queued，活跃任务 ≤ 2
ts = get_tasks()
q_now = task_by(tidQ)
ok("7a.排队（第4任务等待空位）", q_now and q_now["status"] == "queued"
   and sum(1 for t in ts if t["status"] in ("downloading", "resolving",
                                            "verifying", "merging")) <= 2,
   f"Q#{tidQ}状态={q_now['status'] if q_now else '?'}，活跃任务状态="
   f"{[t['status'] for t in ts if t['status'] not in ('queued','done','failed','canceled')]}（上限2）")

# 7e 排队任务取消（queued 态 → 立即取消）
r_q = api("/api/task_cancel", {"id": tidQ})
q_after = wait_task(tidQ, "canceled", 60)
ok("7e.排队任务取消", r_q.get("ok") and q_after["status"] == "canceled",
   f"Q#{tidQ} cancel返回={r_q.get('ok')}，终态={q_after['status']}")

# 7b 并行证据：A 与 E 同时 downloading
saw_both = False
end = time.time() + 240
while time.time() < end:
    a, e = task_by(tidA), task_by(tidE)
    if a["status"] == "downloading" and e["status"] == "downloading":
        saw_both = True
        break
    if a["status"] in ("done", "failed") or e["status"] in ("done", "failed"):
        break
    time.sleep(1.5)
ok("7b.并行下载（A+E 同时进行）", saw_both,
   f"观测到 A 与 E 同时处于 downloading（并行上限 {S.TMGR.MAX_PARALLEL}）")

# 7h 下载中取消：不等整部剧跑完，在集进度边界生效（同时释放并行位）
r_e = api("/api/task_cancel", {"id": tidE})
tE_end = wait_task(tidE, "canceled", 600)
ok("7h.下载中取消（阶段边界生效）", r_e.get("ok") and tE_end["status"] == "canceled",
   f"E#{tidE} 取消返回={r_e.get('ok')}，终态={tE_end['status']}，"
   f"已下载 {tE_end['done']}/{tE_end['total']} 集（A 不受影响）")

# C：假链接（失败隔离）——此时并行位已空出，立即调度
api("/api/task_add", {"text": "https://narto-drama.com/detail/watch/not-a-real-drama-xyz?lang=zh-TW",
                      "out": ROOT})
tidC = new_task_id()

# 7c 失败隔离：C failed 且 A 仍正常推进/完成
tC_end = wait_task(tidC, "failed", 420)
a_now = task_by(tidA) or {}
ok("7c.失败隔离", tC_end["status"] == "failed" and bool(tC_end["error"])
   and a_now.get("status") in ("downloading", "verifying", "merging", "done"),
   f"假链接任务#{tidC} failed（{tC_end['error'][:50]}），A 状态={a_now.get('status')}")

# 7d 去重
same_cnt = sum(1 for t in get_tasks()
               if "Bride" in t["title"] and "Revenge" in t["title"])
ok("7d.去重", (not r2.get("ok", True)) or len(r2.get("skipped", [])) > 0
   or same_cnt == 1,
   f"重复提交返回 skipped={r2.get('skipped', [])}，同名任务数={same_cnt}")

# 7f 任务 A 全流程完成（下载→校验→自动合成）
tA_end = wait_task(tidA, "done", 900)
ok("7f.任务A全流程", tA_end["status"] == "done"
   and not tA_end["fails"] and not tA_end["bad"],
   f"任务#{tidA}《{tA_end['title']}》{tA_end['total']}集 "
   f"fails={tA_end['fails']} bad={tA_end['bad']} -> {tA_end['out']}")
fullA = os.path.join(tA_end["out"], "FULL.mp4")
szA = os.path.getsize(fullA) // 1048576 if os.path.exists(fullA) else 0
_r = subprocess.run([F, "-hide_banner", "-i", fullA], capture_output=True,
                    text=True, timeout=120) if szA else None
_dur = re.search(r"Duration: (\S+)", _r.stderr).group(1) if _r else "?"
_res = re.search(r"(\d{3,4})x(\d{3,4})", _r.stderr) if _r else None
ok("7g.任务A成片", szA > 100,
   f"FULL.mp4={szA}MB 时长={_dur} 分辨率={_res.group(0) if _res else '?'}")

# ---------- 4 任务链接定位（dramawave 回归：与任务队列 _resolve 同一调用） ----------
d4 = S.DramaTool().resolve_task("1062568559", "dramawave")
ok("4.任务链接定位", d4["title"] == "Love Games with My Dragon Boss"
   and d4["official_eps"] == 51,
   f"task 1062568559(dramawave) → 《{d4['title']}》{d4['official_eps']}集"
   f"（Task._resolve 同款调用）")

# ---------- 8 无损合成（merge-only 任务，不受去重限制） ----------
tM = S.Task(drama=S.narto_resolve(
                "https://narto-drama.com/detail/watch/the-backup-bride-s-revenge?lang=zh-TW"),
            out_dir=tA_end["out"], mode="merge")
okm, msgm = S.TMGR.add(tM)
tM_end = wait_task(tM.id, "done", 900)
fullM = os.path.join(tM_end["out"], "FULL.mp4")
szM = os.path.getsize(fullM) // 1048576 if os.path.exists(fullM) else 0
ok("8.无损合成", okm and tM_end["status"] == "done" and szM > 100,
   f"merge任务#{tM.id} done，FULL.mp4={szM}MB")

# ---------- 9 任务管理：移除 / 清除已完成 ----------
n_clear = api("/api/clear_finished", {}).get("removed", 0)
final_tasks = get_tasks()
ok("9.任务管理", tE_end["status"] == "canceled"
   and all(t["id"] not in (tidC, tidQ, tM.id) for t in final_tasks),
   f"E#{tidE} 取消于阶段边界（终态={tE_end['status']}），清除已完成任务 "
   f"{n_clear} 个，剩余={len(final_tasks)}（活跃任务不受清除影响）")

print("\n=== SUMMARY:", sum(1 for _, c, _ in results if c), "/", len(results), "PASS ===",
      flush=True)
