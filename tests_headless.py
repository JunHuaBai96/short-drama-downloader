# -*- coding: utf-8 -*-
"""无头测试 yc_tool_server 核心逻辑"""
import sys, os, shutil
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import yc_tool_server as Y

print("=== 1) 剧名搜索目标剧（预期：无精确匹配） ===", flush=True)
cands = Y.TOOL.search_by_name("When the World Underestimated Her")
for c in cands[:5]:
    print("  cand:", c["ratio"], c["app"], c["title"], flush=True)
exact = [c for c in cands if c["title"].lower() == "when the world underestimated her"]
print("  候选数:", len(cands), "| 精确命中:", bool(exact), flush=True)

print("=== 2) 剧名搜索已知剧（验证检索通道） ===", flush=True)
cands2 = Y.TOOL.search_by_name("The Bride He Couldn")
for c in cands2[:3]:
    print("  cand:", c["ratio"], c["app"], c["title"], c["playlet_id"], flush=True)
hit = next((c for c in cands2 if c["app"] == "yourchannel_drama" and c["ratio"] > 0.6), None)
print("  YC 命中:", hit, flush=True)

print("=== 3) 完整流程：获取+下载+校验+合成 ===", flush=True)
drama = Y.TOOL.fetch_playlet(hit["playlet_id"])
print("  title:", drama["title"], "| official:", drama["official_eps"], "| api eps:", len(drama["eps"]), flush=True)
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_test_out",
                   "the-bride-he-couldnt-let-go")
fails = Y.TOOL.download_eps(drama["eps"], out)
print("  下载失败:", fails, flush=True)
bad = Y.TOOL.verify_eps(out, drama["eps"])
print("  校验不通过:", bad, flush=True)
size = Y.TOOL.merge(out, drama["eps"], out + "-FULL.mp4")
print("  成片:", size // 1048576, "MB ->", out + "-FULL.mp4", flush=True)
ok = size > 100_000_000 and not fails and not bad
print("=== PIPELINE:", "PASS" if ok else "FAIL", "===", flush=True)
