# -*- coding: utf-8 -*-
"""全量验证 v2.0：YC + NetShort 双平台"""
import sys, os, time, subprocess
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import yc_tool_server as Y

R = []
def chk(name, ok, ev=""):
    R.append((name, ok, ev)); print(f"[{'PASS' if ok else 'FAIL'}] {name} — {ev}", flush=True)

NS_URL = "https://web.netshort.com/episode/the-wolfless-alpha-walks-away-2118536963232788481"
YC_PID = "380647332397457408"
TASK_URL = "https://inbeidou.cn/task-detail?task_id=1093640043&app_id=yourchannel_drama&task_type=1"

print("=== 1) 播放 ===", flush=True)
yc = Y.TOOL.fetch_playlet(YC_PID)
chk("YC 播放/API直连", len(yc["eps"]) == 41, f"{yc['title']} {len(yc['eps'])}/41 集")
ns = Y.netshort_resolve(NS_URL)
chk("NetShort 播放页解析", ns["official_eps"] == 45, f"{ns['title']} {ns['official_eps']} 集")

print("=== 2) 实时检索 ===", flush=True)
c = Y.TOOL.search_global("Mafia Boss")
chk("北斗实时检索", any(x["ratio"] > 0.8 for x in c), f"{len(c)} 候选, top={c[0]['title'] if c else ''}")
p = Y.TOOL.search_panel("The Wolfless Alpha Walks Away")
chk("面板跨平台索引", len(p) > 0, f"命中 {len(p)} 条, 平台={p[0]['app'] if p else '-'}")

print("=== 3) 任务链接定位 ===", flush=True)
t = Y.TOOL.parse_task_link(TASK_URL)
d = Y.TOOL.resolve_task(t[0], t[1])
chk("北斗任务链接→YC剧", d["title"].startswith("The Heir") and len(d["eps"]) == 41,
    f"task_id={t[0]} →《{d['title']}》{len(d['eps'])} 集")

print("=== 4) 任务池刷新 ===", flush=True)
sizes = Y.TOOL.refresh_pool()
chk("任务池刷新", len(sizes) == 7 and sizes["yourchannel_drama"] >= 200, str(sizes))

print("=== 5) 任务池清除 ===", flush=True)
Y.TOOL.clear_pool()
chk("清除(内存+文件)", Y.TOOL.cache == {} and not os.path.exists(Y.CACHE_FILE))
c2 = Y.TOOL.search_by_name("Mafia Boss")
chk("清除后自动重建", len(Y.TOOL.cache) == 7 and len(c2) > 0, f"{len(c2)} 候选")

print("=== 6) 下载 ===", flush=True)
out_yc = os.path.abspath(os.path.join("_test_out", "the-heir-who-wasnt-mine"))
f1 = Y.TOOL.download_eps(yc["eps"], out_yc)
chk("YC 下载 41 集", not f1, f"失败 {f1}")

out_ns = os.path.abspath(os.path.join("_test_out", "the-wolfless-alpha"))
t0 = time.time()
f2 = Y.netshort_download(ns["eps"], out_ns, progress=None)
chk("NetShort 下载 45 集", not f2, f"失败 {f2} | {time.time()-t0:.0f}s")

print("=== 7) 1080p/原档 无损合成 ===", flush=True)
def probe(f):
    r = subprocess.run([Y.FFMPEG, "-i", f], capture_output=True, text=True)
    res = next((l.split(", ")[2] for l in r.stderr.splitlines()
                if "Stream #0:0" in l and "Video" in l), "?")
    dur = next((l.split("Duration: ")[1].split(",")[0] for l in r.stderr.splitlines()
                if "Duration:" in l), "?")
    return res, dur
src_res, _ = probe(os.path.join(out_yc, "ep01.mp4"))
full_yc = os.path.join(out_yc, "FULL.mp4")
sz1 = Y.TOOL.merge(out_yc, yc["eps"], full_yc)
r1, d1 = probe(full_yc)
chk("YC 无损合成", src_res in r1 and sz1 > 100_000_000, f"{sz1//1048576}MB {r1} {d1}")

src_res2, _ = probe(os.path.join(out_ns, "ep01.mp4"))
full_ns = os.path.join(out_ns, "FULL.mp4")
sz2 = Y.TOOL.merge(out_ns, ns["eps"], full_ns)
r2, d2 = probe(full_ns)
chk("NetShort 无损合成", src_res2 in r2 and sz2 > 50_000_000, f"{sz2//1048576}MB {r2} {d2}")

print("\n=== SUMMARY ===", flush=True)
for n, ok, e in R:
    print(f"  {'PASS' if ok else 'FAIL'} | {n} | {e}", flush=True)
print("=== ALL:", "PASS" if all(x[1] for x in R) else "FAIL", "===", flush=True)
