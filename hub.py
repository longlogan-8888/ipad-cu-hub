# -*- coding: utf-8 -*-
"""
Hub trung chuyen du lieu giua iPad va may tinh:
  - inbox : anh/video gui tu iPad len (chia theo ngay)
  - outbox: file tren may tinh de iPad xem / luu anh
  - note  : ghi chu dung chung (clipboard)
"""
import json
import mimetypes
import os
import re
import threading
import time

try:
    import pymupdf as fitz
except ImportError:
    try:
        import fitz
    except ImportError:
        fitz = None

GENERIC = re.compile(r"^(image|images|video|capturedvideo|trim\..*|photo)$", re.I)
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff"}
VIDEO_EXT = {".mov", ".mp4", ".m4v", ".3gp"}


def kind_of(name):
    ext = os.path.splitext(name)[1].lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in VIDEO_EXT:
        return "video"
    if ext in (".pdf", ".epub"):
        return "book"
    return "other"


class Hub:
    def __init__(self, inbox, outbox, note_path):
        self.box = {"inbox": inbox, "outbox": outbox}
        self.note_path = note_path
        self.lock = threading.Lock()
        self.thumbs = {}
        self.thumb_order = []
        for p in self.box.values():
            os.makedirs(p, exist_ok=True)

    # ---------- duong dan an toan ----------
    def path(self, box, rel):
        root = self.box.get(box)
        if not root or not rel:
            return None
        p = os.path.normpath(os.path.join(root, rel))
        if os.path.commonpath([p, root]) != root or not os.path.isfile(p):
            return None
        return p

    # ---------- danh sach ----------
    def list(self, box, limit=200):
        root = self.box.get(box)
        if not root:
            return None
        items = []
        for d, _dirs, files in os.walk(root):
            for fn in files:
                if fn.startswith(".") or fn.lower() in ("desktop.ini", "thumbs.db") or fn.endswith(".part"):
                    continue
                p = os.path.join(d, fn)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                items.append({"path": os.path.relpath(p, root).replace("\\", "/"), "name": fn,
                              "size": st.st_size, "t": int(st.st_mtime), "kind": kind_of(fn)})
        items.sort(key=lambda x: -x["t"])
        return {"items": items[:limit], "total": len(items), "folder": root}

    # ---------- nhan file tu iPad ----------
    def save_upload(self, rfile, length, filename, ctype):
        filename = os.path.basename((filename or "").replace("\\", "/"))
        base, ext = os.path.splitext(filename)
        ext = ext.lower()
        if not re.match(r"^\.[a-z0-9]{1,5}$", ext):
            ext = mimetypes.guess_extension((ctype or "").split(";")[0].strip()) or ".bin"
        if ext in (".jpe", ".jfif"):
            ext = ".jpg"
        base = re.sub(r"[^\w\-. ]", "_", base).strip(" .")[:60]
        tag = "" if (not base or GENERIC.match(base)) else "_" + base
        folder = os.path.join(self.box["inbox"], time.strftime("%Y-%m-%d"))
        os.makedirs(folder, exist_ok=True)
        stamp = time.strftime("%H%M%S")
        with self.lock:
            i = 1
            while True:
                final = os.path.join(folder, "%s_%02d%s%s" % (stamp, i, tag, ext))
                if not os.path.exists(final) and not os.path.exists(final + ".part"):
                    break
                i += 1
            part = final + ".part"
            open(part, "wb").close()
        remaining = length
        try:
            with open(part, "wb") as f:
                while remaining > 0:
                    chunk = rfile.read(min(1 << 20, remaining))
                    if not chunk:
                        raise ConnectionError("upload bi ngat giua chung")
                    f.write(chunk)
                    remaining -= len(chunk)
            os.replace(part, final)
        except Exception:
            try:
                os.remove(part)
            except OSError:
                pass
            raise
        return os.path.relpath(final, self.box["inbox"]).replace("\\", "/")

    # ---------- anh thu nho ----------
    def thumb(self, box, rel, size=360):
        p = self.path(box, rel)
        if not p or kind_of(p) != "image" or not fitz:
            return None
        key = (p, os.path.getmtime(p), size)
        if key in self.thumbs:
            return self.thumbs[key]
        try:
            with fitz.open(p) as doc:
                pg = doc[0]
                z = min(1.0, float(size) / max(pg.rect.width, pg.rect.height))
                data = pg.get_pixmap(matrix=fitz.Matrix(z, z), alpha=False).tobytes("jpeg")
        except Exception:
            return None
        with self.lock:
            self.thumbs[key] = data
            self.thumb_order.append(key)
            while len(self.thumb_order) > 300:
                self.thumbs.pop(self.thumb_order.pop(0), None)
        return data

    # ---------- ghi chu chung ----------
    def get_note(self):
        try:
            with open(self.note_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {"text": "", "t": 0}

    def set_note(self, text):
        n = {"text": str(text)[:50000], "t": int(time.time())}
        with self.lock:
            tmp = self.note_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(n, f, ensure_ascii=False)
            os.replace(tmp, self.note_path)
        return n
