# -*- coding: utf-8 -*-
"""补充验证：实时检索（全名）+ 面板跨平台索引（面板内实际存在的剧名）"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import yc_tool_server as Y

print("=== 2b) 实时检索（全名） ===", flush=True)
c = Y.TOOL.search_global("Mafia Boss: Spoiling His Little Princess")
top = c[0] if c else None
ok1 = bool(top) and top["ratio"] > 0.95 and top["app"] == "yourchannel_drama"
print(f"[{'PASS' if ok1 else 'FAIL'}] 北斗实时检索 — {len(c)} 候选, "
      f"top={top['title'] if top else '-'} ratio={top['ratio'] if top else '-'}", flush=True)

print("=== 2c) 面板跨平台索引 ===", flush=True)
hits = Y.TOOL.search_panel("Ghost-Eyed Lin Yu")
ok2 = len(hits) > 0 and "面板" in hits[0]["app"]
print(f"[{'PASS' if ok2 else 'FAIL'}] 面板索引命中 — {len(hits)} 条, "
      f"top={hits[0]['title'] if hits else '-'} / {hits[0]['app'] if hits else '-'} "
      f"({hits[0]['date'] if hits else '-'})", flush=True)

print("=== 2d) 面板索引规模 ===", flush=True)
alld = Y.panel_index()
from collections import Counter
cnt = Counter(x["platform"] for x in alld)
print(f"[{'PASS' if len(alld) > 1000 and len(cnt) >= 10 else 'FAIL'}] "
      f"11 平台索引 — {len(alld)} 条 / {len(cnt)} 平台: {dict(cnt)}", flush=True)
