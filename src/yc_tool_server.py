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
                cmd = [FFMPEG, "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
                       "-headers", "Referer: https://web.netshort.com/\r\n"
                       f"User-Agent: {NS_UA}\r\n",
                       "-i", media, "-c", "copy", "-bsf:a", "aac_adtstoasc",
                       "-movflags", "+faststart", f]
                rc, _err = ff_run(cmd, 300)
                if rc == 0 and os.path.exists(f) and \
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
    # 权威分集数：播放页 SSR 内嵌 episodeItemsRaw（后端 episode 表）
    raw = re.search(r'const episodeItemsRaw = (\[.*?\]);\s*\n', html, re.S)
    if raw:
        try:
            n = len(json.loads(raw.group(1)))
            if n > 0:
                total = n
        except Exception:
            pass
    eps = {no: {"slug": slug} for no in range(1, total + 1)}
    return {"id": slug, "title": title or slug, "official_eps": total,
            "platform": "narto", "platform_name": "Narto Drama",
            "watch_url": page_url, "eps": eps}


# 全局限流：多任务并行时所有 Narto 请求共享这一配额（跨任务）。
# 实测 2 个任务各 4 并发（8 路同时打边缘域）会被站点限流，refresh-source
# 大量超时返回空 → 每集 3×90s 重试无限拖长。限到 4 路后稳定。
NARTO_REQ = threading.Semaphore(4)


def narto_play_url(slug, no, sess=None, force_edge=False):
    """经边缘域 refresh-source 端点实时取分集直链（免登录，含重试）。
    ① 全部请求带 no_cache=1：边缘缓存可能回放**已过期签名 URL**（GET 410
    Gone），必须强制回源；② 站点有冷却机制（refresh_source_cooldown_active /
    recently_failed，429 + retry_after_seconds），按站点给的秒数退避。
    force_edge=True 时加 force=1&force_edge=1，要求后端返回站方边缘代理流
    （moviebox-edge/stream 边缘），用于直链 CDN 被 CloudFront 拦截（428）
    的场景——与站点播放器自身降级逻辑一致。"""
    sess = sess or narto_session()
    extra = "&no_cache=1"
    if force_edge:
        extra += "&force=1&force_edge=1"
    u = (f"{NARTO_EDGE}/e/rs/detail/watch/{slug}/{no}/refresh-source"
         f"?lang={NARTO_LANG}{extra}")
    last_err = None
    for attempt in range(3):
        try:
            with NARTO_REQ:
                r = sess.get(u, headers={"Accept": "application/json"},
                             timeout=60)
            if r.status_code == 429:
                # 站点冷却：按响应里的 retry_after_seconds 退避（有下限防 0）
                try:
                    wait_s = float(r.json().get("retry_after_seconds") or 0)
                except Exception:
                    wait_s = 0
                last_err = RuntimeError("429 冷却: " + r.text[:80])
                time.sleep(max(wait_s, 10.0) * (1 if attempt else 0.5))
                continue
            d = r.json()
            if d.get("ok") and d.get("play_url"):
                return d["play_url"]
            last_err = RuntimeError(d.get("message") or "未返回播放地址")
        except Exception as e:
            last_err = e
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"refresh-source 第 {no} 集失败: {last_err}")


def narto_probe(url, sess=None):
    """轻量探测直链可用性（Range 取 1KB）。直链 CDN（如 hakunaymatata 的
    CloudFront）可能返回 428/403——与浏览器播放行为一致，需降级到边缘流。"""
    sess = sess or narto_session()
    try:
        with NARTO_REQ:
            r = sess.get(url, headers={"Range": "bytes=0-1023",
                                       "User-Agent": NARTO_UA,
                                       "Referer": NARTO_MAIN + "/"},
                         timeout=20, stream=True)
        ok = r.status_code in (200, 206)
        r.close()
        return ok
    except Exception:
        return False


def narto_download(eps, out_dir, log=None, progress=None, should_stop=None):
    """逐集实时解析直链 + ffmpeg 无损转存（4 并发）。
    直链探测失败（如 CloudFront 428）自动降级站方边缘代理流。"""
    os.makedirs(out_dir, exist_ok=True)
    fails = []
    sess = narto_session()
    slugs = {no: src.get("slug") for no, src in eps.items()}
    ref = NARTO_MAIN + "/"

    def resolve_one(no, sess_):
        slug = slugs[no]
        try:
            u = narto_play_url(slug, no, sess_)
            if narto_probe(u, sess_):
                return no, {"mp4": u, "referer": ref}, ""
            # 直链 CDN 拒绝（428/403 等）→ 强制边缘代理流（站点同款降级）
            time.sleep(1.5)   # 别紧接着二次打点，容易触发站点 429 冷却
            try:
                u2 = narto_play_url(slug, no, sess_, force_edge=True)
                return no, {"mp4": u2 or u, "referer": ref}, ""
            except Exception:
                # 降级请求失败（429/超时）时先用原直链试，别整集判死
                return no, {"mp4": u, "referer": ref}, ""
        except Exception as e:
            return no, None, str(e)[:140]

    import concurrent.futures as cf
    urls, errs = {}, {}
    total = len(eps)
    ex = cf.ThreadPoolExecutor(4)
    try:
        for no, src, err in ex.map(lambda n: resolve_one(n, sess), sorted(eps)):
            if should_stop and should_stop():
                raise TaskCanceled()
            urls[no] = src
            if not src:
                fails.append(no)
                errs[no] = err
                if progress:
                    progress(no, "ERR 未取到播放地址", len(urls), total)
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    # 第二轮补拉：并发抢站点时偶发空返回，串行 + 新 session + 退避重试
    if fails:
        if log:
            log(f"[Narto] 首轮 {len(fails)} 集直链失败 {sorted(fails)}，串行补拉…")
        s2 = narto_session()
        still = []
        for no in sorted(fails):
            time.sleep(6)   # 站点对失败集有冷却，间隔太短只会继续 429
            _n, src, err = resolve_one(no, s2)
            if src:
                urls[no] = src
                fails.remove(no)
                errs.pop(no, None)
                if log:
                    log(f"[Narto] ep{no:02d} 补拉成功")
            else:
                still.append(no)
                errs[no] = err
        for no in still:
            if log:
                log(f"[Narto] ep{no:02d} 补拉仍失败：{errs.get(no)}")
    dl_eps = {no: src for no, src in urls.items() if src}
    if log:
        log(f"[Narto] 已解析 {total - len(fails)}/{total} 集直链，开始无损转存")
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


def ff_run(cmd, timeout=300):
    """运行 ffmpeg，返回 (returncode, stderr 文本)。
    stderr 写临时文件而不是 PIPE：多线程并发起子进程时，PIPE 句柄会被
    其他线程的子进程继承，父进程 read 不到 EOF → 卡死（Windows 实测，
    表现为 merge 卡在 merging 且无 ffmpeg 进程）。"""
    import tempfile
    fd, path = tempfile.mkstemp(prefix="ff_", suffix=".log")
    os.close(fd)
    try:
        with open(path, "wb") as fe:
            r = subprocess.run(cmd, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=fe,
                               env=ENV_NO_PROXY, timeout=timeout)
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as fr:
                txt = fr.read()
        except Exception:
            txt = ""
        return r.returncode, txt
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


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
            ref = src.get("referer") or "https://yourchannel.ai/"
            for tag, url in (("mp4", src.get("mp4")), ("m3u8", src.get("m3u8"))):
                if not url:
                    continue
                cmd = [FFMPEG, "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
                       "-headers", f"Referer: {ref}\r\n"
                       "User-Agent: Mozilla/5.0\r\n",
                       "-i", url, "-c", "copy", "-bsf:a", "aac_adtstoasc",
                       "-movflags", "+faststart", f]
                try:
                    rc, _err = ff_run(cmd, 300)
                except subprocess.TimeoutExpired:
                    rc = -1
                if rc == 0 and \
                        os.path.exists(f) and os.path.getsize(f) > 500_000:
                    return no, f"ok[{tag}] {os.path.getsize(f)//1048576}MB"
                if os.path.exists(f):
                    os.remove(f)
            return no, "ERR"

        import concurrent.futures as cf
        done = 0
        ex = cf.ThreadPoolExecutor(workers)
        try:
            futs = {ex.submit(dl_one, no, src): no for no, src in eps.items()}
            for fut in cf.as_completed(futs):
                no, st = fut.result()
                done += 1
                if progress_fn:
                    progress_fn(no, st, done, len(eps))
                if st.startswith("ERR"):
                    fails.append(no)
        finally:
            # 取消/异常时不再等剩余集跑完（已在跑的 ffmpeg 自行结束）
            ex.shutdown(wait=False, cancel_futures=True)
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
                rc, err = ff_run([FFMPEG, "-nostdin", "-hide_banner", "-i", f], 60)
                if "Duration:" not in err:
                    bad.append(no)
            except Exception:
                bad.append(no)
        return bad

    def _probe_params(self, f):
        """探测分集编码参数（视频 codec/分辨率/fps/像素格式，音频 codec/
        采样率/声道）。解析失败返回 None。"""
        try:
            _rc, err = ff_run([FFMPEG, "-nostdin", "-hide_banner", "-i", f], 60)
        except Exception:
            return None
        vline = aline = None
        for ln in err.splitlines():
            if ": Video: " in ln and vline is None:
                vline = ln
            elif ": Audio: " in ln and aline is None:
                aline = ln
        if not vline:
            return None
        p = {"vcodec": "", "profile": "", "w": 0, "h": 0, "fps": 0.0,
             "pixfmt": "", "acodec": "", "arate": 0, "ach": 0}
        m = re.search(r"Video: (\w+)", vline)
        if m:
            p["vcodec"] = m.group(1)
        pm = re.search(r"Video: \w+ *\(([^)]*)\)", vline)
        if pm:
            p["profile"] = pm.group(1)
        m = re.search(r"(\d{2,5})x(\d{2,5})", vline)
        if m:
            p["w"], p["h"] = int(m.group(1)), int(m.group(2))
        m = re.search(r"([\d.]+) fps", vline)
        if m:
            p["fps"] = float(m.group(1))
        m = re.search(r"(yuv420p10le|yuv420p|yuvj420p)", vline)
        if m:
            p["pixfmt"] = m.group(1)
        if aline:
            m = re.search(r"Audio: (\w+)", aline)
            if m:
                p["acodec"] = m.group(1)
            m = re.search(r"(\d+) Hz", aline)
            if m:
                p["arate"] = int(m.group(1))
            m = re.search(r"(\d+) channels", aline)
            if m:
                p["ach"] = int(m.group(1))
            elif "stereo" in aline:
                p["ach"] = 2
            elif "mono" in aline:
                p["ach"] = 1
        return p

    def _realign(self, src, dst, t):
        """混装剧分集统一转码（H.264 CRF18 veryfast，视觉无损档）。
        关键约束：MP4 单视频轨只写一份 SPS/PPS（extradata，取自第一集），
        而 slice header 的位宽等编码随各自的 SPS——不同编码器/参数生成的
        流即使分辨率帧率一致，也解不出正确画面（灰噪声/彩虹）。所以混装
        剧必须全剧由同一编码器同参数统一转码，SPS 才天然一致；仅对齐
        少数派是无效修复（实测已验证）。参数一致剧不走此路径（零转码）。"""
        cmd = [FFMPEG, "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
               "-i", src]
        if (t["w"], t["h"]) != (self._srcp["w"], self._srcp["h"]):
            cmd += ["-vf", f"scale={t['w']}:{t['h']}"]
        if t["fps"] > 0:
            cmd += ["-r", f"{t['fps']:.3f}"]
        cmd += ["-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
                "-pix_fmt", "yuv420p", "-video_track_timescale", "15360"]
        if (t["acodec"] == self._srcp["acodec"]
                and t["arate"] == self._srcp["arate"]
                and t["ach"] == self._srcp["ach"] and self._srcp["acodec"]):
            cmd += ["-c:a", "copy"]
        else:
            cmd += ["-c:a", "aac", "-b:a", "128k"]
            if t["arate"]:
                cmd += ["-ar", str(t["arate"])]
            if t["ach"]:
                cmd += ["-ac", str(t["ach"])]
        cmd += ["-avoid_negative_ts", "make_zero",
                "-movflags", "+faststart", dst]
        rc, err = ff_run(cmd, 3600)
        if rc != 0 or not os.path.exists(dst) or os.path.getsize(dst) < 100_000:
            raise RuntimeError("分集参数对齐失败 " + os.path.basename(src)
                               + ": " + (err or "")[-200:])

    def _trim_start(self, src, offset):
        """流拷贝归一化：①输入侧 -ss 修剪到视频首帧关键帧（首帧 pts 归零，
        片源 +0.1s 头部偏移会造成接缝冻结）；②时基统一 15360 tick/s。
        单输入、零转码，替代旧的 -itsoffset 双输入方案（该方案会改动
        时间戳基线，是播放器卡死/花屏的嫌疑源之一）。失败回退原文件。"""
        dst = src[:-4] + "._ns.mp4"
        cmd = [FFMPEG, "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
               "-ss", f"{offset:.3f}", "-i", src, "-c", "copy",
               "-video_track_timescale", "15360",
               "-avoid_negative_ts", "make_zero",
               "-movflags", "+faststart", dst]
        try:
            rc, _err = ff_run(cmd, 300)
        except Exception:
            rc = -1
        if rc != 0 or not os.path.exists(dst):
            return src          # 修复失败时回退原文件（不影响合成）
        return dst

    def merge(self, out_dir, eps, full_path, progress_fn=None):
        """无损合成。流程：探测各集参数 → 以多数派参数为目标，不一致分集
        对齐转码 → 首帧/时基归一 → concat 流拷贝 → 时长校验。
        根因背景：MP4 单视频轨只写一份 SPS/PPS（取自第一集），分集混装
        分辨率/帧率/编码参数时，从切换集开始解码参数错配 → 彩虹噪声/
        黑屏（ffmpeg 软解容忍、常见播放器硬解必坏），播放器反复重建
        解码器导致卡顿甚至假死。"""
        use, tmps = {}, []
        eps_sorted = sorted(eps)
        # 缺集会静默合成出不完整成片（concat 跳过不存在文件仍返回 0），先拦住
        missing = [no for no in eps_sorted
                   if not os.path.exists(os.path.join(out_dir, f"ep{no:02d}.mp4"))]
        if missing:
            raise RuntimeError(f"缺少分集文件 ep{missing}，请先补齐再合成")

        # 1) 参数探测 + 多数派目标
        params, durs = {}, {}
        for no in eps_sorted:
            p = os.path.join(out_dir, f"ep{no:02d}.mp4")
            params[no] = self._probe_params(p)
            durs[no] = self._container_dur(p) or 0.0
            if progress_fn:
                progress_fn(no, f"参数探测 ok", 0, len(eps_sorted))
        valid = [params[no] for no in eps_sorted if params[no]]
        if not valid:
            raise RuntimeError("无法解析分集编码参数，合成中止")
        key = lambda p: (p["vcodec"], p["w"], p["h"], round(p["fps"] or 0, 2),
                         p["pixfmt"])
        cnt = {}
        for p in valid:
            cnt[key(p)] = cnt.get(key(p), 0) + 1
        tk = max(cnt, key=cnt.get)
        target = next(p for p in valid if key(p) == tk)
        n_off = len(eps_sorted) - cnt[tk]
        if n_off:
            TOOL.log(f"[merge] 分集参数混装（{len(cnt)} 种参数组合），"
                     f"全剧统一转码 H.264 {target['w']}x{target['h']}"
                     f"@{target['fps']:.0f} 以根治花屏/黑屏")
            # 混装剧统一目标：多数派的分辨率/帧率 + H.264（同一编码器同
            # 参数才能保证全剧 SPS 一致，见 _realign 注释）
            target = dict(target, vcodec="h264", profile="")

        # 2) 参数对齐：混装时全剧统一转码（SPS 必须同源一致）；参数一致
        #    时零转码快路径
        self._srcp = None
        for no in eps_sorted:
            p = params[no]
            if not p:
                continue            # 探测失败的集交由后续 verify 兜底
            src = os.path.join(out_dir, f"ep{no:02d}.mp4")
            if n_off:
                self._srcp = p
                dst = os.path.join(out_dir, f"ep{no:02d}._al.mp4")
                self._realign(src, dst, target)
                tmps.append(dst)
                use[no] = dst
                durs[no] = self._container_dur(dst) or durs[no]
                if progress_fn:
                    progress_fn(no, "参数已对齐", 0, len(eps_sorted))
            else:
                use[no] = src

        # 3) 首帧偏移/时基归一（流拷贝）
        nopro = [no for no in eps_sorted if no not in use]
        if nopro:
            raise RuntimeError(f"分集编码参数无法解析 ep{nopro}，合成中止")
        for no in eps_sorted:
            if no not in use:
                continue
            off, tbn = self._v_info(use[no])
            if off > 0.02 or (tbn and tbn != 15360):
                ns = self._trim_start(use[no], off)
                if ns != use[no]:
                    tmps.append(ns)
                    use[no] = ns
            if progress_fn:
                progress_fn(no, "归一化 ok", 0, len(eps_sorted))

        # 4) concat 流拷贝（清单内路径必须是绝对路径：ffmpeg 以清单文件
        #    所在目录为基准解析相对路径）
        lst = os.path.join(out_dir, "concat.txt")
        with open(lst, "w", encoding="utf-8") as fh:
            for no in eps_sorted:
                fh.write(f"file '{os.path.abspath(use[no]).replace(os.sep, '/')}'\n")
        cmd = [FFMPEG, "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
               "-fflags", "+genpts",
               "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy",
               "-bsf:a", "aac_adtstoasc", "-avoid_negative_ts", "make_zero",
               "-movflags", "+faststart", full_path]
        try:
            rc, tail_txt = ff_run(cmd, 3600)
        except subprocess.TimeoutExpired:
            raise RuntimeError("合成超时（3600s）")
        # 中间文件（对齐副本 + 归一化副本 + concat 清单）放后台删：某些环境
        # 对删除有安全拦截会挂起调用，不能让任务卡在 merging
        def _cleanup(paths):
            for p in paths:
                try:
                    os.remove(p)
                except OSError:
                    pass

        _tmp_all = list(tmps)
        if os.path.exists(lst):
            _tmp_all.append(lst)
        if _tmp_all:
            threading.Thread(target=_cleanup, args=(_tmp_all,),
                             daemon=True).start()
        if rc != 0:
            raise RuntimeError("合成失败: " + (tail_txt or "")[-300:])

        # 5) 时长 sanity check：成片时长 ≈ 各集时长之和（偏差 >3% 或 >2s
        #    视为时间轴异常，宁可报错也不产出会让播放器卡死的坏文件）
        expect = sum(d for no, d in durs.items() if no in use and d)
        got = self._container_dur(full_path)
        if got and expect and abs(got - expect) > max(2.0, 0.03 * expect):
            raise RuntimeError(f"成片时长校验失败：实际 {got:.1f}s vs "
                               f"预期 {expect:.1f}s，时间轴异常，已拒绝产出")
        return os.path.getsize(full_path)

    def _container_dur(self, f):
        try:
            _rc, err = ff_run([FFMPEG, "-nostdin", "-hide_banner", "-i", f], 60)
            import re
            m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err)
            if m:
                return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
        except Exception:
            pass
        return None

    def _v_info(self, f):
        """读视频轨 (首帧pts秒, 时基tbn)。"""
        pts, tbn = 0.0, None
        try:
            _rc, err = ff_run(
                [FFMPEG, "-nostdin", "-hide_banner", "-i", f, "-vf", "showinfo",
                 "-an", "-frames:v", "1", "-f", "null", "-"], 60)
            m = re.search(r"(\d+) tbn", err)
            if m:
                tbn = int(m.group(1))
            for line in err.splitlines():
                if "pts_time:" in line:
                    pts = float(line.split("pts_time:")[1].split(" ")[0])
                    break
        except Exception:
            pass
        return pts, tbn



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
            return {"busy": self.busy or TMGR.active_count() > 0,
                    "legacy_busy": self.busy,
                    "status": self.status_msg,
                    "logs": self.logs[-400:], "result": self.result,
                    "current": ({"title": self.current["title"],
                                 "official_eps": self.current["official_eps"],
                                 "eps": len(self.current["eps"])}
                                if self.current else None),
                    "tasks": [t.info() for t in TMGR.tasks.values()]}


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
# 多任务队列：并行/排队下载、独立进度、失败隔离、去重与任务管理
# ---------------------------------------------------------------------------
class TaskCanceled(Exception):
    pass


class Task:
    """一部剧的下载任务（独立状态与进度）。
    mode: full=解析→下载→校验→合成；merge=仅合成既有分集目录"""
    _seq = 0

    def __init__(self, drama=None, out_dir=None, source="", mode="full",
                 out_root=None, auto_merge=True):
        Task._seq += 1
        self.id = Task._seq
        self.drama = drama
        self.source = source or ""
        self.mode = mode
        self.out_dir = out_dir
        self.out_root = out_root
        self.auto_merge = auto_merge
        self.title = (drama or {}).get("title") or (source[:40] or "未解析")
        self.status = "queued"    # queued/resolving/downloading/verifying/
        #                           merging/done/failed/canceled
        self.done = 0
        self.total = len((drama or {}).get("eps") or {})
        self.fails, self.bad = [], []
        self.error = ""
        self.created = datetime.now()
        self.canceled = False

    def src_key(self):
        """未解析时按链接规范化出的去重键（同一链接/任务/剧 slug 可识别）"""
        s = self.source or ""
        m = re.search(r"[?&]task_id=(\d+)", s)
        if m:
            return "task:" + m.group(1)
        m = re.search(r"/detail/watch/([^/?#]+)", s)
        if m:
            return "watch:" + m.group(1)
        m = re.search(r"[?&]playlet_id=(\d+)", s)
        if m:
            return "yc:" + m.group(1)
        return ""

    def keys(self):
        """去重键集合：解析前用链接键，解析后同时给出平台键——
        两两取交集即可判断同剧（含任务池链接与播放页链接指向同一剧）。"""
        out = set()
        sk = self.src_key()
        if sk:
            out.add(sk)
        d = self.drama or {}
        did = str(d.get("id") or d.get("playlet_id") or "")
        if did:
            out.add(f"{d.get('platform') or ''}:{did}")
            out.add("watch:" + did)      # 与未解析的播放页链接对齐
        return out

    def info(self):
        pct = round(100 * self.done / self.total) if self.total else 0
        return {"id": self.id, "title": self.title,
                "status": self.status, "done": self.done,
                "total": self.total, "pct": pct,
                "fails": self.fails, "bad": self.bad,
                "error": self.error, "out": self.out_dir or "",
                "mode": self.mode,
                "created": self.created.strftime("%H:%M:%S")}


def ts_conflict(a, b):
    """两个任务是否指向同一部剧（仅对 full 模式去重；merge 不参与）。"""
    if a is b or a.id == b.id or b.mode != "full":
        return False
    return bool(a.keys() & b.keys())


class TaskManager:
    """并行任务调度：信号量限并发；NetShort（本机 Edge）独占串行。"""
    MAX_PARALLEL = 2

    def __init__(self):
        self.lock = threading.Lock()
        self.tasks = {}                    # id -> Task（按添加顺序）
        self.sema = threading.Semaphore(self.MAX_PARALLEL)
        self.ns_lock = threading.Lock()    # NetShort Edge 独占

    # -- 管理 --------------------------------------------------------------
    def add(self, task):
        """返回 (ok, msg)。去重：同剧目已有任务（未移除）则拒绝。"""
        with self.lock:
            ks = task.keys()
            if ks and task.mode == "full":
                for t in self.tasks.values():
                    if ts_conflict(t, task) and t.status not in ("canceled", "failed"):
                        return False, (f"已在任务列表中（#{t.id}《{t.title}》"
                                       f"{t.status}），无需重复添加")
            self.tasks[task.id] = task
        threading.Thread(target=self._run, args=(task,), daemon=True).start()
        return True, f"任务 #{task.id} 已加入队列"

    def get(self, tid):
        return self.tasks.get(tid)

    def cancel(self, tid):
        t = self.tasks.get(tid)
        if not t:
            return False, "任务不存在"
        if t.status in ("done", "failed", "canceled"):
            return False, f"任务已是终态（{t.status}）"
        t.canceled = True
        if t.status == "queued":
            t.status = "canceled"
        return True, f"任务 #{tid} 已标记取消（当前阶段完成后停止）"

    def remove(self, tid):
        t = self.tasks.get(tid)
        if not t:
            return False, "任务不存在"
        if t.status in ("resolving", "downloading", "verifying", "merging"):
            return False, "任务执行中，请先取消再移除"
        with self.lock:
            self.tasks.pop(tid, None)
        return True, f"任务 #{tid} 已移除"

    def clear_finished(self):
        with self.lock:
            dead = [tid for tid, t in self.tasks.items()
                    if t.status in ("done", "failed", "canceled")]
            for tid in dead:
                self.tasks.pop(tid, None)
        return len(dead)

    def active_count(self):
        return sum(1 for t in self.tasks.values()
                   if t.status in ("resolving", "downloading",
                                   "verifying", "merging", "queued"))

    # -- 执行 --------------------------------------------------------------
    def _run(self, task):
        with self.sema:
            try:
                self._pipeline(task)
            except TaskCanceled:
                task.status = "canceled"
                TOOL.log(f"[任务#{task.id}] 已取消")
            except Exception as e:
                task.status = "failed"
                task.error = str(e)
                TOOL.log(f"[任务#{task.id}] 失败（失败隔离，不影响其他任务）: {e}")

    def _chk(self, task):
        if task.canceled:
            raise TaskCanceled()

    def _resolve(self, task):
        """按 source 解析剧目（北斗任务链接 / 播放页链接 / playlet_id）"""
        text = task.source
        task_link = DramaTool.parse_task_link(text)
        if task_link:
            return TOOL.resolve_task(task_link[0], task_link[1])
        pkey, pname = detect_platform(text)
        if pkey == "netshort":
            return netshort_resolve(text)
        if pkey == "narto":
            return narto_resolve(text)
        if pkey and pkey != "yc":
            raise RuntimeError(f"{pname} 暂不支持自动下载，请使用支持的播放页链接")
        pid, kw = DramaTool.parse_input(text)
        if pid:
            return TOOL.fetch_playlet(pid)
        raise RuntimeError("无法定位剧目：任务支持北斗任务链接、播放页链接"
                           "（YourChannel / NetShort / Narto Drama）或 playlet_id；"
                           "剧名请先搜索后点「用此剧」")

    def _download(self, task):
        drama, out = task.drama, task.out_dir
        os.makedirs(out, exist_ok=True)

        def prog(no, st, done, total):
            # 用户在下载途中取消：立即中断（不再等整部剧跑完）
            if task.canceled:
                raise TaskCanceled()
            # 解析阶段（直链未取到）不推进完成数，避免 20 集任务显示成 19 集
            if not st.startswith("ERR 未取到播放地址"):
                task.done = done
            if not task.total:
                task.total = total
            STATE.status_msg = (f"[任务#{task.id}]《{task.title}》"
                                f"下载中 {task.done}/{task.total}（ep{no:02d}）")
            TOOL.log(f"[任务#{task.id}]  ep{no:02d} {st}  ({task.done}/{task.total})")

        if drama.get("platform") == "netshort":
            with self.ns_lock:
                self._chk(task)
                TOOL.log(f"[任务#{task.id}] 平台 NetShort：逐集解析签名直链"
                         f"（本机 Edge，任务串行）")
                return netshort_download(drama["eps"], out, prog)
        if drama.get("platform") == "narto":
            TOOL.log(f"[任务#{task.id}] 平台 Narto Drama：逐集实时解析直链")
            return narto_download(drama["eps"], out, TOOL.log, prog,
                                  should_stop=lambda: task.canceled)
        return TOOL.download_eps(drama["eps"], out, prog)

    def _pipeline(self, task):
        # 1) 解析
        if task.drama is None:
            task.status = "resolving"
            STATE.status_msg = f"[任务#{task.id}] 解析剧目…"
            self._chk(task)
            task.drama = self._resolve(task)
            task.title = task.drama["title"]
            task.total = len(task.drama["eps"])
            TOOL.log(f"[任务#{task.id}] 解析完成：《{task.title}》 "
                     f"{task.total} 集（{task.drama.get('platform_name', '')}）")
            # 解析后二次查重：任务池链接与播放页链接可能指向同一部剧
            if task.mode == "full":
                with self.lock:
                    dup = next((t for t in self.tasks.values()
                                if ts_conflict(task, t)
                                and t.status not in ("canceled", "failed")), None)
                if dup:
                    task.status = "canceled"
                    task.error = (f"与任务#{dup.id}《{dup.title}》指向同一部剧，"
                                  f"已自动去重跳过")
                    TOOL.log(f"[任务#{task.id}] {task.error}")
                    return
        # 2) 输出目录
        if not task.out_dir:
            root = task.out_root or DEFAULT_OUT
            slug = re.sub(r"[^a-z0-9]+", "-",
                          task.drama["title"].lower()).strip("-")
            task.out_dir = os.path.join(root, slug)
        # 3) 下载 + 校验
        if task.mode == "full":
            task.status = "downloading"
            self._chk(task)
            TOOL.log(f"[任务#{task.id}] 开始下载《{task.title}》 -> {task.out_dir}")
            task.fails = self._download(task)
            task.status = "verifying"
            self._chk(task)
            if task.fails:
                TOOL.log(f"[任务#{task.id}] [警告] 下载失败 "
                         f"{len(task.fails)} 集: {task.fails}")
            task.bad = TOOL.verify_eps(task.out_dir, task.drama["eps"])
            if task.bad:
                TOOL.log(f"[任务#{task.id}] [警告] 校验未通过: {task.bad}")
            else:
                TOOL.log(f"[任务#{task.id}] [校验] {task.total} 集全部通过")
            official = task.drama["official_eps"]
            got = task.total - len(set(task.fails) | set(task.bad))
            TOOL.log(f"[任务#{task.id}] [完整性] 官方 {official} 集 / 成功 {got} 集")
            STATE.result = {"type": "download", "title": task.title,
                            "out": task.out_dir, "official": official,
                            "got": got, "fails": task.fails, "bad": task.bad}
            # 4) 合成
            if task.auto_merge and not task.fails and not task.bad:
                self._merge(task)
        elif task.mode == "merge":
            self._merge(task)
        if task.status != "canceled":
            task.status = "done"
            STATE.status_msg = f"[任务#{task.id}]《{task.title}》完成"
            TOOL.log(f"[任务#{task.id}] 《{task.title}》 完成 -> {task.out_dir}")

    def _merge(self, task):
        task.status = "merging"
        self._chk(task)
        full = os.path.join(task.out_dir, "FULL.mp4")
        size = TOOL.merge(task.out_dir, task.drama["eps"], full)
        _rc, err = ff_run([FFMPEG, "-nostdin", "-hide_banner", "-i", full], 120)
        m = re.search(r"Duration: (\S+)", err)
        dur = m.group(1) if m else "?"
        TOOL.log(f"[任务#{task.id}] [合成] FULL.mp4 {size//1048576}MB 时长 {dur}")
        STATE.result = {"type": "merged", "title": task.title,
                        "out": task.out_dir, "full": full,
                        "size_mb": size // 1048576, "duration": dur}


TMGR = TaskManager()


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
    """兼容旧流程：把「当前选中剧目」作为任务加入队列（自动校验+合成）"""
    drama = STATE.current
    if not drama:
        set_status("请先搜索选定剧目", busy=False)
        return
    out_root = payload.get("out") or os.path.join(os.path.expanduser("~"), "Desktop")
    slug = re.sub(r"[^a-z0-9]+", "-", drama["title"].lower()).strip("-")
    out = os.path.join(out_root, slug)
    t = Task(drama=drama, out_dir=out, mode="full")
    ok, msg = TMGR.add(t)
    TOOL.log(f"[任务] {msg}")
    if not ok:
        set_status(msg, busy=False)
    else:
        STATE.download_dir = out
        set_status(f"任务 #{t.id} 已排队：《{drama['title']}》"
                   f"{len(drama['eps'])} 集（并行上限 {TMGR.MAX_PARALLEL}）",
                   busy=False)


def job_merge():
    """兼容旧流程：把「当前剧目已下载目录」的合成作为任务加入队列"""
    drama = STATE.current
    out = STATE.download_dir
    if not drama or not out or not os.path.isdir(out):
        set_status("请先完成下载", busy=False)
        return
    t = Task(drama=drama, out_dir=out, mode="merge")
    ok, msg = TMGR.add(t)
    TOOL.log(f"[任务] {msg}")
    set_status(f"任务 #{t.id} 已排队：合成《{drama['title']}》" if ok else msg,
               busy=False)


def job_task_add(payload):
    """新增任务：直接粘贴链接（北斗任务链接 / 播放页链接 / playlet_id），
    解析→下载→校验→合成 全自动；支持多条（换行/分号分隔），去重排队"""
    raw = payload.get("text") or ""
    out_root = payload.get("out") or DEFAULT_OUT
    lines = [x.strip() for x in re.split(r"[\n;；]+", raw) if x.strip()]
    if not lines:
        set_status("任务内容为空", busy=False)
        return
    added, skipped = [], []
    for text in lines:
        t = Task(source=text, out_root=out_root, mode="full")
        ok, msg = TMGR.add(t)
        if ok:
            added.append(t)
            TOOL.log(f"[任务] {msg}：{text[:60]}")
        else:
            skipped.append(msg)
    if added:
        set_status(f"已添加 {len(added)} 个任务（排队执行）", busy=False)
    if skipped:
        for m in skipped:
            TOOL.log(f"[任务] 跳过：{m}")
    STATE.result = {"type": "tasks_added",
                    "added": [t.id for t in added], "skipped": skipped}
    return {"added": [t.id for t in added], "skipped": skipped}


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
<h1>🎬 Short Drama Downloader <small style="font-size:12px;color:#8ea2bd">v2.5.0 · YourChannel · NetShort · Narto Drama 短剧下载与合成</small></h1>
<div class="row">
  <input id="q" type="text" placeholder="剧名 / 播放页URL（YourChannel·NetShort·Narto Drama）/ playlet_id / 北斗任务链接（多条链接用换行或分号分隔可批量添加任务）">
  <button onclick="doSearch()">搜索</button>
  <button class="green" onclick="doTaskAdd()">添加为任务</button>
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
<div class="row" style="margin:2px 0"><span style="color:#8ea2bd;font-size:12px">任务队列（并行上限 2，超出自动排队）</span>
  <button class="gray" style="padding:4px 10px;font-size:12px" onclick="doClearFinished()">清除已完成任务</button></div>
<table id="tasks" style="display:none"><thead><tr><th>#</th><th>剧名</th><th>状态</th><th>进度</th><th>输出目录</th><th>操作</th></tr></thead><tbody></tbody></table>
<table id="cands" style="display:none"><thead><tr><th>匹配度</th><th>平台</th><th>剧名</th><th>集数</th><th>playlet_id</th><th></th></tr></thead><tbody></tbody></table>
<div id="log"></div>
<p style="color:#66708a;font-size:12px">支持平台：<b>YourChannel</b>（API 直连）、<b>NetShort</b>（播放页链接，签名直链由本机 Edge 解析）、<b>Narto Drama 多剧场</b>（播放页链接直连，含 BibiShort 等 35+ 片源，直链下载）；其余剧场（ShortMax / DramaBox / ReelShort / StardustTV / KalosTV / GoodShort / FlickReels / MoboReels / StoryReel）可识别并跨平台检索，下载适配持续推进。<br>
提示：剧名搜索为实时检索——北斗任务池（8 平台 × 各 200 条）+ 11 平台上新面板索引 + Narto Drama 站内搜索（免登录，候选可一键「用此剧」）；北斗非 YC 源任务（dramawave / dramabox 等）链接可自动定位到 Narto 对应片源；新剧也可直接粘贴播放页链接或北斗任务链接。Narto 片源直链若被源站 CDN 拦截（实测部分片源返回 428），工具自动降级为站方边缘代理流（与站点播放器行为一致），逐集实时解析、下载失败集可重跑补齐。</p>
<script>
let timer=null;
function fmt(){return (new Date).toLocaleTimeString('zh-CN',{hour12:false})}
async function post(url,data){const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data||{})});return r.json()}
async function doSearch(){const t=document.getElementById('q').value;if(!t)return alert('请输入剧名、URL 或 id');await post('/api/search',{text:t})}
async function doTaskAdd(){const t=document.getElementById('q').value;if(!t)return alert('请输入链接（多条用换行/分号分隔）');await post('/api/task_add',{text:t,out:document.getElementById('out').value})}
async function doTaskCancel(el){const d=await post('/api/task_cancel',{id:Number(el.getAttribute('data-id'))});if(!d.ok)alert(d.msg)}
async function doTaskRemove(el){const d=await post('/api/task_remove',{id:Number(el.getAttribute('data-id'))});if(!d.ok)alert(d.msg)}
async function doClearFinished(){await post('/api/clear_finished',{})}
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
  const tk=document.querySelector('#tasks tbody');const tasks=s.tasks||[];
  if(tasks.length){document.getElementById('tasks').style.display='';
    const sig=tasks.map(t=>t.id+t.status+t.done+t.total+(t.error||'')).join('|');
    if(tk.dataset.n!==sig){tk.innerHTML='';tk.dataset.n=sig;
      for(const t of tasks){
        const tr=document.createElement('tr');
        const stColor={done:'#4fd07a',failed:'#ff6b6b',canceled:'#8ea2bd'}[t.status]||'#ffb14f';
        const op=t.status==='done'||t.status==='failed'||t.status==='canceled'
          ?`<button class="gray" onclick=doTaskRemove(this) data-id="${t.id}">移除</button>`
          :`<button class="gray" onclick=doTaskCancel(this) data-id="${t.id}">取消</button>`;
        tr.innerHTML=`<td>${t.id}</td><td>${t.title}</td><td style="color:${stColor}">${t.status}${t.error?'<br><span style="color:#ff6b6b">'+t.error+'</span>':''}</td><td><div style="display:inline-block;vertical-align:middle;width:110px;height:6px;background:#232838;border-radius:3px;overflow:hidden;margin-right:6px"><div style="height:100%;width:${t.pct}%;background:${stColor}"></div></div>${t.done}/${t.total} (${t.pct}%)</td><td>${t.out||'-'}</td><td>${op}</td>`;
        tk.appendChild(tr)}}
  } else {document.getElementById('tasks').style.display='none';tk.dataset.n=''}
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
        # 旧流程互斥（搜索/获取/刷新/清除池）：同一时刻一个；任务提交不受限
        LEGACY = {"/api/search", "/api/fetch", "/api/fetch_narto",
                  "/api/refresh", "/api/clear_pool"}
        if STATE.busy and self.path in LEGACY:
            self._json({"ok": False, "msg": "busy"})
            return
        if self.path == "/api/search":
            threading.Thread(target=job_search, args=(payload,), daemon=True).start()
            self._json({"ok": True})
        elif self.path == "/api/task_add":
            res = job_task_add(payload) or {}
            self._json({"ok": True, "added": res.get("added", []),
                        "skipped": res.get("skipped", [])})
        elif self.path == "/api/task_cancel":
            ok, msg = TMGR.cancel(payload.get("id"))
            self._json({"ok": ok, "msg": msg})
        elif self.path == "/api/task_remove":
            ok, msg = TMGR.remove(payload.get("id"))
            self._json({"ok": ok, "msg": msg})
        elif self.path == "/api/clear_finished":
            n_done = TMGR.clear_finished()
            self._json({"ok": True, "removed": n_done})
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
