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
import tempfile
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
               "touchshort", "dramabox", "reelshort", "dramawave"]

# 多平台剧场识别（来源：短剧上新监控面板 11 平台清单）
PLATFORMS = {
    "yourchannel.ai": ("yc", "YourChannel"),
    "netshort.com": ("netshort", "NetShort"),
    "shorttv.live": ("shortmax", "ShortMax"),
    "dramabox.com": ("dramabox", "DramaBox"),
    "reelshort.com": ("reelshort", "ReelShort"),
    "stardusttv.net": ("stardusttv", "StardustTV"),
    "kalostv.com": ("kalostv", "KalosTV"),
    "goodshort.com": ("goodshort", "GoodShort"),
    "flickreels.net": ("flickreels", "FlickReels"),
    "moboreels.com": ("moboreels", "MoboReels"),
    "storyreel.life": ("storyreel", "StoryReel"),
    "narto-drama.com": ("narto", "Narto Drama"),
    "nartodrama.com": ("narto", "Narto Drama"),
}
DOWNLOAD_SUPPORTED = {"yc", "netshort", "narto"}
PANEL_URL = "https://e6efc0c913ac4c26b646abfbb6e6b0e6.app.codebuddy.work/"


def detect_platform(text):
    """从 URL 识别所属剧场，返回 (key, name) 或 (None, None)"""
    t = (text or "").lower()
    for domain, (key, name) in PLATFORMS.items():
        if domain in t:
            return key, name
    return None, None


NS_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def find_edge():
    for p in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
        if os.path.exists(p):
            return p
    return shutil.which("msedge")


class EdgeSession:
    """用本机 Edge（headless）通过 CDP 抓取播放器实际请求的媒体直链"""

    def __init__(self, port=9333):
        self.port = port
        self.proc = None
        self.profile = None

    def start(self):
        edge = find_edge()
        if not edge:
            raise RuntimeError("未找到 Microsoft Edge，无法解析 NetShort 播放地址")
        self.profile = tempfile.mkdtemp(prefix="sdd_edge_")
        self.proc = subprocess.Popen([
            edge, "--headless=new", f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.profile}", "--no-first-run",
            "--no-default-browser-check", "--disable-gpu", "--mute-audio",
            "--autoplay-policy=no-user-gesture-required", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(80):
            try:
                requests.get(f"http://127.0.0.1:{self.port}/json/version", timeout=1)
                return
            except Exception:
                time.sleep(0.5)
        raise RuntimeError("Edge 启动超时")

    def grab(self, page_url, timeout=35):
        """打开页面并返回第一个签名视频直链（硬超时，防卡死）"""
        import threading as _th

        box = {}

        def work():
            try:
                box["url"] = self._grab_inner(page_url, timeout - 5)
            except Exception as e:
                box["err"] = str(e)

        th = _th.Thread(target=work, daemon=True)
        th.start()
        th.join(timeout)
        return box.get("url")

    def _grab_inner(self, page_url, timeout=30):
        import websocket
        tgt = requests.put(
            f"http://127.0.0.1:{self.port}/json/new?about:blank", timeout=10).json()
        ws = websocket.create_connection(tgt["webSocketDebuggerUrl"], timeout=30)
        try:
            ws.send(json.dumps({"id": 1, "method": "Network.enable", "params": {}}))
            ws.send(json.dumps({"id": 2, "method": "Page.enable", "params": {}}))
            ws.send(json.dumps({"id": 3, "method": "Page.navigate",
                                "params": {"url": page_url}}))
            end = time.time() + timeout
            ws.settimeout(5)
            while time.time() < end:
                try:
                    msg = json.loads(ws.recv())
                except Exception:
                    continue
                if msg.get("method") == "Network.requestWillBeSent":
                    u = msg["params"]["request"]["url"]
                    if "auth_key" in u and (".mp4" in u or "cfcdn" in u
                                            or "videocdn" in u):
                        return u
            return None
        finally:
            try:
                ws.close()
            except Exception:
                pass
            try:
                requests.get(
                    f"http://127.0.0.1:{self.port}/json/close/{tgt['id']}", timeout=3)
            except Exception:
                pass

    def close(self):
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except Exception:
                self.proc.kill()
        if self.profile:
            shutil.rmtree(self.profile, ignore_errors=True)


def netshort_resolve(url):
    """解析 NetShort 播放页 → 剧名 + 全部分集播放页链接（SSR 即可枚举）"""
    m = re.search(r"/episode/([^/?#]+)", url)
    if not m:
        raise RuntimeError("未能识别 NetShort 播放页链接（应形如 "
                           "web.netshort.com/episode/<slug>-<id>）")
    slug = re.sub(r"-ep-\d+$", "", m.group(1))
    base = f"https://web.netshort.com/episode/{slug}"
    r = requests.get(base, headers={"User-Agent": NS_UA,
                                    "Referer": "https://web.netshort.com/"},
                     timeout=40)
    if r.status_code != 200:
        raise RuntimeError(f"NetShort 页面打开失败 HTTP {r.status_code}")
    html = r.text
    title = ""
    tm = re.search(r"<title>(.*?)</title>", html, re.S)
    if tm:
        title = re.split(r"\s*(Full Episodes|Watch Online|\|)", tm.group(1))[0].strip()
    nums = {int(x) for x in re.findall(re.escape(slug) + r"-ep-(\d+)", html)}
    total = max(nums) if nums else 1
    eps = {1: {"page": base}}
    for n in range(2, total + 1):
        eps[n] = {"page": f"{base}-ep-{n}"}
    return {"id": slug, "title": title or slug, "official_eps": total,
            "platform": "netshort", "platform_name": "NetShort", "eps": eps}


def netshort_download(eps, out_dir, log=None, progress=None):
    """逐集通过 Edge 抓签名直链 + ffmpeg 无损转存"""
    os.makedirs(out_dir, exist_ok=True)
    fails = []
    session = EdgeSession()
    session.start()
    try:
        total = len(eps)
        for i, (no, src) in enumerate(sorted(eps.items()), 1):
            f = os.path.join(out_dir, f"ep{no:02d}.mp4")
            if os.path.exists(f) and os.path.getsize(f) > 500_000:
                if progress:
                    progress(no, "skip(已存在)", i, total)
                continue
            try:
                media = session.grab(src["page"])
                if not media:
                    fails.append(no)
                    if progress:
                        progress(no, "ERR 未取到播放地址", i, total)
                    continue
                cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                       "-headers", "Referer: https://web.netshort.com/\r\n"
                       f"User-Agent: {NS_UA}\r\n",
                       "-i", media, "-c", "copy", "-bsf:a", "aac_adtstoasc",
                       "-movflags", "+faststart", f]
                r = subprocess.run(cmd, capture_output=True, text=True,
                                   env=ENV_NO_PROXY, timeout=300)
                if r.returncode == 0 and os.path.exists(f) and \
                        os.path.getsize(f) > 500_000:
                    if progress:
                        progress(no, f"ok {os.path.getsize(f)//1048576}MB", i, total)
                else:
                    fails.append(no)
                    if progress:
                        progress(no, "ERR 转存失败", i, total)
            except Exception as e:
                fails.append(no)
                if progress:
                    progress(no, f"ERR {e}", i, total)
    finally:
        session.close()
    return fails


# ---------------------------------------------------------------------------
# Narto Drama（多剧场聚合站，含 BibiShort 等 35+ 片源）——直链通道，无需浏览器
# ---------------------------------------------------------------------------
NARTO_MAIN = "https://narto-drama.com"
NARTO_EDGE = "https://nartodrama.com"          # refresh-source 走此边缘域
NARTO_UA = NS_UA
NARTO_LANG = "zh-TW"


def _nd_ck():
    """复刻站点 JS 浏览器检查 Cookie：Date.now().toString(36)+随机 10 位"""
    import random
    import string
    return (format(int(time.time() * 1000), "x")
            + "".join(random.choices(string.ascii_lowercase + string.digits, k=10)))


def narto_session():
    s = new_session()
    ck = _nd_ck()
    for dom in ("narto-drama.com", "nartodrama.com"):
        s.cookies.set("nd_ck", ck, domain=dom)
    s.headers.update({"User-Agent": NARTO_UA,
                      "Referer": NARTO_MAIN + "/"})
    return s


def narto_sections(sess, provider, page=1):
    """实时拉取某片源剧场列表（for-you + feed-stream，每页 15 条）"""
    params = {"provider": provider, "lang": NARTO_LANG}
    if page > 1:
        params["tab_pages[for-you]"] = str(page)
        params["tab_pages[feed-stream]"] = str(page)
    r = sess.get(NARTO_MAIN + "/home/providers/sections", params=params,
                 timeout=30)
    d = r.json() if r.headers.get("Content-Type", "").startswith("application/json") else {}
    items = []
    for sec in d.get("sections") or []:
        items += sec.get("items") or []
    return items


def narto_provider_keys(sess):
    try:
        r = sess.get(NARTO_MAIN + "/home/providers/sections",
                     params={"lang": NARTO_LANG}, timeout=30)
        return [p.get("key") for p in (r.json().get("providers") or []) if p.get("key")]
    except Exception:
        return ["bibishort"]


def narto_resolve(url):
    """解析 Narto 播放页（/detail/watch/<slug> 或 /search/import?…）→
    剧名 + 全部分集（分集直链在下载时经 refresh-source 实时获取）"""
    sess = narto_session()
    url = (url or "").strip()
    if "search/import" in url:
        r = sess.get(url, timeout=40, allow_redirects=True)
        url = r.url
    m = re.search(r"/detail/watch/([a-z0-9-]+)", url)
    if not m:
        raise RuntimeError("未能识别 Narto Drama 播放页链接（应形如 "
                           "narto-drama.com/detail/watch/<slug>）")
    slug = m.group(1)
    page_url = f"{NARTO_MAIN}/detail/watch/{slug}?lang={NARTO_LANG}"
    r = sess.get(page_url, timeout=40)
    if r.status_code != 200:
        raise RuntimeError(f"Narto Drama 页面打开失败 HTTP {r.status_code}")
    html = r.text
    title = ""
    tm = re.search(r"<title>(.*?)</title>", html, re.S)
    if tm:
        title = re.sub(r"\s*-\s*免費觀看.*$", "", tm.group(1)).strip()
    nums = {int(x) for x in
            re.findall(re.escape(slug) + r"/(\d+)\?", html)}
    total = max(nums) if nums else 1
    eps = {no: {"slug": slug} for no in range(1, total + 1)}
    return {"id": slug, "title": title or slug, "official_eps": total,
            "platform": "narto", "platform_name": "Narto Drama",
            "watch_url": page_url, "eps": eps}


def narto_play_url(slug, no, sess=None):
    """经边缘域 refresh-source 端点实时取分集 MP4 直链（免登录，含 3 次重试）"""
    sess = sess or narto_session()
    u = (f"{NARTO_EDGE}/e/rs/detail/watch/{slug}/{no}/refresh-source"
         f"?lang={NARTO_LANG}")
    last_err = None
    for attempt in range(3):
        try:
            r = sess.get(u, headers={"Accept": "application/json"}, timeout=40)
            d = r.json()
            if d.get("ok") and d.get("play_url"):
                return d["play_url"]
            last_err = RuntimeError(d.get("message") or "未返回播放地址")
        except Exception as e:
            last_err = e
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"refresh-source 第 {no} 集失败: {last_err}")


def narto_download(eps, out_dir, log=None, progress=None):
    """逐集实时解析直链 + ffmpeg 无损转存（4 并发）"""
    os.makedirs(out_dir, exist_ok=True)
    fails = []
    sess = narto_session()
    slugs = {no: src.get("slug") for no, src in eps.items()}

    def resolve_one(no):
        try:
            return no, narto_play_url(slugs[no], no, sess)
        except Exception as e:
            return no, None

    import concurrent.futures as cf
    urls = {}
    total = len(eps)
    with cf.ThreadPoolExecutor(4) as ex:
        for no, u in ex.map(resolve_one, sorted(eps)):
            urls[no] = u
            if not u:
                fails.append(no)
                if progress:
                    progress(no, "ERR 未取到播放地址", len(urls), total)
    if log:
        log(f"[Narto] 已解析 {total - len(fails)}/{total} 集直链，开始无损转存")

    dl_eps = {no: {"mp4": u} for no, u in urls.items() if u}
    fails2 = DramaTool().download_eps(dl_eps, out_dir, progress, workers=4)
    return sorted(set(fails) | set(fails2))


def narto_search_titles(keyword, limit=24):
    """免登录站内搜索：GET /search?q=<kw>&lang=zh-TW（SSR 渲染，JSON-LD 清单）。
    返回 [(watch_url, title), ...]。注意：sections API 的 q 参数与
    /search/providers/retry 均需登录（实测 login_required:true），此通道不需。"""
    sess = narto_session()
    r = sess.get(NARTO_MAIN + "/search",
                 params={"q": keyword, "lang": NARTO_LANG}, timeout=40)
    items = re.findall(r'"@type":"ListItem","position":\d+,'
                       r'"url":"([^"]+)","name":"([^"]+)"', r.text)
    seen, out = set(), []
    for u, title in items:
        slug = u.split("/detail/watch/")[1].split("?")[0]
        if slug not in seen:
            seen.add(slug)
            out.append((f"{NARTO_MAIN}/detail/watch/{slug}?lang={NARTO_LANG}",
                        title))
    return out[:limit]


def narto_locate_by_title(title, log=None):
    """按剧名在 Narto Drama 定位片源并解析播放页（供非 YC 源任务复用）"""
    hits = narto_search_titles(title)
    if not hits:
        raise RuntimeError("站内搜索无结果")
    kw = (title or "").lower().replace("’", "'").strip()
    best, best_ratio = None, 0.0
    for u, t in hits:
        ratio = difflib.SequenceMatcher(
            None, kw, t.lower().replace("’", "'").strip()).ratio()
        if ratio > best_ratio:
            best, best_ratio = (u, t), ratio
    if best_ratio < 0.55:
        raise RuntimeError(f"最接近的《{best[1]}》匹配度仅 {best_ratio:.2f}，"
                           f"未达自动定位阈值")
    if log:
        log(f"[Narto] 片源定位命中：《{best[1]}》（匹配度 {best_ratio:.3f}）")
    return narto_resolve(best[0])


def panel_index():
    """短剧上新监控面板：11 平台剧名索引（用于跨平台识别）"""
    r = requests.get(PANEL_URL, headers={"User-Agent": NS_UA}, timeout=40)
    html = r.text
    out = []
    for seg in html.split('<div class="title-area"')[1:]:
        m = re.search(r'class="name">([^<]+)</span>', seg)
        if not m:
            continue
        name = m.group(1).strip()
        titles = re.findall(r'<div class="title">([^<]+)</div>', seg)
        date = re.findall(r'class="date drama-date[^"]*">([^<]+)</div>', seg)
        for i, t in enumerate(titles):
            out.append({"platform": name, "title": t.strip(),
                        "date": date[i].strip() if i < len(date) else ""})
    return out
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(APP_DIR, "beidou_pool_cache.json")


def pick_folder_dialog():
    """弹出 Windows 文件夹选择对话框（PowerShell FolderBrowserDialog）"""
    script = ("Add-Type -AssemblyName System.Windows.Forms; "
              "$f = New-Object System.Windows.Forms.FolderBrowserDialog; "
              "$f.Description = '选择文件夹'; "
              "if ($f.ShowDialog() -eq 'OK') { $f.SelectedPath } "
              "else { '' }")
    r = subprocess.run(["powershell", "-STA", "-NoProfile", "-Command", script],
                       capture_output=True, text=True, timeout=180)
    p = (r.stdout or "").strip()
    return p if p and os.path.isdir(p) else None
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

    @staticmethod
    def parse_task_link(text):
        """识别北斗任务详情链接：inbeidou.cn/task-detail?task_id=…&app_id=…"""
        text = (text or "").strip()
        if "inbeidou.cn" not in text and "task_id" not in text:
            return None
        m = re.search(r"task_id=(\d+)", text)
        if not m:
            return None
        app = re.search(r"app_id=([\w-]+)", text)
        return m.group(1), app.group(1) if app else None

    def fetch_task(self, task_id, app_id=None):
        """按 task_id 从北斗任务池取任务记录（不区分平台）"""
        s = new_session()
        s.headers.update({"Referer": "https://inbeidou.cn/"})
        p = {"task_id": task_id, "page_num": 1, "page_size": 10}
        if app_id:
            p["app_id"] = app_id
        r = s.get(BEIDOU_POOL, params=p, timeout=30)
        data = ((r.json() or {}).get("body") or {}).get("data") or []
        if not data:
            raise RuntimeError(f"北斗任务池未找到 task_id={task_id}")
        return data[0]

    def resolve_task(self, task_id, app_id=None):
        """按 task_id 从北斗任务池定位任务：
        YC 源 → 直连 YC v3 API；非 YC 源 → Narto Drama 片源按剧名定位"""
        t = self.fetch_task(task_id, app_id)
        pid = str(t.get("third_serial_id") or "")
        title = str(t.get("title") or "")
        self.log(f"[任务链接] task_id={task_id} → 《{title}》 "
                 f"{t.get('episode_count')}集 playlet_id={pid or '缺失'}")
        if app_id and app_id != "yourchannel_drama":
            self.log(f"[提示] 该任务来自 {app_id}（非 YC 源），"
                     f"尝试 Narto Drama 多剧场片源定位…")
            try:
                drama = narto_locate_by_title(title, self.log)
            except Exception as e:
                raise RuntimeError(
                    f"任务 {task_id} 来自 {app_id}（非 YC 源），任务信息："
                    f"《{title}》{t.get('episode_count')} 集；未能从 Narto "
                    f"Drama 片源定位（{e}）。可到 narto-drama.com 搜索该剧，"
                    f"复制播放页链接后粘贴重试。")
            drama["task_id"] = t.get("task_id")
            drama["source_app"] = app_id
            return drama
        drama = self.fetch_playlet(pid)
        drama["task_id"] = t.get("task_id")
        return drama

    def clear_pool(self):
        """清除本地任务池缓存（文件一并删除，下次搜索自动重新拉取）"""
        self.cache = {}
        try:
            if os.path.exists(CACHE_FILE):
                os.remove(CACHE_FILE)
                self.log("[缓存] 缓存文件已删除")
        except Exception as e:
            self.log(f"[缓存] 删除缓存文件失败: {e}")
        self.log("[缓存] 任务池已清除（下次搜索将自动重新拉取）")

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

    def search_global(self, keyword, pages_per_app=2):
        """实时全局检索：绕过本地缓存，实时拉取北斗各平台全量窗口。

        背景（实测）：YourChannel 官网搜索接口已停用（任何参数均返回
        "短剧未找到"，包括在库剧名与 playlet_id），北斗智影任务池是唯一
        可检索数据源；每平台仅暴露 200 条（收入排序，平台硬限制）。
        检索后自动更新本地缓存兜底。
        """
        s = new_session()
        s.headers.update({"Referer": "https://inbeidou.cn/"})
        pool = {}
        for app in BEIDOU_APPS:
            items = []
            for page in range(1, pages_per_app + 1):
                p = {"page_num": page, "page_size": 100, "app_id": app,
                     "order_field": "total_income", "order_dir": "desc"}
                try:
                    r = s.get(BEIDOU_POOL, params=p, timeout=30)
                    d = ((r.json() or {}).get("body") or {}).get("data") or []
                except Exception as e:
                    self.log(f"[实时检索] {app} 第 {page} 页失败: {e}")
                    break
                items += d
                if len(d) < 100:
                    break
            pool[app] = items
        total = sum(len(v) for v in pool.values())
        if total == 0:
            raise RuntimeError("北斗任务池实时拉取失败（0 条）")
        self.log(f"[实时检索] 已拉取 {len(pool)} 平台共 {total} 条（未用本地缓存）")
        self.cache = pool
        self.save_cache()
        kw = (keyword or "").lower().replace("’", "'").strip()
        cands = []
        for app, items in pool.items():
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

    _panel_cache = None
    _panel_at = 0

    def search_panel(self, keyword, limit=8):
        """跨平台识别：短剧上新监控面板 11 平台索引（仅剧名+日期，无播放 ID）"""
        now = time.time()
        if not DramaTool._panel_cache or now - DramaTool._panel_at > 1800:
            DramaTool._panel_cache = panel_index()
            DramaTool._panel_at = now
        kw = (keyword or "").lower().replace("’", "'").strip()
        hits = []
        for it in DramaTool._panel_cache:
            t = it["title"].lower().replace("’", "'")
            ratio = difflib.SequenceMatcher(None, kw, t).ratio()
            if kw in t or ratio > 0.75:
                hits.append({"ratio": round(ratio, 3), "app": it["platform"] + "(面板)",
                             "title": it["title"], "task_id": None,
                             "playlet_id": "", "date": it["date"]})
        hits.sort(key=lambda x: -x["ratio"])
        return hits[:limit]

    def search_narto(self, keyword, limit=8):
        """实时检索 Narto Drama 片源（免登录 /search?q= 通道）。
        返回候选（含 watch_url，可直接定位播放页）。"""
        hits = narto_search_titles(keyword, limit=24)
        if not hits:
            raise RuntimeError("Narto 站内搜索无结果")
        kw = (keyword or "").lower().replace("’", "'").strip()
        cands = []
        for u, title in hits:
            ratio = difflib.SequenceMatcher(
                None, kw, title.lower().replace("’", "'").strip()).ratio()
            slug = u.split("/detail/watch/")[1].split("?")[0]
            cands.append({"ratio": round(ratio, 3), "app": "narto",
                          "title": title,
                          "watch_url": u, "book_id": slug,
                          "episode_count": "", "playlet_id": slug})
        self.log(f"[Narto] 站内搜索命中 {len(cands)} 条")
        cands.sort(key=lambda x: -x["ratio"])
        return cands[:limit]

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
                                       env=ENV_NO_PROXY, timeout=300)
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

    def _v_info(self, f):
        """读视频轨 (首帧pts秒, 时基tbn)。"""
        pts, tbn = 0.0, None
        try:
            import re
            r = subprocess.run(
                [FFMPEG, "-hide_banner", "-i", f, "-vf", "showinfo",
                 "-an", "-frames:v", "1", "-f", "null", "-"],
                capture_output=True, text=True, timeout=60)
            m = re.search(r"(\d+) tbn", r.stderr)
            if m:
                tbn = int(m.group(1))
            for line in r.stderr.splitlines():
                if "pts_time:" in line:
                    pts = float(line.split("pts_time:")[1].split(" ")[0])
                    break
        except Exception:
            pass
        return pts, tbn

    def _container_dur(self, f):
        try:
            r = subprocess.run([FFMPEG, "-hide_banner", "-i", f],
                               capture_output=True, text=True, timeout=60)
            import re
            m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", r.stderr)
            if m:
                return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
        except Exception:
            pass
        return None

    def _normalize_vstart(self, src, offset):
        """流拷贝归一化视频轨：①首帧 pts 前移 offset 归零；②时基统一为
        15360 tick/s——片源存在 30fps(15360) 与 24fps(12288) 混装，时基不
        一致会让 concat -c copy 错算时间轴（24fps 内容被压进 1/30 刻度，
        每集提速 25% 并产生大空洞），这是合成卡顿的核心根因；③截除片尾
        音频静音填充。全程零转码。失败回退原文件。"""
        dst = src[:-4] + "._ns.mp4"
        dur = self._container_dur(src)
        cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
               "-itsoffset", str(-offset), "-i", src, "-i", src,
               "-map", "0:v", "-map", "1:a", "-c", "copy",
               "-video_track_timescale", "15360"]
        if dur:
            cmd += ["-t", str(round(dur - offset - 0.02, 3))]
        cmd += ["-movflags", "+faststart", dst]
        r = subprocess.run(cmd, capture_output=True, text=True,
                           env=ENV_NO_PROXY, timeout=300)
        if r.returncode != 0 or not os.path.exists(dst):
            return src          # 修复失败时回退原文件（不影响合成）
        return dst

    def merge(self, out_dir, eps, full_path, progress_fn=None):
        """无损合成。逐集归一化后 concat：①视频首帧 pts 归零（片源 +0.16s
        头部偏移会造成接缝冻结）②视频时基统一 15360（30/24fps 混装时基不
        一致会造成跳帧与大空洞）③截除片尾音频静音填充。"""
        use, tmps = {}, []
        eps_sorted = sorted(eps)
        for i, no in enumerate(eps_sorted):
            p = os.path.join(out_dir, f"ep{no:02d}.mp4")
            off, tbn = self._v_info(p)
            if off > 0.02 or (tbn and tbn != 15360):
                use[no] = self._normalize_vstart(p, off)
                if use[no] != p:
                    tmps.append(use[no])
            else:
                use[no] = p
            if progress_fn:
                progress_fn(i + 1, len(eps_sorted))
        lst = os.path.join(out_dir, "concat.txt")
        with open(lst, "w", encoding="utf-8") as fh:
            for no in eps_sorted:
                fh.write(f"file '{use[no].replace(os.sep, '/')}'\n")
        cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
               "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy",
               "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", full_path]
        r = subprocess.run(cmd, capture_output=True, text=True, env=ENV_NO_PROXY,
                           timeout=3600)
        for t in tmps:
            try:
                os.remove(t)
            except OSError:
                pass
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
        task = DramaTool.parse_task_link(text)
        if task:
            TOOL.log(f"[搜索] 识别为北斗任务链接: task_id={task[0]} app_id={task[1]}")
            if task[1] == "netshort":
                try:
                    drama = TOOL.resolve_task(task[0], task[1])
                except Exception:
                    info = TOOL.fetch_task(task[0], task[1])
                    TOOL.log(f"[提示] Narto 片源未收录《{info.get('title')}》；"
                             f"NetShort 直下需官网播放页链接（粘贴后用 Edge 解析）")
                    set_status(f"NetShort 任务：{info.get('title')}"
                               f"（Narto 未收录，需播放页链接）", busy=False)
                    return
            drama = TOOL.resolve_task(task[0], task[1])
            STATE.current = drama
            STATE.download_dir = None
            STATE.result = None
            TOOL.log(f"[结果] 《{drama['title']}》 官方 {drama['official_eps']} 集，"
                     f"API 返回 {len(drama['eps'])} 集")
            set_status(f"已就绪：《{drama['title']}》({len(drama['eps'])} 集) —— 可开始下载",
                       busy=False)
            return
        pkey, pname = detect_platform(text)
        if pkey == "netshort":
            TOOL.log(f"[平台] 识别为 {pname}，解析播放页与分集…")
            drama = netshort_resolve(text)
            STATE.current = drama
            STATE.download_dir = None
            STATE.result = None
            TOOL.log(f"[结果] 《{drama['title']}》 {drama['official_eps']} 集"
                     f"（{pname}，签名直链需本机 Edge 解析）")
            set_status(f"已就绪：《{drama['title']}》({drama['official_eps']} 集，"
                       f"{pname}) —— 可开始下载", busy=False)
            return
        if pkey == "narto":
            TOOL.log(f"[平台] 识别为 {pname}，解析播放页与分集…")
            drama = narto_resolve(text)
            STATE.current = drama
            STATE.download_dir = None
            STATE.result = None
            TOOL.log(f"[结果] 《{drama['title']}》 {drama['official_eps']} 集"
                     f"（{pname}，直链下载）")
            set_status(f"已就绪：《{drama['title']}》({drama['official_eps']} 集，"
                       f"{pname}) —— 可开始下载", busy=False)
            return
        if pkey and pkey != "yc":
            TOOL.log(f"[平台] 识别为 {pname}（{pkey}），本版下载适配支持 "
                     f"YourChannel / NetShort / Narto Drama 三个平台；"
                     f"该平台暂仅做识别")
            set_status(f"已识别为 {pname}（暂不支持下载）", busy=False)
            return
        pid, kw = DramaTool.parse_input(text)
        if pid:
            TOOL.log(f"[搜索] 识别为 playlet_id: {pid}")
            drama = TOOL.fetch_playlet(pid)
            STATE.current = drama
            STATE.download_dir = None
            STATE.result = None
            TOOL.log(f"[结果] 《{drama['title']}》 官方 {drama['official_eps']} 集，"
                     f"API 返回 {len(drama['eps'])} 集")
            set_status(f"已就绪：《{drama['title']}》({len(drama['eps'])} 集) —— 可开始下载",
                       busy=False)
        elif kw:
            TOOL.log(f"[搜索] 按剧名实时全局检索: {kw}")
            try:
                cands = TOOL.search_global(kw)
            except Exception as e:
                TOOL.log(f"[实时检索] 失败（{e}），回落本地缓存匹配")
                cands = TOOL.search_by_name(kw)
            try:
                panel_hits = TOOL.search_panel(kw)
                if panel_hits:
                    cands = panel_hits + cands
                    TOOL.log(f"[面板索引] 命中 {len(panel_hits)} 条跨平台记录"
                             f"（11 平台上新面板，仅剧名/日期，无播放 ID）")
            except Exception as e:
                TOOL.log(f"[面板索引] 不可用: {e}")
            try:
                narto_hits = TOOL.search_narto(kw)
                if narto_hits:
                    cands = narto_hits + cands
            except Exception as e:
                TOOL.log(f"[Narto] 实时检索不可用: {e}")
            if not cands:
                TOOL.log("[结果] 未找到匹配剧目。")
                TOOL.log("  原因说明：YourChannel 官网搜索接口已停用（实测），")
                TOOL.log("  剧名实时检索走北斗任务池（8 平台 × 各 200 条，收入排序，")
                TOOL.log("  平台仅暴露 200 条为硬限制）+ Narto Drama 站内搜索。")
                TOOL.log("  新上架或低热度剧可能不在北斗池窗口中。请到北斗任务详情页、")
                TOOL.log("  narto-drama.com 或 yourchannel.ai 复制链接，")
                TOOL.log("  直接粘贴到输入框重试（任务链接/播放页链接均可直连）。")
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


def job_fetch_narto(url):
    set_status("解析 Narto Drama 播放页…", busy=True)
    try:
        drama = narto_resolve(url)
        STATE.current = drama
        STATE.download_dir = None
        STATE.result = None
        TOOL.log(f"[结果] 《{drama['title']}》 {drama['official_eps']} 集"
                 f"（Narto Drama，直链下载）")
        set_status(f"已就绪：《{drama['title']}》({drama['official_eps']} 集) "
                   f"—— 可开始下载", busy=False)
    except Exception as e:
        TOOL.log(f"[错误] {e}")
        set_status(f"解析失败: {e}", busy=False)


def job_fetch(pid):
    set_status(f"获取剧目 {pid} …", busy=True)
    try:
        drama = TOOL.fetch_playlet(pid)
        STATE.current = drama
        STATE.download_dir = None
        STATE.result = None
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
        if drama.get("platform") == "netshort":
            TOOL.log(f"[平台] NetShort：逐集解析签名直链（本机 Edge 驱动）")
            fails = netshort_download(drama["eps"], out, prog)
        elif drama.get("platform") == "narto":
            TOOL.log(f"[平台] Narto Drama：逐集实时解析直链（免浏览器）")
            fails = narto_download(drama["eps"], out, TOOL.log, prog)
        else:
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
        if drama.get("platform") == "netshort":
            TOOL.log("[字幕] NetShort 剧集字幕为画面烧录式，已随视频获取")
        else:
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
    full = os.path.join(out, "FULL.mp4")
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
<h1>🎬 Short Drama Downloader <small style="font-size:12px;color:#8ea2bd">YourChannel · NetShort · Narto Drama 短剧下载与合成</small></h1>
<div class="row">
  <input id="q" type="text" placeholder="剧名 / 播放页URL（YourChannel·NetShort·Narto Drama）/ playlet_id / 北斗任务链接">
  <button onclick="doSearch()">搜索</button>
  <button class="gray" onclick="doClearResult()">清除结果</button>
</div>
<div class="row">
  <input id="out" type="text" style="flex:2" placeholder="输出目录（分集下载与合成成片 FULL.mp4 均保存在此）">
  <button class="gray" onclick="pickDir('out')">浏览…</button>
</div>
<div class="row">
  <button class="green" id="bdl" onclick="doDownload()">开始下载</button>
  <button class="green" id="bmg" onclick="doMerge()">合成为完整视频</button>
  <button class="gray" id="bex" onclick="doExport()">导出日志</button>
  <input id="expath" type="text" style="flex:1" placeholder="日志导出路径（留空=桌面）">
  <button class="gray" onclick="pickDir('expath')">浏览…</button>
  <button class="gray" onclick="openDir()">打开输出目录</button>
  <button class="gray" onclick="doClearPool()">清除任务池</button>
  <button class="gray" onclick="doClearLog()">清空日志</button>
</div>
<div id="bar"><div id="barin"></div></div>
<div class="st" id="st">初始化…</div>
<table id="cands" style="display:none"><thead><tr><th>匹配度</th><th>平台</th><th>剧名</th><th>集数</th><th>playlet_id</th><th></th></tr></thead><tbody></tbody></table>
<div id="log"></div>
<p style="color:#66708a;font-size:12px">支持平台：<b>YourChannel</b>（API 直连）、<b>NetShort</b>（播放页链接，签名直链由本机 Edge 解析）、<b>Narto Drama 多剧场</b>（播放页链接直连，含 BibiShort 等 35+ 片源，直链下载）；其余剧场（ShortMax / DramaBox / ReelShort / StardustTV / KalosTV / GoodShort / FlickReels / MoboReels / StoryReel）可识别并跨平台检索，下载适配持续推进。<br>
提示：剧名搜索为实时检索——北斗任务池（8 平台 × 各 200 条）+ 11 平台上新面板索引 + Narto Drama 站内搜索（免登录，候选可一键「用此剧」）；北斗非 YC 源任务（dramawave / dramabox 等）链接可自动定位到 Narto 对应片源；新剧也可直接粘贴播放页链接或北斗任务链接。</p>
<script>
let timer=null;
function fmt(){return (new Date).toLocaleTimeString('zh-CN',{hour12:false})}
async function post(url,data){const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data||{})});return r.json()}
async function doSearch(){const t=document.getElementById('q').value;if(!t)return alert('请输入剧名、URL 或 id');await post('/api/search',{text:t})}
async function doRefresh(){await post('/api/refresh',{})}
async function doClearPool(){if(!confirm('确定清除本地任务池缓存？下次搜索将自动重新拉取。'))return;await post('/api/clear_pool',{})}
async function doClearResult(){await post('/api/clear_result',{})}
async function doClearLog(){await post('/api/clear_log',{})}
async function pickDir(target){const d=await post('/api/pick_dir',{});if(d.path)document.getElementById(target).value=d.path}
async function doDownload(){await post('/api/download',{out:document.getElementById('out').value})}
async function doMerge(){await post('/api/merge',{})}
async function doExport(){const d=await post('/api/export_log',{path:document.getElementById('expath').value});if(d.path)alert('日志已导出:\\n'+d.path)}
function openDir(){post('/api/open_dir',{out:document.getElementById('out').value})}
function usePid(el){post('/api/fetch',{id:el.getAttribute('data-pid')})}
function useNarto(el){post('/api/fetch_narto',{url:el.getAttribute('data-url')})}
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
        tr.innerHTML=`<td>${c.ratio}</td><td>${c.app}</td><td>${c.title}</td><td>${c.episode_count||''}</td><td>${c.playlet_id||''}</td><td>${c.app==='yourchannel_drama'?'<button onclick=usePid(this) data-pid="'+c.playlet_id+'">用此剧</button>':(c.watch_url?'<button onclick=useNarto(this) data-url="'+c.watch_url+'">用此剧</button>':'<span style="color:#66708a">仅识别</span>')}</td>`;
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
        if STATE.busy and self.path not in ("/api/open_dir", "/api/pick_dir"):
            self._json({"ok": False, "msg": "busy"})
            return
        if self.path == "/api/search":
            threading.Thread(target=job_search, args=(payload,), daemon=True).start()
            self._json({"ok": True})
        elif self.path == "/api/fetch":
            threading.Thread(target=job_fetch, args=(payload.get("id"),),
                             daemon=True).start()
            self._json({"ok": True})
        elif self.path == "/api/fetch_narto":
            threading.Thread(target=job_fetch_narto,
                             args=(payload.get("url"),),
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
        elif self.path == "/api/clear_pool":
            def work():
                set_status("清除任务池缓存…", busy=True)
                try:
                    TOOL.clear_pool()
                    set_status("任务池已清除（下次搜索自动重新拉取）", busy=False)
                except Exception as e:
                    set_status(f"清除失败: {e}", busy=False)
            threading.Thread(target=work, daemon=True).start()
            self._json({"ok": True})
        elif self.path == "/api/clear_result":
            STATE.result = None
            TOOL.log("[界面] 已清除候选列表")
            self._json({"ok": True})
        elif self.path == "/api/clear_log":
            STATE.logs = []
            STATE.push_log("[界面] 日志已清空")
            self._json({"ok": True})
        elif self.path == "/api/pick_dir":
            try:
                p = pick_folder_dialog()
                self._json({"ok": True, "path": p or ""})
            except Exception as e:
                self._json({"ok": False, "msg": str(e), "path": ""})
        elif self.path == "/api/export_log":
            p = (payload.get("path") or "").strip()
            if not p:
                p = os.path.join(os.path.expanduser("~"), "Desktop",
                                 f"yc_tool_日志_{datetime.now():%Y%m%d_%H%M%S}.txt")
            elif os.path.isdir(p) or p.endswith(("\\", "/")):
                p = os.path.join(
                    p.rstrip("\\/"),
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
