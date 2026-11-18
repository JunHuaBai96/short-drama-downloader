# -*- coding: utf-8 -*-
"""v1.2.0 UI/端点级验证：按钮存在性、实时检索、结果清除、日志清空"""
import json, time, urllib.request

BASE = "http://127.0.0.1:8765"
def get(u):
    with urllib.request.urlopen(BASE + u, timeout=30) as r:
        return r.read().decode("utf-8", "ignore")
def post(u, d=None):
    req = urllib.request.Request(BASE + u, data=json.dumps(d or {}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())

RESULTS = []
def check(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""), flush=True)

page = get("/")
check("UI-移除手动刷新入口", "刷新北斗任务池" not in page)
check("UI-清除结果按钮", "doClearResult" in page and "清除结果" in page)
check("UI-清空日志按钮", "doClearLog" in page and "清空日志" in page)
check("UI-输出目录浏览+标注", page.count("pickDir(") >= 2 and "FULL.mp4 均保存在此" in page)
check("UI-实时检索提示", "实时全局检索" in page)

# 实时检索：先清任务池再搜索（证明不走缓存）
post("/api/clear_pool")
time.sleep(1.5)
post("/api/search", {"text": "Mafia Boss: Spoiling His Little Princess"})
for _ in range(60):
    time.sleep(1)
    s = json.loads(get("/api/status"))
    if not s["busy"]:
        break
check("实时检索-日志说明", any("实时检索" in l or "已拉取" in l for l in s["logs"]),
      next((l for l in s["logs"] if "已拉取" in l), ""))
check("实时检索-命中", s["status"].find("已就绪") >= 0, s["status"])

# 任务链接定位 + 候选表清除
post("/api/clear_result")
time.sleep(0.5)
s0 = json.loads(get("/api/status"))
s0["result"] = {"type": "candidates"}  # 模拟残留
post("/api/search", {"text": "https://inbeidou.cn/task-detail?task_id=1093640043&app_id=yourchannel_drama&task_type=1"})
for _ in range(60):
    time.sleep(1)
    s = json.loads(get("/api/status"))
    if not s["busy"]:
        break
check("任务链接定位", s["status"].find("The Heir Who Wasn’t Mine") >= 0, s["status"])
check("候选表已清除(直连后 result=None)", s["result"] is None)

# 清空日志
n_before = len(s["logs"])
post("/api/clear_log")
time.sleep(0.5)
s2 = json.loads(get("/api/status"))
check("清空日志", len(s2["logs"]) <= 1, f"{n_before} 条 → {len(s2['logs'])} 条")

print("=== UI-TESTS:", "ALL PASS" if all(RESULTS) else "FAIL", "===", flush=True)
