# -*- coding: utf-8 -*-
"""无头验证 v2.2.0：YC 回归 + Narto /search 通道 + 全链路"""
import os, sys, json, subprocess, re

sys.path.insert(0, r"C:\Users\白俊华\Desktop\yc_drama_tool")
os.chdir(r"C:\Users\白俊华\Desktop\yc_drama_tool")
import yc_tool_server as S
import imageio_ffmpeg
FF = imageio_ffmpeg.get_ffmpeg_exe()
OUT = r"C:\Users\白俊华\Desktop\yc_drama_tool\_test_narto"
ok = lambda name, cond, ev: print(f"[{'PASS' if cond else 'FAIL'}] {name} | {ev}")

# 1) 平台识别
k, n = S.detect_platform("https://narto-drama.com/detail/watch/abc")
ok("1.平台识别", k == "narto", f"detect->({k},{n})")

# 2) 实时检索（/search?q= 免登录通道，目标剧命中）
cands = S.DramaTool().search_narto("Love Games with My Dragon Boss")
ok("2.Narto实时检索", len(cands) > 0 and cands[0]["ratio"] > 0.9,
   f"命中{len(cands)}条 首条:《{cands[0]['title']}》ratio={cands[0]['ratio']}")

# 3) YC 源回归：任务链接定位（task 716518113 → YC API）
d_yc = S.DramaTool().resolve_task("716518113", "yourchannel_drama")
ok("3.YC任务定位回归", d_yc["official_eps"] == 75 and len(d_yc["eps"]) == 75,
   f"《{d_yc['title']}》官方{d_yc['official_eps']}集 API返回{len(d_yc['eps'])}集")

# 4) 非 YC 任务定位（dramawave → narto 片源）
d = S.DramaTool().resolve_task("1062568559", "dramawave")
ok("4.非YC任务定位", d["official_eps"] == 51 and d.get("source_app") == "dramawave",
   f"task 1062568559(dramawave) → 《{d['title']}》{d['official_eps']}集 narto")

# 5) refresh-source 直链（bibishort 源 mp4 / dramawave 源代理 HLS）
sess = S.narto_session()
u1 = S.narto_play_url("shao-ye-pian-yao-wo-dong-xin", 1, sess)
u2 = S.narto_play_url("love-games-with-my-dragon-boss", 1, sess)
r2 = sess.get(u2, headers={"Accept": "video/*"}, timeout=60)
ok("5.refresh-source直链", u1.endswith(".mp4") and r2.content[:4] == b"#EXT",
   f"bibishort={u1[:60]}... | dramawave=代理HLS {r2.headers.get('Content-Type')}")

# 6) 全 51 集直链解析
import concurrent.futures as cf
def g(no):
    try:
        S.narto_play_url(d["id"], no, sess); return no, True
    except Exception as e:
        return no, str(e)
with cf.ThreadPoolExecutor(4) as ex:
    res = dict(ex.map(g, range(1, 52)))
fails = [n for n, r in res.items() if r is not True]
ok("6.全51集直链", not fails, f"成功{51-len(fails)}/51 失败={fails[:5]}")

# 7) 下载 + 逐集校验（ep01/ep02/ep51）
os.makedirs(OUT, exist_ok=True)
tool = S.DramaTool()
eps_test = {1: {"mp4": S.narto_play_url(d["id"], 1, sess)},
            2: {"mp4": S.narto_play_url(d["id"], 2, sess)},
            51: {"mp4": S.narto_play_url(d["id"], 51, sess)}}
fails = tool.download_eps(eps_test, OUT,
                          lambda no, st, dn, tt: print(f"    ep{no:02d} {st}"),
                          workers=2)
bad = tool.verify_eps(OUT, eps_test)
ok("7.下载+逐集校验", not fails and not bad, f"fails={fails} bad={bad}")

# 8) 无损合成 + 时间轴连续性
full = os.path.join(OUT, "FULL_v220.mp4")
p0, t0 = tool._v_info(os.path.join(OUT, "ep01.mp4"))
size = tool.merge(OUT, eps_test, full)
r = subprocess.run([FF, "-hide_banner", "-i", full], capture_output=True,
                   text=True, timeout=120)
dur = re.search(r"Duration: (\S+)", r.stderr).group(1)
vline = [l for l in r.stderr.splitlines() if "Video" in l][0]
vres = re.search(r"(\d{3,4})x(\d{3,4})", vline)
ns = tool._normalize_vstart(os.path.join(OUT, "ep01.mp4"), p0)
p1, t1 = tool._v_info(ns)
sc = subprocess.run([sys.executable, "stts_check.py", full],
                    capture_output=True, text=True, timeout=60)
ok("8.1080p无损合成", os.path.getsize(full) > 1_000_000 and vres and
   p1 < 0.05 and t1 == 15360,
   f"FULL={size//1048576}MB 时长={dur} 分辨率={vres.group(0) if vres else '?'} | "
   f"原始首帧pts={p0:.3f}s tbn={t0} → 归一化 {p1:.3f}s / tbn={t1}")
print("stts_check:", sc.stdout[:200])
print("DONE")
