# -*- coding: utf-8 -*-
"""
YC Drama Downloader —— YourChannel 短剧下载与合成工具（浏览器界面版）
=====================================================================
运行后自动打开浏览器控制台（本地 http://127.0.0.1:8765）。

功能：
  1. 输入 剧名 / 播放页URL / playlet_id
     - URL/ID：直连 YourChannel v3 API（主通道）
     - 剧名：北斗智影任务池（7 平台 × Top200）缓存模糊匹配
       （YourChannel 官网无按剧名搜索接口，此为唯一可行检索通道）
  2. 下载每集视频：优先 720p/1080p 原档 mp4，失败自动回落 m3u8 流
     （YC 剧集字幕为画面烧录式，随视频一并获取）
  3. 逐集校验 + 与官方集数比对，防漏集
  4. concat 无损合成全剧成片
  5. 日志实时显示、可导出；自动落盘 logs/ 目录

依赖：requests（其余全部为 Python 标准库 + 自带 ffmpeg）
"""
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import difflib
import webbrowser
import warnings
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

warnings.simplefilter("ignore")

# ---------------------------------------------------------------------------
API_BASE = "https://api.yourwebnovel.com/yourchannel/app"
PLAYLET_V3 = API_BASE + "/playlet/v3/{}?pageNo={}&pageSize=100"
BEIDOU_POOL = "https://api-scenter.inbeidou.cn/agent/v1/task/page"
BEIDOU_APPS = ["yourchannel_drama", "kalostv", "netshort", "stardusttv",
               "touchshort", "dramabox", "reelshort"]
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(APP_DIR, "beidou_pool_cache.json")
LOG_DIR = os.path.join(APP_DIR, "logs")
PORT = 8765

UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://yourchannel.ai/"}


def find_ffmpeg():
    if getattr(sys, "frozen", False):
        bundled = os.path.join(sys._MEIPASS, "ffmpeg.exe")
        if os.path.exists(bundled):
            return bundled
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    return shutil.which("ffmpeg")


FFMPEG = find_ffmpeg()
ENV_NO_PROXY = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}


def new_session():
    s = requests.Session()
    s.verify = False
    s.trust_env = False
    s.proxies = {"http": None, "https": None}
    s.headers.update(UA)
    return s


# ---------------------------------------------------------------------------
# 核心逻辑
# ---------------------------------------------------------------------------
class DramaTool:
    def __init__(self):
        self.cache = self._load_cache()

    def log(self, msg):
        LOGGER.info(str(msg))
        STATE.push_log(str(msg))

    def _load_cache(self):
        try:
            with open(CACHE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def save_cache(self):
        try:
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(self.cache, f, ensure_ascii=False)
        except Exception as e:
            self.log(f"[缓存] 写入失败: {e}")

    @staticmethod
    def parse_input(text):
        text = (text or "").strip()
        m = re.search(r"[?&]id=(\d+)", text)
        if not m:
            m = re.search(r"(\d{15,25})", text)
        if m:
            return m.group(1), None
        return None, text or None

    def fetch_playlet(self, pid):
        s = new_session()
        last_err = None
        for attempt in range(3):
            try:
                d = s.get(PLAYLET_V3.format(pid, 1), timeout=60).json()
                if not d.get("success"):
                    raise RuntimeError(d.get("message", "接口返回失败"))
                res = d["result"]
                playlet = res.get("playlet", {})
                ep = res.get("episodePage", {})
                eps = {}
                for r in ep.get("records", []):
                    eps[int(r["serialNo"])] = {"mp4": r.get("mp4Url"),
                                               "m3u8": r.get("m3u8Url")}
                page = 1
                while page < (ep.get("pages") or 1):
                    page += 1
                    d = s.get(PLAYLET_V3.format(pid, page), timeout=60).json()
                    for r in d["result"]["episodePage"].get("records", []):
                        eps[int(r["serialNo"])] = {"mp4": r.get("mp4Url"),
                                                   "m3u8": r.get("m3u8Url")}
                return {"id": pid,
                        "title": playlet.get("title") or f"playlet-{pid}",
                        "official_eps": playlet.get("episodeCount") or len(eps),
                        "eps": dict(sorted(eps.items()))}
            except Exception as e:
                last_err = e
                self.log(f"[重试] v3 接口第 {attempt+1} 次失败: {e}")
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"获取剧目失败: {last_err}")

    def refresh_pool(self):
        s = new_session()
        s.headers.update({"Referer": "https://inbeidou.cn/"})
        pool = {}
        for app in BEIDOU_APPS:
            items = []
            for page in (1, 2):
                p = {"page_num": page, "page_size": 100, "app_id": app,
                     "order_field": "total_income", "order_dir": "desc"}
                try:
                    r = s.get(BEIDOU_POOL, params=p, timeout=30)
                    d = ((r.json() or {}).get("body") or {}).get("data") or []
                except Exception as e:
                    self.log(f"[北斗] {app} 第 {page} 页拉取失败: {e}")
                    break
                items += [{"title": t.get("title"), "task_id": t.get("task_id"),
                           "third_serial_id": t.get("third_serial_id"),
                           "episode_count": t.get("episode_count")} for t in d]
                if not d:
                    break
            pool[app] = items
            self.log(f"[北斗] {app}: {len(items)} 条")
        self.cache = pool
        self.save_cache()
        return {a: len(v) for a, v in pool.items()}

    def search_by_name(self, keyword):
        if not self.cache:
            self.log("[搜索] 本地无缓存，自动拉取北斗任务池…")
            self.refresh_pool()
        kw = (keyword or "").lower().replace("’", "'").strip()
        cands = []
        for app, items in self.cache.items():
            for t in items:
                title = str(t.get("title") or "")
                ratio = difflib.SequenceMatcher(
                    None, kw, title.lower().replace("’", "'").strip()).ratio()
                if ratio > 0.4 or kw in title.lower():
                    cands.append({"ratio": round(ratio, 3), "app": app,
                                  "title": title, "task_id": t.get("task_id"),
                                  "playlet_id": str(t.get("third_serial_id")),
                                  "episode_count": t.get("episode_count")})
        cands.sort(key=lambda x: -x["ratio"])
        return cands[:15]

    def download_eps(self, eps, out_dir, progress_fn=None, workers=4):
        os.makedirs(out_dir, exist_ok=True)
        fails = []

        def dl_one(no, src):
            f = os.path.join(out_dir, f"ep{no:02d}.mp4").replace("\\", "/")
            if os.path.exists(f) and os.path.getsize(f) > 500_000:
                return no, "skip(已存在)"
            for tag, url in (("mp4", src.get("mp4")), ("m3u8", src.get("m3u8"))):
                if not url:
                    continue
                cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                       "-headers", "Referer: https://yourchannel.ai/\r\n"
                       "User-Agent: Mozilla/5.0\r\n",
                       "-i", url, "-c", "copy", "-bsf:a", "aac_adtstoasc",
                       "-movflags", "+faststart", f]
                try:
                    r = subprocess.run(cmd, capture_output=True, text=True,
                                       env=ENV_NO_PROXY, timeout=900)
                except subprocess.TimeoutExpired:
                    r = None
                if r is not None and r.returncode == 0 and \
                        os.path.exists(f) and os.path.getsize(f) > 500_000:
                    return no, f"ok[{tag}] {os.path.getsize(f)//1048576}MB"
                if os.path.exists(f):
                    os.remove(f)
            return no, "ERR"

        import concurrent.futures as cf
        done = 0
        with cf.ThreadPoolExecutor(workers) as ex:
            futs = {ex.submit(dl_one, no, src): no for no, src in eps.items()}
            for fut in cf.as_completed(futs):
                no, st = fut.result()
                done += 1
                if progress_fn:
                    progress_fn(no, st, done, len(eps))
                if st.startswith("ERR"):
                    fails.append(no)
        for no in list(fails):
            no2, st = dl_one(no, eps[no])
            if not st.startswith("ERR"):
                fails.remove(no)
                if progress_fn:
                    progress_fn(no2, st + "(重试成功)", done, len(eps))
        return sorted(fails)

    def verify_eps(self, out_dir, eps):
        bad = []
        for no in sorted(eps):
            f = os.path.join(out_dir, f"ep{no:02d}.mp4")
            if not os.path.exists(f) or os.path.getsize(f) < 500_000:
                bad.append(no)
                continue
            try:
                r = subprocess.run([FFMPEG, "-hide_banner", "-i", f],
                                   capture_output=True, text=True, timeout=60)
                if "Duration:" not in r.stderr:
                    bad.append(no)
            except Exception:
                bad.append(no)
        return bad

    def merge(self, out_dir, eps, full_path):
        lst = os.path.join(out_dir, "concat.txt")
        with open(lst, "w", encoding="utf-8") as fh:
            for no in sorted(eps):
                p = os.path.join(out_dir, f"ep{no:02d}.mp4").replace("\\", "/")
                fh.write(f"file '{p}'\n")
        cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
               "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy",
               "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", full_path]
        r = subprocess.run(cmd, capture_output=True, text=True, env=ENV_NO_PROXY,
                           timeout=3600)
        if os.path.exists(lst):
            os.remove(lst)
        if r.returncode != 0:
            raise RuntimeError("合成失败: " + (r.stderr or "")[-300:])
        return os.path.getsize(full_path)


# ---------------------------------------------------------------------------
# 全局状态（HTTP 服务与后台线程共享）
# ---------------------------------------------------------------------------
class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.logs = []
        self.busy = False
        self.status_msg = "空闲"
        self.current = None       # 当前剧 dict
        self.download_dir = None
        self.result = None        # 最近一次交付清单 dict

    def push_log(self, msg):
        with self.lock:
            self.logs.append(f"[{datetime.now():%H:%M:%S}] {msg}")
            self.logs = self.logs[-2000:]

    def snapshot(self):
        with self.lock:
            return {"busy": self.busy, "status": self.status_msg,
                    "logs": self.logs[-400:], "result": self.result,
                    "current": ({"title": self.current["title"],
                                 "official_eps": self.current["official_eps"],
                                 "eps": len(self.current["eps"])}
                                if self.current else None)}


STATE = State()
TOOL = DramaTool()

LOGGER = logging.getLogger("yc_tool")
os.makedirs(LOG_DIR, exist_ok=True)
_logfile = os.path.join(LOG_DIR, f"yc_tool_{datetime.now():%Y%m%d_%H%M%S}.log")
logging.basicConfig(filename=_logfile, level=logging.INFO,
                    format="%(asctime)s %(message)s", encoding="utf-8")
TOOL.log = lambda msg: (LOGGER.info(str(msg)), STATE.push_log(str(msg)))


def set_status(msg, busy=None):
    STATE.status_msg = msg
    if busy is not None:
        STATE.busy = busy


# ---------------------------------------------------------------------------
# 后台任务
# ---------------------------------------------------------------------------
def job_search(payload):
    set_status("搜索中…", busy=True)
    try:
        text = payload.get("text", "")
        pid, kw = DramaTool.parse_input(text)
        if pid:
            TOOL.log(f"[搜索] 识别为 playlet_id: {pid}")
            drama = TOOL.fetch_playlet(pid)
            STATE.current = drama
            STATE.download_dir = None
            TOOL.log(f"[结果] 《{drama['title']}》 官方 {drama['official_eps']} 集，"
                     f"API 返回 {len(drama['eps'])} 集")
            set_status(f"已就绪：《{drama['title']}》({len(drama['eps'])} 集) —— 可开始下载",
                       busy=False)
        elif kw:
            TOOL.log(f"[搜索] 按剧名模糊匹配: {kw}")
            cands = TOOL.search_by_name(kw)
            if not cands:
                TOOL.log("[结果] 未找到匹配剧目。")
                TOOL.log("  原因说明：YourChannel 官网不提供按剧名搜索的接口（已实测），")
                TOOL.log("  剧名匹配依赖北斗任务池缓存（7 平台 × 各 Top200，按收入排序）。")
                TOOL.log("  新上架或低热度剧可能不在池中。请到北斗任务详情页或")
                TOOL.log("  yourchannel.ai 播放页复制链接，直接粘贴到输入框重试。")
                set_status("搜索无结果（详见日志说明）", busy=False)
            else:
                STATE.result = {"type": "candidates", "candidates": cands}
                exact = [c for c in cands if c["app"] == "yourchannel_drama"
                         and c["ratio"] > 0.85]
                if len(cands) == 1 or exact:
                    c = exact[0] if exact else cands[0]
                    TOOL.log(f"[结果] 最佳匹配: 《{c['title']}》({c['app']}, "
                             f"匹配度 {c['ratio']}) playlet_id={c['playlet_id']}")
                    if c["app"] == "yourchannel_drama":
                        drama = TOOL.fetch_playlet(c["playlet_id"])
                        STATE.current = drama
                        STATE.download_dir = None
                        TOOL.log(f"[结果] 《{drama['title']}》 官方 "
                                 f"{drama['official_eps']} 集，API 返回 "
                                 f"{len(drama['eps'])} 集")
                        set_status(f"已就绪：《{drama['title']}》 —— 可开始下载",
                                   busy=False)
                        return
                set_status(f"找到 {len(cands)} 个候选（见下方列表）", busy=False)
                TOOL.log("[结果] 候选列表（请点击候选行的 playlet_id 复制，"
                         "或直接点「用此剧」）：")
                for c in cands[:10]:
                    TOOL.log(f"  [{c['ratio']}] {c['app']} | 《{c['title']}》"
                             f" {c['episode_count']}集 | id={c['playlet_id']}")
        else:
            set_status("输入为空", busy=False)
    except Exception as e:
        TOOL.log(f"[错误] 搜索失败: {e}")
        set_status(f"搜索失败: {e}", busy=False)


def job_fetch(pid):
    set_status(f"获取剧目 {pid} …", busy=True)
    try:
        drama = TOOL.fetch_playlet(pid)
        STATE.current = drama
        STATE.download_dir = None
        TOOL.log(f"[结果] 《{drama['title']}》 官方 {drama['official_eps']} 集，"
                 f"API 返回 {len(drama['eps'])} 集")
        set_status(f"已就绪：《{drama['title']}》 —— 可开始下载", busy=False)
    except Exception as e:
        TOOL.log(f"[错误] {e}")
        set_status(f"获取失败: {e}", busy=False)


def job_download(payload):
    drama = STATE.current
    if not drama:
        set_status("请先搜索选定剧目", busy=False)
        return
    out_root = payload.get("out") or os.path.join(os.path.expanduser("~"), "Desktop")
    slug = re.sub(r"[^a-z0-9]+", "-", drama["title"].lower()).strip("-")
    out = os.path.join(out_root, slug)
    os.makedirs(out, exist_ok=True)
    STATE.download_dir = out
    set_status(f"下载中：《{drama['title']}》 {len(drama['eps'])} 集", busy=True)
    TOOL.log(f"[下载] 《{drama['title']}》 -> {out}")

    def prog(no, st, done, total):
        STATE.status_msg = f"下载中 {done}/{total}（当前 ep{no:02d}）"
        TOOL.log(f"  ep{no:02d} {st}  ({done}/{total})")

    try:
        fails = TOOL.download_eps(drama["eps"], out, prog)
        if fails:
            TOOL.log(f"[警告] 下载失败 {len(fails)} 集: {fails}")
        bad = TOOL.verify_eps(out, drama["eps"])
        if bad:
            TOOL.log(f"[警告] 校验未通过（缺文件/时长异常）: {bad}")
        else:
            TOOL.log(f"[校验] {len(drama['eps'])} 集全部通过（体积、时长、可解码）")
        official = drama["official_eps"]
        got = len(drama["eps"]) - len(set(fails) | set(bad))
        note = "数量一致，无漏集" if got == official else "数量不一致，请查看日志!"
        TOOL.log(f"[完整性] 官方 {official} 集 / 成功 {got} 集 —— {note}")
        TOOL.log("[字幕] YourChannel 剧集字幕为画面烧录式，已随视频获取；"
                 "片源无独立字幕文件")
        STATE.result = {"type": "download",
                        "title": drama["title"], "out": out,
                        "official": official, "got": got,
                        "fails": fails, "bad": bad}
        set_status(f"下载完成 {got}/{official} 集 -> {out}", busy=False)
    except Exception as e:
        TOOL.log(f"[错误] 下载失败: {e}")
        set_status(f"下载失败: {e}", busy=False)


def job_merge():
    drama = STATE.current
    out = STATE.download_dir
    if not drama or not out or not os.path.isdir(out):
        set_status("请先完成下载", busy=False)
        return
    set_status("合成中（无损拼接，不重编码）…", busy=True)
    full = out.rstrip("\\/") + "-FULL.mp4"
    try:
        size = TOOL.merge(out, drama["eps"], full)
        TOOL.log(f"[合成] 成片: {full} ({size//1048576}MB)")
        r = subprocess.run([FFMPEG, "-hide_banner", "-i", full],
                           capture_output=True, text=True, timeout=120)
        m = re.search(r"Duration: (\S+)", r.stderr)
        TOOL.log(f"[合成] 成片时长: {m.group(1) if m else '?'}")
        STATE.result = {"type": "merged", "title": drama["title"],
                        "out": out, "full": full,
                        "size_mb": size // 1048576,
                        "duration": m.group(1) if m else "?"}
        set_status(f"合成完成: {full}", busy=False)
        TOOL.log("[完成] 交付清单：")
        TOOL.log(f"  分集目录: {out}")
        TOOL.log(f"  合成成片: {full}")
        TOOL.log(f"  运行日志: {_logfile}（界面可一键导出）")
    except Exception as e:
        TOOL.log(f"[错误] 合成失败: {e}")
        set_status(f"合成失败: {e}", busy=False)


# ---------------------------------------------------------------------------
# HTTP 服务
# ---------------------------------------------------------------------------
PAGE = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>YC Drama Downloader</title>
<style>
body{font-family:"Microsoft YaHei",sans-serif;max-width:960px;margin:24px auto;padding:0 16px;background:#12141a;color:#d8dce6}
h1{font-size:20px;color:#7ab8ff} .row{display:flex;gap:8px;margin:8px 0}
input[type=text]{flex:1;padding:8px;border:1px solid #3a4152;border-radius:6px;background:#1b1f2a;color:#d8dce6}
button{padding:8px 16px;border:0;border-radius:6px;background:#2f6fed;color:#fff;cursor:pointer}
button:disabled{opacity:.5;cursor:default}
button.gray{background:#3a4152} button.green{background:#2e9e5b}
#bar{height:8px;background:#232838;border-radius:4px;overflow:hidden;margin:8px 0}
#bar>div{height:100%;background:#2f6fed;width:0%}
table{width:100%;border-collapse:collapse;margin:8px 0;font-size:13px}
th,td{border:1px solid #2a3040;padding:5px 8px;text-align:left}
th{background:#1b2130;color:#9fb6d9}
#log{background:#0d0f15;border:1px solid #2a3040;border-radius:6px;height:300px;overflow-y:auto;padding:8px;font:12px Consolas,monospace;white-space:pre-wrap}
.st{color:#8ea2bd;font-size:13px;margin:6px 0}
.ok{color:#4fd07a} .warn{color:#ffb14f} .err{color:#ff6b6b}
</style></head><body>
<h1>🎬 YC Drama Downloader <small style="font-size:12px;color:#8ea2bd">YourChannel 短剧下载与合成</small></h1>
<div class="row">
  <input id="q" type="text" placeholder="剧名（模糊匹配北斗任务池） 或 播放页URL / playlet_id（直连）">
  <button onclick="doSearch()">搜索</button>
  <button class="gray" onclick="doRefresh()">刷新北斗任务池</button>
</div>
<div class="row">
  <input id="out" type="text" style="flex:2" placeholder="输出目录">
  <button class="gray" onclick="pickDir()">浏览…</button>
</div>
<div class="row">
  <button class="green" id="bdl" onclick="doDownload()">开始下载</button>
  <button class="green" id="bmg" onclick="doMerge()">合成为完整视频</button>
  <button class="gray" id="bex" onclick="doExport()">导出日志</button>
  <button class="gray" onclick="openDir()">打开输出目录</button>
</div>
<div id="bar"><div id="barin"></div></div>
<div class="st" id="st">初始化…</div>
<table id="cands" style="display:none"><thead><tr><th>匹配度</th><th>平台</th><th>剧名</th><th>集数</th><th>playlet_id</th><th></th></tr></thead><tbody></tbody></table>
<div id="log"></div>
<p style="color:#66708a;font-size:12px">提示：YourChannel 官网无按名搜索接口，剧名搜索走北斗任务池（各平台 Top200）；
新剧请直接粘贴播放页链接（yourchannel.ai/pagesSub/player?id=…）。YC 剧集字幕为画面烧录式，随视频获取。</p>
<script>
let timer=null;
function fmt(){return (new Date).toLocaleTimeString('zh-CN',{hour12:false})}
async function post(url,data){const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data||{})});return r.json()}
async function doSearch(){const t=document.getElementById('q').value;if(!t)return alert('请输入剧名、URL 或 id');await post('/api/search',{text:t})}
async function doRefresh(){await post('/api/refresh',{})}
async function doDownload(){await post('/api/download',{out:document.getElementById('out').value})}
async function doMerge(){await post('/api/merge',{})}
async function doExport(){const d=await post('/api/export_log',{});if(d.path)alert('日志已导出:\\n'+d.path)}
function openDir(){post('/api/open_dir',{out:document.getElementById('out').value})}
function usePid(el){post('/api/fetch',{id:el.getAttribute('data-pid')})}
async function poll(){
  try{const r=await fetch('/api/status');const s=await r.json();
  document.getElementById('st').textContent=s.status;
  document.getElementById('bdl').disabled=s.busy;document.getElementById('bmg').disabled=s.busy;
  const log=document.getElementById('log');
  const txt=(s.logs||[]).join('\\n');
  if(log.dataset.n!==String((s.logs||[]).length)){log.textContent=txt;log.scrollTop=log.scrollHeight;log.dataset.n=String((s.logs||[]).length)}
  const m=s.status.match(/(\\d+)\\/(\\d+)/);if(m&&s.busy)document.getElementById('barin').style.width=(100*m[1]/m[2])+'%';
  else if(!s.busy)document.getElementById('barin').style.width='100%';
  const tb=document.querySelector('#cands tbody');
  if(s.result&&s.result.type==='candidates'){
    document.getElementById('cands').style.display='';
    if(tb.dataset.n!==String(s.result.candidates.length)){
      tb.innerHTML='';tb.dataset.n=String(s.result.candidates.length);
      for(const c of s.result.candidates){
        const tr=document.createElement('tr');
        tr.innerHTML=`<td>${c.ratio}</td><td>${c.app}</td><td>${c.title}</td><td>${c.episode_count||''}</td><td>${c.playlet_id||''}</td><td>${c.app==='yourchannel_drama'?'<button onclick=usePid(this) data-pid="'+c.playlet_id+'">用此剧</button>':'<span style="color:#66708a">非YC源</span>'}</td>`;
        tb.appendChild(tr)}}
  } else {document.getElementById('cands').style.display='none';tb.dataset.n='0'}
  }catch(e){}
}
timer=setInterval(poll,1200);poll();
async function init(){const r=await fetch('/api/init');const d=await r.json();
  document.getElementById('out').value=d.default_out||'';
  if(d.logfile)document.getElementById('st').textContent='日志: '+d.logfile}
init();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            b = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        elif self.path == "/api/status":
            self._json(STATE.snapshot())
        elif self.path == "/api/init":
            self._json({"default_out": DEFAULT_OUT, "logfile": _logfile})
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        payload = {}
        if n:
            try:
                payload = json.loads(self.rfile.read(n).decode("utf-8"))
            except Exception:
                payload = {}
        if STATE.busy and self.path not in ("/api/open_dir",):
            self._json({"ok": False, "msg": "busy"})
            return
        if self.path == "/api/search":
            threading.Thread(target=job_search, args=(payload,), daemon=True).start()
            self._json({"ok": True})
        elif self.path == "/api/fetch":
            threading.Thread(target=job_fetch, args=(payload.get("id"),),
                             daemon=True).start()
            self._json({"ok": True})
        elif self.path == "/api/download":
            threading.Thread(target=job_download, args=(payload,),
                             daemon=True).start()
            self._json({"ok": True})
        elif self.path == "/api/merge":
            threading.Thread(target=job_merge, daemon=True).start()
            self._json({"ok": True})
        elif self.path == "/api/refresh":
            def work():
                set_status("刷新北斗任务池…", busy=True)
                try:
                    sizes = TOOL.refresh_pool()
                    TOOL.log(f"[缓存] 已刷新: {sizes}")
                    set_status("任务池已刷新", busy=False)
                except Exception as e:
                    set_status(f"刷新失败: {e}", busy=False)
            threading.Thread(target=work, daemon=True).start()
            self._json({"ok": True})
        elif self.path == "/api/export_log":
            p = payload.get("path") or os.path.join(
                os.path.expanduser("~"), "Desktop",
                f"yc_tool_日志_{datetime.now():%Y%m%d_%H%M%S}.txt")
            try:
                with open(p, "w", encoding="utf-8") as f:
                    f.write("\n".join(STATE.logs))
                TOOL.log(f"[日志] 已导出到 {p}")
                self._json({"ok": True, "path": p})
            except Exception as e:
                self._json({"ok": False, "msg": str(e)})
        elif self.path == "/api/open_dir":
            d = payload.get("out") or STATE.download_dir or DEFAULT_OUT
            if os.path.isdir(d):
                os.startfile(d)
            self._json({"ok": True})
        else:
            self.send_response(404)
            self.end_headers()


def pick_default_out():
    for c in (os.path.join(os.path.expanduser("~"), "Desktop"),
              os.path.expanduser("~"), APP_DIR):
        if os.path.isdir(c):
            return c
    return APP_DIR


DEFAULT_OUT = pick_default_out()


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"YC Drama Downloader 运行中: http://127.0.0.1:{PORT}")
    print(f"ffmpeg: {FFMPEG}")
    print(f"日志文件: {_logfile}")
    threading.Timer(0.8, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
