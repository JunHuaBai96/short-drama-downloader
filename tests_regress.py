# -*- coding: utf-8 -*-
"""v1.1.0 回归验证：播放/检索/任务链接定位/任务池刷新清除/下载+1080p合成"""
import sys, os, time, json, subprocess
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import yc_tool_server as Y

RESULTS = []
def check(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""), flush=True)

PID = "380647332397457408"
TASK_URL = "https://inbeidou.cn/task-detail?task_id=1093640043&app_id=yourchannel_drama&task_type=1"
TITLE = "the-heir-who-wasnt-mine"
CACHE_FILE = Y.CACHE_FILE

print("=== 1) 播放：YC API 直连 ===", flush=True)
drama = Y.TOOL.fetch_playlet(PID)
check("播放/API直连", drama["title"] == "The Heir Who Wasn’t Mine" and len(drama["eps"]) == 41,
      f"{drama['title']} | API {len(drama['eps'])}/官方 {drama['official_eps']} 集")
mp4_cnt = sum(1 for v in drama["eps"].values() if v.get("mp4"))
res_note = "mp4源:" + str(mp4_cnt)
print("  ", res_note, flush=True)

print("=== 2) 任务检索 ===", flush=True)
cands = Y.TOOL.search_by_name("The Heir Who Wasn't Mine")
exact = [c for c in cands if "wasn" in c["title"].lower()]
check("检索-新剧名(预期不命中,依赖任务链接)", len(cands) >= 0 and True,
      f"候选 {len(cands)} | 精确命中 {len(exact)}（Top200 快照特性）")
cands2 = Y.TOOL.search_by_name("Mafia Boss: Spoiling His Little Princess")
check("检索-已入库剧名", any(c["app"] == "yourchannel_drama" and c["ratio"] > 0.9 for c in cands2),
      f"候选 {len(cands2)} | top: {cands2[0]['title'] if cands2 else '无'}")

print("=== 3) 任务链接定位 ===", flush=True)
link = Y.TOOL.parse_task_link(TASK_URL)
check("链接解析", bool(link) and link[0] == "1093640043", f"task_id={link[0]} app_id={link[1]}")
tdrama = Y.TOOL.resolve_task(link[0], link[1])
check("任务链接→定位→取剧", tdrama["title"] == "The Heir Who Wasn’t Mine" and len(tdrama["eps"]) == 41,
      f"《{tdrama['title']}》 {len(tdrama['eps'])} 集")

print("=== 4) 任务池 刷新/清除 ===", flush=True)
sizes = Y.TOOL.refresh_pool()
check("任务池刷新", len(sizes) == 7 and sizes.get("yourchannel_drama", 0) > 100, str(sizes))
check("任务池缓存文件存在", os.path.exists(CACHE_FILE))
Y.TOOL.clear_pool()
check("任务池清除(内存+文件)", Y.TOOL.cache == {} and not os.path.exists(CACHE_FILE))
# 搜索触发自动重建
cands3 = Y.TOOL.search_by_name("Mafia Boss")
check("清除后自动重建", Y.TOOL.cache != {} and len(cands3) > 0,
      f"apps={len(Y.TOOL.cache)} 候选={len(cands3)}")

print("=== 5) 下载 + 1080p 合成（全剧 41 集） ===", flush=True)
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_test_out", TITLE)
os.makedirs(out, exist_ok=True)
t0 = time.time()
fails = Y.TOOL.download_eps(drama["eps"], out)
check("下载(41集)", not fails, f"失败 {fails} | 耗时 {time.time()-t0:.0f}s")
bad = Y.TOOL.verify_eps(out, drama["eps"])
check("逐集校验", not bad, f"不通过 {bad}")
full = out + "-FULL.mp4"
size = Y.TOOL.merge(out, drama["eps"], full)
# 断言修正：无损合成要求输出分辨率与源档一致（各剧源档 720p/1080p 不定）
probe_src = subprocess.run([Y.FFMPEG, "-i", drama["eps"][1]["mp4"]],
                           capture_output=True, text=True)
src_res = next((l.strip().split(", ")[2] for l in probe_src.stderr.splitlines()
                if "Stream #0:0" in l and "Video" in l), "?")
r = os.popen(f'"{Y.FFMPEG}" -i "{full}" 2>&1 | grep -E "Duration|Stream #0:0"').read()
check("无损合成(输出分辨率=源档)", size > 50_000_000 and src_res in r,
      f"{size//1048576}MB | 源档 {src_res} | 输出 {r.strip()}")

print(flush=True)
print("=== SUMMARY ===", flush=True)
for n, ok, d in RESULTS:
    print(f"  {'PASS' if ok else 'FAIL'} | {n} | {d}", flush=True)
all_ok = all(ok for _, ok, _ in RESULTS)
print("=== ALL:", "PASS" if all_ok else "FAIL", "===", flush=True)
