# -*- coding: utf-8 -*-
"""
PC Remote - dieu khien may tinh Windows tu iPad cu (Safari iOS 9).
Chay: python server.py   (khong can cai them thu vien nao)
"""
import ctypes
import hmac
import json
import os
import secrets
import shutil
from ctypes import wintypes
import socket
import subprocess
import sys
import threading
import time
import mimetypes
import urllib.parse
import urllib.request
from http.cookies import CookieError, SimpleCookie

import books
import hub
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))

# Chay ngam bang pythonw thi khong co cua so console -> ghi log ra file
if sys.stdout is None or sys.stderr is None:
    _log = open(os.path.join(HERE, "server.log"), "w", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = _log

CONFIG_PATH = os.path.join(HERE, "config.json")
INDEX_PATH = os.path.join(HERE, "index.html")
CLOCK_PATH = os.path.join(HERE, "clock.html")
WORK_PATH = os.path.join(HERE, "work.html")
TODO_PATH = os.path.join(HERE, "todos.json")

DEFAULT_CONFIG = {
    "port": 8765,
    "pin": "",
    "weather": {"city": "TP.HCM", "lat": 10.8231, "lon": 106.6297, "timezone": "Asia/Ho_Chi_Minh"},
    "library": ["books"],
    "hub": {"inbox": "inbox", "outbox": "outbox"},
    "apps": [
        {"name": "File Explorer", "icon": "explorer", "cmd": "explorer"},
        {"name": "Chrome", "icon": "chrome", "cmd": "start chrome"},
        {"name": "YouTube", "icon": "youtube", "cmd": "start https://www.youtube.com"},
        {"name": "Claude", "icon": "claude", "cmd": "start \"\" \"%LOCALAPPDATA%\\AnthropicClaude\\claude.exe\""},
    ],
}


def load_config():
    if not os.path.exists(CONFIG_PATH):
        cfg = dict(DEFAULT_CONFIG)
        cfg["pin"] = "%04d" % secrets.randbelow(10000)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return cfg
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, v)
    return cfg


CONFIG = load_config()

PAGES = {
    "/": INDEX_PATH, "/index.html": INDEX_PATH,
    "/clock": CLOCK_PATH, "/work": WORK_PATH,
    "/books": os.path.join(HERE, "books.html"), "/read": os.path.join(HERE, "read.html"),
    "/hub": os.path.join(HERE, "hub.html"),
}
HUB = hub.Hub(os.path.normpath(os.path.join(HERE, CONFIG["hub"]["inbox"])),
              os.path.normpath(os.path.join(HERE, CONFIG["hub"]["outbox"])),
              os.path.join(HERE, "hub_note.json"))
LIB_FOLDERS = [os.path.normpath(os.path.join(HERE, p)) for p in CONFIG["library"]]
for _p in LIB_FOLDERS:
    if _p.startswith(HERE):
        os.makedirs(_p, exist_ok=True)
LIB = books.Library(LIB_FOLDERS, os.path.join(HERE, "positions.json"))

# ---------------- Ban phim ao (Windows) ----------------
user32 = ctypes.windll.user32
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002

VK = {
    "playpause": 0xB3, "next": 0xB0, "prev": 0xB1, "stop": 0xB2,
    "volup": 0xAF, "voldown": 0xAE, "mute": 0xAD,
    "space": 0x20, "enter": 0x0D, "esc": 0x1B, "f": 0x46,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "f11": 0x7A,
}
EXTENDED = {0xB3, 0xB0, 0xB1, 0xB2, 0xAF, 0xAE, 0xAD, 0x25, 0x26, 0x27, 0x28}
ALT = 0x12
TAB = 0x09


def tap(vk, times=1):
    flag = KEYEVENTF_EXTENDEDKEY if vk in EXTENDED else 0
    for _ in range(max(1, min(times, 20))):
        user32.keybd_event(vk, 0, flag, 0)
        user32.keybd_event(vk, 0, flag | KEYEVENTF_KEYUP, 0)
        time.sleep(0.01)


def alt_tab():
    user32.keybd_event(ALT, 0, 0, 0)
    user32.keybd_event(TAB, 0, 0, 0)
    user32.keybd_event(TAB, 0, KEYEVENTF_KEYUP, 0)
    user32.keybd_event(ALT, 0, KEYEVENTF_KEYUP, 0)


def later(fn, delay=0.5):
    threading.Timer(delay, fn).start()


def run(cmd):
    subprocess.Popen(cmd, shell=True)


def do_action(action, count):
    if action in VK:
        tap(VK[action], count)
        return "OK"
    if action == "alttab":
        alt_tab()
        return "OK"
    if action == "lock":
        later(lambda: user32.LockWorkStation())
        return "Đã khóa máy"
    if action == "sleep":
        later(lambda: ctypes.windll.powrprof.SetSuspendState(False, True, False))
        return "Máy đang ngủ..."
    if action == "shutdown":
        run("shutdown /s /t 60")
        return "Máy sẽ tắt sau 60 giây"
    if action == "restart":
        run("shutdown /r /t 60")
        return "Máy sẽ khởi động lại sau 60 giây"
    if action == "cancel":
        run("shutdown /a")
        return "Đã hủy lệnh tắt máy"
    if action.startswith("app"):
        try:
            idx = int(action[3:])
            app = CONFIG["apps"][idx]
        except (ValueError, IndexError):
            return None
        run(app["cmd"])  # chi chay lenh co san trong config.json
        return "Đã mở " + app["name"]
    return None


# ---------------- Thoi tiet (Open-Meteo, mien phi, khong can key) ----------------
WEATHER_TTL = 600  # giay
_weather_lock = threading.Lock()
_weather_cache = {"at": 0, "data": None}


def fetch_weather():
    w = CONFIG["weather"]
    q = urllib.parse.urlencode({
        "latitude": w["lat"], "longitude": w["lon"], "timezone": w["timezone"],
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m,is_day",
        "hourly": "temperature_2m,precipitation_probability,weather_code,is_day",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset",
        "forecast_days": 6,
    })
    with urllib.request.urlopen("https://api.open-meteo.com/v1/forecast?" + q, timeout=10) as r:
        raw = json.loads(r.read().decode("utf-8"))

    cur = raw["current"]
    hourly = raw["hourly"]
    this_hour = cur["time"][:13] + ":00"
    times = hourly["time"]
    start = times.index(this_hour) + 1 if this_hour in times else 0
    hours = []
    for i in range(start, min(start + 8, len(times))):
        hours.append({
            "h": int(times[i][11:13]),
            "t": round(hourly["temperature_2m"][i]),
            "code": hourly["weather_code"][i],
            "day": hourly["is_day"][i],
            "rain": hourly["precipitation_probability"][i],
        })
    d = raw["daily"]
    days = []
    for i in range(len(d["time"])):
        days.append({
            "date": d["time"][i],
            "max": round(d["temperature_2m_max"][i]),
            "min": round(d["temperature_2m_min"][i]),
            "code": d["weather_code"][i],
            "rain": d["precipitation_probability_max"][i],
        })
    return {
        "city": w["city"],
        "updated": cur["time"][11:16],
        "cur": {
            "t": round(cur["temperature_2m"]),
            "feel": round(cur["apparent_temperature"]),
            "hum": cur["relative_humidity_2m"],
            "wind": round(cur["wind_speed_10m"]),
            "code": cur["weather_code"],
            "day": cur["is_day"],
        },
        "sunrise": d["sunrise"][0][11:16],
        "sunset": d["sunset"][0][11:16],
        "hours": hours,
        "days": days,
    }


def get_weather():
    with _weather_lock:
        if _weather_cache["data"] and time.time() - _weather_cache["at"] < WEATHER_TTL:
            return _weather_cache["data"]
        try:
            _weather_cache["data"] = fetch_weather()
            _weather_cache["at"] = time.time()
        except Exception as e:  # mat mang: tra ban cu neu co
            print("Loi lay thoi tiet: %r" % e)
            if not _weather_cache["data"]:
                return None
        return _weather_cache["data"]


# ---------------- Thong so may tinh (CPU, RAM, o dia) ----------------
kernel32 = ctypes.windll.kernel32
kernel32.GetTickCount64.restype = ctypes.c_ulonglong


class FILETIME(ctypes.Structure):
    _fields_ = [("lo", wintypes.DWORD), ("hi", wintypes.DWORD)]


class MEMSTAT(ctypes.Structure):
    _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


_cpu = {"pct": 0, "hist": []}


def _cpu_sampler():
    prev = None
    while True:
        i, k, u = FILETIME(), FILETIME(), FILETIME()
        kernel32.GetSystemTimes(ctypes.byref(i), ctypes.byref(k), ctypes.byref(u))
        cur = [(f.hi << 32) | f.lo for f in (i, k, u)]
        if prev:
            idle = cur[0] - prev[0]
            total = (cur[1] - prev[1]) + (cur[2] - prev[2])  # kernel da gom ca idle
            pct = 0 if total <= 0 else max(0, min(100, round(100.0 * (total - idle) / total)))
            _cpu["pct"] = pct
            _cpu["hist"] = (_cpu["hist"] + [pct])[-60:]
        prev = cur
        time.sleep(1)


def get_stats():
    m = MEMSTAT()
    m.dwLength = ctypes.sizeof(MEMSTAT)
    kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    disks = []
    mask = kernel32.GetLogicalDrives()
    for n in range(26):
        if mask & (1 << n):
            root = "%s:\\" % chr(65 + n)
            if kernel32.GetDriveTypeW(root) != 3:  # chi lay o cung co dinh
                continue
            try:
                du = shutil.disk_usage(root)
            except OSError:
                continue
            disks.append({"name": root[:2], "used": du.used, "total": du.total})
    return {
        "cpu": _cpu["pct"], "cpuHist": list(_cpu["hist"]),
        "ramUsed": m.ullTotalPhys - m.ullAvailPhys, "ramTotal": m.ullTotalPhys,
        "disks": disks, "uptime": kernel32.GetTickCount64() // 1000,
        "host": socket.gethostname(),
    }


# ---------------- Viec can lam (luu trong todos.json) ----------------
_todo_lock = threading.Lock()


def load_todos():
    try:
        with open(TODO_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def save_todos(todos):
    tmp = TODO_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(todos, f, ensure_ascii=False, indent=1)
    os.replace(tmp, TODO_PATH)


def todo_action(action, body):
    with _todo_lock:
        todos = load_todos()
        if action == "list":
            return todos
        if action == "add":
            text = str(body.get("text", "")).strip()[:200]
            if text:
                new_id = max([t["id"] for t in todos] + [0]) + 1
                todos.append({"id": new_id, "text": text, "done": False})
        elif action == "toggle":
            for t in todos:
                if t["id"] == body.get("id"):
                    t["done"] = not t["done"]
        elif action == "del":
            todos = [t for t in todos if t["id"] != body.get("id")]
        elif action == "clear":
            todos = [t for t in todos if not t["done"]]
        else:
            return None
        save_todos(todos)
        return todos


# ---------------- HTTP ----------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stdout.write("[%s] %s\n" % (self.address_string(), fmt % args))

    def send(self, code, body, ctype="application/json; charset=utf-8", cache=False):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=86400" if cache else "no-store")
        self.end_headers()
        self.wfile.write(data)

    def authed(self):
        pin = self.headers.get("X-Pin", "")
        if not pin:  # anh/file mo bang the <img>, <a> khong gui header duoc -> doc cookie
            try:
                c = SimpleCookie(self.headers.get("Cookie", ""))
                if "pcpin" in c:
                    pin = urllib.parse.unquote(c["pcpin"].value)
            except CookieError:
                pass
        return hmac.compare_digest(pin.encode(), str(CONFIG["pin"]).encode())

    def send_file(self, p):
        """Gui file, ho tro Range (Safari tren iPad can de phat video)."""
        size = os.path.getsize(p)
        start, end, status = 0, size - 1, 200
        m = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range", "").strip())
        if m and size and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 206
        self.send_response(status)
        self.send_header("Content-Type", mimetypes.guess_type(p)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.send_header("Content-Disposition", "inline; filename*=UTF-8''" + urllib.parse.quote(os.path.basename(p)))
        self.end_headers()
        try:
            with open(p, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except ConnectionError:
            pass

    def hub_get(self, path):
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        box = qs.get("box", [""])[0]
        rel = qs.get("p", [""])[0]
        what = path[len("/api/hub/"):]
        if what == "list":
            r = HUB.list(box)
            return self.send(200, json.dumps(r, ensure_ascii=False)) if r else self.send(404, "{}")
        if what == "note":
            return self.send(200, json.dumps(HUB.get_note(), ensure_ascii=False))
        if what == "thumb":
            data = HUB.thumb(box, rel)
            if data:
                return self.send(200, data, "image/jpeg", cache=True)
            what = "file"  # khong lam duoc anh nho -> gui anh goc
        if what == "file":
            p = HUB.path(box, rel)
            return self.send_file(p) if p else self.send(404, '{"error":"Không tìm thấy"}')
        self.send(404, "{}")

    def read_body(self, limit=4096):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            return None
        body = {}
        if n:
            try:
                body = json.loads(self.rfile.read(n).decode("utf-8"))
            except ValueError:
                body = {}
        return body if isinstance(body, dict) else {}

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in PAGES:
            with open(PAGES[path], "rb") as f:
                self.send(200, f.read(), "text/html; charset=utf-8")
        elif path.startswith("/api/books"):
            if not self.authed():
                return self.send(401, '{"error":"Sai mã PIN"}')
            self.books_get(path)
        elif path.startswith("/api/hub/"):
            if not self.authed():
                return self.send(401, '{"error":"Sai mã PIN"}')
            self.hub_get(path)
        elif path == "/api/stats":
            self.send(200, json.dumps(get_stats()))
        elif path == "/api/weather":
            # Khong can PIN: chi la thong tin thoi tiet cong khai
            data = get_weather()
            body = {"now": int(time.time() * 1000), "weather": data}
            self.send(200 if data else 502, json.dumps(body, ensure_ascii=False))
        else:
            self.send(404, '{"error":"not found"}')

    def books_get(self, path):
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        parts = path.split("/")[3:]
        if not parts or not parts[0]:
            return self.send(200, json.dumps(LIB.list(), ensure_ascii=False))
        bid = parts[0]
        what = parts[1] if len(parts) > 1 else "info"
        n = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        notfound = '{"error":"Không tìm thấy"}'
        if what == "info":
            r = LIB.info(bid)
            return self.send(200, json.dumps(r, ensure_ascii=False)) if r else self.send(404, notfound)
        if what == "ch":
            h = LIB.chapter(bid, n)
            return self.send(200, json.dumps({"html": h}, ensure_ascii=False)) if h is not None else self.send(404, notfound)
        if what in ("res", "cover"):
            r = LIB.resource(bid, qs.get("p", [""])[0]) if what == "res" else LIB.cover(bid)
            return self.send(200, r[0], r[1], cache=True) if r else self.send(404, notfound)
        if what == "page":
            data = LIB.page(bid, n, qs.get("w", ["1024"])[0] or "1024")
            return self.send(200, data, "image/jpeg", cache=True) if data else self.send(404, notfound)
        if what == "file":
            p = LIB.file_path(bid)
            return self.send_file(p) if p else self.send(404, notfound)
        self.send(404, notfound)

    def do_POST(self):
        if not self.path.startswith("/api/"):
            return self.send(404, '{"error":"not found"}')
        if not self.authed():
            return self.send(401, '{"error":"Sai mã PIN"}')
        parts = self.path[5:].split("?")[0].split("/")
        action = parts[0]
        if action == "apps":
            names = [a["name"] for a in CONFIG["apps"]]
            icons = [a.get("icon", "") for a in CONFIG["apps"]]
            return self.send(200, json.dumps({"apps": names, "icons": icons}, ensure_ascii=False))
        if action == "hub":
            sub = parts[1] if len(parts) > 1 else ""
            if sub == "upload":
                n = int(self.headers.get("Content-Length") or 0)
                if n <= 0:
                    return self.send(411, '{"error":"Thiếu dữ liệu"}')
                name = urllib.parse.unquote(self.headers.get("X-Filename", ""))
                try:
                    saved = HUB.save_upload(self.rfile, n, name, self.headers.get("Content-Type", ""))
                except (ConnectionError, OSError) as e:
                    print("Loi nhan file: %r" % e)
                    return self.send(500, '{"error":"Gửi bị lỗi"}')
                return self.send(200, json.dumps({"saved": saved}, ensure_ascii=False))
            body = self.read_body(limit=200000)
            if body is None:
                return self.send(413, '{"error":"Quá dài"}')
            if sub == "note":
                return self.send(200, json.dumps(HUB.set_note(body.get("text", "")), ensure_ascii=False))
            if sub == "open" and body.get("box") in HUB.box:
                os.startfile(HUB.box[body["box"]])
                return self.send(200, '{"msg":"Đã mở thư mục trên máy tính"}')
            return self.send(404, '{"error":"not found"}')
        if action in ("todos", "books"):
            body = self.read_body()
            if body is None:
                return self.send(413, '{"error":"Quá dài"}')
        if action == "books":
            if len(parts) == 3 and parts[2] == "pos":
                try:
                    LIB.save_pos(parts[1], body.get("ch", 0), body.get("frac", 0))
                except (TypeError, ValueError):
                    return self.send(400, '{"error":"Dữ liệu sai"}')
                return self.send(200, '{"ok":true}')
            return self.send(404, '{"error":"not found"}')
        if action == "todos":
            todos = todo_action(parts[1] if len(parts) > 1 else "list", body)
            if todos is None:
                return self.send(400, '{"error":"Lệnh không hợp lệ"}')
            return self.send(200, json.dumps({"todos": todos}, ensure_ascii=False))
        count = 1
        if len(parts) > 1 and parts[1].isdigit():
            count = int(parts[1])
        msg = do_action(action, count)
        if msg is None:
            return self.send(400, '{"error":"Lệnh không hợp lệ"}')
        self.send(200, json.dumps({"msg": msg}, ensure_ascii=False))


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


if __name__ == "__main__":
    port = int(CONFIG["port"])
    # Tren Windows, reuse_address cho phep 2 server cung cong -> tat di de chi chay 1 ban
    ThreadingHTTPServer.allow_reuse_address = False
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    except OSError:
        print("Cong %d dang duoc dung - co the server da chay roi." % port)
        sys.exit(1)
    threading.Thread(target=_cpu_sampler, daemon=True).start()
    print("=" * 46)
    print(" PC Remote dang chay")
    print(" Mo tren iPad:  http://%s:%d" % (lan_ip(), port))
    print(" Ma PIN:        %s   (doi trong config.json)" % CONFIG["pin"])
    print(" Tat server:    Ctrl+C")
    print("=" * 46)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
