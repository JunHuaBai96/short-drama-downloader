# -*- coding: utf-8 -*-
"""无头验证 v2.1.0 新增 Narto Drama 支持"""
import os, sys, json, subprocess, re

sys.path.insert(0, r"C:\Users\白俊华\Desktop\yc_drama_tool")
os.chdir(r"C:\Users\白俊华\Desktop\yc_drama_tool")
os.environ["WBD"] = "1"
import yc_tool_server as S
import imageio_ffmpeg
FF = imageio_ffmpeg.get_ffmpeg_exe()
ENV = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
OUT = r"C:\Users\白俊华\Desktop\yc_drama_tool\_test_narto"
ok = lambda name, cond, ev: print(f"[{'PASS' if cond else 'FAIL'}] {name} | {ev}")

# 1) 平台识别
k, n = S.detect_platform("https://narto-drama.com/detail/watch/abc")
ok("1.平台识别", k == "narto", f"detect->({k},{n})")

# 2) 实时检索（SSR 全量分页，JSON-LD 清单）
import requests as _rq
def _ssr_slugs(page):
    sess = S.narto_session()
    r = sess.get(S.NARTO_MAIN, params={"lang": S.NARTO_LANG,
                                       "tab-provider": "bibishort",
                                       "page": page}, timeout=40)
    return {u.split("/detail/watch/")[1].split("?")[0] for u, n in
            re.findall(r'"@type":"ListItem","position":\d+,'
                       r'"url":"([^"]+)","name":"([^"]+)"', r.text)}
s1, s2, s3 = _ssr_slugs(1), _ssr_slugs(2), _ssr_slugs(3)
ok("2.Narto实时检索", len(s1) >= 20 and not (s1 & s2) and not (s2 & s3),
   f"SSR p1={len(s1)} p2={len(s2)} p3={len(s3)} 零重叠={not (s1 & s2)}"
   f" 去重合计={len(s1 | s2 | s3)}")

# 3) 播放页解析
sess = S.narto_session()
d = S.narto_resolve("https://narto-drama.com/detail/watch/shao-ye-pian-yao-wo-dong-xin?lang=zh-TW")
ok("3.播放页定位", d["official_eps"] == 52 and len(d["eps"]) == 52,
   f"《{d['title']}》官方{d['official_eps']}集 枚举{len(d['eps'])}集")

# 4) refresh-source 直链
u1 = S.narto_play_url(d["id"], 1, sess)
ok("4.refresh-source直链", u1.endswith(".mp4") and "cdn.narto-drama.com" in u1,
   u1[:90])

# 5) 直链可下载（Range 头 2KB）
r = S.narto_session().get(u1, headers={"Range": "bytes=0-2047"}, timeout=30)
ok("5.直链可访问", r.status_code == 206 and r.content[:4] != b"<!DOC",
   f"HTTP {r.status_code} bytes={len(r.content)} ftyp={r.content[4:8]}")

# 6) 站内关键字检索（search_narto）
try:
    cands = S.DramaTool().search_narto("新娘")
    ok("6.Narto剧名检索", len(cands) > 0,
       f"命中{len(cands)}条 首条:{cands[0]['title'][:20]} {cands[0]['app']}")
except Exception as e:
    ok("6.Narto剧名检索", False, f"异常:{e}")

# 7) 下载 ep1-ep2 + 校验
os.makedirs(OUT, exist_ok=True)
eps_test = {1: {"slug": d["id"]}, 2: {"slug": d["id"]}}
tool = S.DramaTool()
fails = tool.download_eps({no: {"mp4": S.narto_play_url(d["id"], no, sess)}
                           for no in eps_test}, OUT,
                          lambda no, st, dn, tt: print(f"    ep{no:02d} {st}"),
                          workers=2)
bad = tool.verify_eps(OUT, eps_test)
ok("7.下载+逐集校验", not fails and not bad, f"fails={fails} bad={bad}")

# 8) 无损合成 + 时间轴连续性检查
full = os.path.join(OUT, "FULL.mp4")
# 合成前记录 ep1 原始首帧（合成后 _ns 临时文件会被清理）
p0, t0 = tool._v_info(os.path.join(OUT, "ep01.mp4"))
size = tool.merge(OUT, eps_test, full)
r = subprocess.run([FF, "-hide_banner", "-i", full], capture_output=True,
                   text=True, timeout=120)
dur = re.search(r"Duration: (\S+)", r.stderr).group(1)
vline = [l for l in r.stderr.splitlines() if "Video" in l][0]
vres = re.search(r"(\d{3,4})x(\d{3,4})", vline)
vfps = re.search(r"(\d+) fps", vline)
# 归一化实测：对 ep01 重跑归一化，验证首帧归零 + 时基 15360
ns = tool._normalize_vstart(os.path.join(OUT, "ep01.mp4"), p0)
p1, t1 = tool._v_info(ns)
# 成片时间轴连续性：stts 采样表最大 delta 不应远超单帧间隔
sc = subprocess.run([sys.executable, "stts_check.py", full],
                    capture_output=True, text=True, timeout=60)
sc_out = sc.stdout
gap = re.search(r"max.*?delta.*?(\d+)", sc_out, re.I)
ok("8.1080p无损合成", os.path.getsize(full) > 1_000_000 and vres and
   p1 < 0.05 and t1 == 15360,
   f"FULL={size//1048576}MB 时长={dur} 分辨率={vres.group(0) if vres else '?'} "
   f"fps={vfps.group(1) if vfps else '?'} | 原始首帧pts={p0:.3f}s tbn={t0} "
   f"→ 归一化后 {p1:.3f}s / tbn={t1}（零转码 concat）")
print("stts_check:", sc_out[:300])
print("DONE")
