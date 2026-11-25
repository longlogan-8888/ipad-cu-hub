# -*- coding: utf-8 -*-
"""
Thu vien sach cho PC Remote: quet thu muc, doc EPUB (chi dung thu vien co san),
PDF thi neu co PyMuPDF se ve tung trang thanh anh, khong co thi mo bang Safari.
"""
import hashlib
import html
import io
import json
import mimetypes
import os
import posixpath
import threading
import time
import urllib.parse
import zipfile
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

try:
    import pymupdf as fitz  # PyMuPDF - tuy chon
except ImportError:
    try:
        import fitz
    except ImportError:
        fitz = None

EXTS = (".epub", ".pdf")


def _local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _text(el):
    return " ".join("".join(el.itertext()).split())


# ---------------- EPUB ----------------
def parse_epub(path):
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        cont = ET.fromstring(z.read("META-INF/container.xml"))
        opf_path = next(el.get("full-path") for el in cont.iter() if _local(el.tag) == "rootfile")
        opf = ET.fromstring(z.read(opf_path))
        base = posixpath.dirname(opf_path)

        def res(href, rel=base):
            href = urllib.parse.unquote(href.split("#")[0])
            return posixpath.normpath(posixpath.join(rel, href)) if href else ""

        title = author = cover_id = toc_id = None
        manifest, spine = {}, []
        for el in opf.iter():
            t = _local(el.tag)
            if t == "title" and title is None and el.text and el.text.strip():
                title = el.text.strip()
            elif t == "creator" and author is None and el.text and el.text.strip():
                author = el.text.strip()
            elif t == "meta" and el.get("name") == "cover":
                cover_id = el.get("content")
            elif t == "item":
                manifest[el.get("id")] = {"href": res(el.get("href", "")),
                                          "type": el.get("media-type", ""),
                                          "props": el.get("properties", "")}
            elif t == "spine":
                toc_id = el.get("toc")
            elif t == "itemref":
                spine.append(el.get("idref"))

        chapters = [manifest[i]["href"] for i in spine if i in manifest and manifest[i]["href"] in names]

        cover = None
        if cover_id in manifest and manifest[cover_id]["type"].startswith("image"):
            cover = manifest[cover_id]["href"]
        else:
            for m in manifest.values():
                if "cover-image" in m["props"].split():
                    cover = m["href"]
                    break
        if cover not in names:
            cover = None

        # Muc luc: EPUB2 (toc.ncx) hoac EPUB3 (nav)
        titles = {}
        try:
            if toc_id in manifest:
                ncx_path = manifest[toc_id]["href"]
                ncx_dir = posixpath.dirname(ncx_path)
                for np in ET.fromstring(z.read(ncx_path)).iter():
                    if _local(np.tag) != "navPoint":
                        continue
                    label = src = None
                    for ch in np:
                        if _local(ch.tag) == "navLabel":
                            label = _text(ch)
                        elif _local(ch.tag) == "content":
                            src = res(ch.get("src", ""), ncx_dir)
                    if label and src:
                        titles.setdefault(src, label)
            else:
                nav = next((m for m in manifest.values() if "nav" in m["props"].split()), None)
                if nav:
                    nav_dir = posixpath.dirname(nav["href"])
                    for a in ET.fromstring(z.read(nav["href"])).iter():
                        if _local(a.tag) == "a" and a.get("href"):
                            titles.setdefault(res(a.get("href"), nav_dir), _text(a))
        except Exception:
            pass
        toc = [{"ch": i, "title": titles[h]} for i, h in enumerate(chapters) if titles.get(h)]

    return {"title": title, "author": author, "chapters": chapters, "cover": cover, "toc": toc}


ALLOWED = {"p", "div", "span", "h1", "h2", "h3", "h4", "h5", "h6", "em", "i", "b", "strong", "u",
           "blockquote", "ul", "ol", "li", "sup", "sub", "small", "pre", "code", "table", "thead",
           "tbody", "tr", "td", "th", "figure", "figcaption", "section", "article", "a", "dl", "dt",
           "dd", "center", "aside", "header", "footer", "cite", "q", "s", "big"}
SKIP = {"script", "style", "head", "title", "iframe", "object", "embed", "form", "input",
        "button", "noscript", "audio", "video", "math"}


class Cleaner(HTMLParser):
    """Giu lai phan chu va anh, bo het script/style/thuoc tinh la."""

    def __init__(self, resolve):
        super().__init__(convert_charrefs=True)
        self.out, self.skip, self.resolve = [], 0, resolve

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in SKIP:
            self.skip += 1
            return
        if self.skip:
            return
        if tag in ("img", "image"):
            a = dict(attrs)
            src = a.get("src") or a.get("href") or a.get("xlink:href")
            if src:
                self.out.append('<img src="%s" alt="">' % html.escape(self.resolve(src)))
        elif tag in ("br", "hr"):
            self.out.append("<%s>" % tag)
        elif tag in ALLOWED:
            self.out.append("<%s>" % tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if not self.skip and tag in ALLOWED:
            self.out.append("</%s>" % tag)

    def handle_data(self, data):
        if not self.skip:
            self.out.append(html.escape(data, quote=False))


# ---------------- Thu vien ----------------
class Library:
    def __init__(self, folders, pos_path):
        self.folders = folders
        self.pos_path = pos_path
        self.lock = threading.Lock()
        self.cache = {}   # path -> (mtime, meta)
        self.by_id = {}   # id -> path
        self.pages = {}   # (id, page, w) -> jpeg (bo nho dem nho)
        self.page_order = []

    # --- vi tri doc ---
    def _load_pos(self):
        try:
            with open(self.pos_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def save_pos(self, bid, ch, frac):
        with self.lock:
            pos = self._load_pos()
            pos[bid] = {"ch": int(ch), "frac": max(0.0, min(1.0, float(frac))), "t": int(time.time())}
            tmp = self.pos_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(pos, f)
            os.replace(tmp, self.pos_path)

    # --- quet sach ---
    def meta(self, path):
        mtime = os.path.getmtime(path)
        c = self.cache.get(path)
        if c and c[0] == mtime:
            return c[1]
        bid = hashlib.sha1(os.path.abspath(path).lower().encode("utf-8")).hexdigest()[:12]
        name = os.path.splitext(os.path.basename(path))[0]
        m = {"id": bid, "path": path, "file": os.path.basename(path), "title": name, "author": None}
        if path.lower().endswith(".epub"):
            m["type"] = "epub"
            try:
                e = parse_epub(path)
                m.update(title=e["title"] or name, author=e["author"], chapters=e["chapters"],
                         cover=e["cover"], toc=e["toc"], total=len(e["chapters"]))
                m["mode"] = "reader" if e["chapters"] else "broken"
            except Exception as ex:
                m.update(mode="broken", total=0, error=str(ex))
        else:
            m["type"] = "pdf"
            m["cover"] = None
            m["toc"] = []
            if fitz:
                try:
                    with fitz.open(path) as d:
                        m["total"] = d.page_count
                        m["toc"] = [{"ch": p - 1, "title": t} for lv, t, p in d.get_toc() if lv <= 2 and p >= 1]
                    m["mode"] = "reader"
                except Exception as ex:
                    m.update(mode="native", total=0, error=str(ex))
            else:
                m.update(mode="native", total=0)
        self.cache[path] = (mtime, m)
        self.by_id[bid] = path
        return m

    def list(self):
        pos = self._load_pos()
        out = []
        for folder in self.folders:
            if not os.path.isdir(folder):
                continue
            for root, _dirs, files in os.walk(folder):
                for fn in files:
                    if not fn.lower().endswith(EXTS):
                        continue
                    try:
                        m = self.meta(os.path.join(root, fn))
                    except OSError:
                        continue
                    p = pos.get(m["id"])
                    prog = 0
                    if p and m.get("total"):
                        prog = min(100, round(100.0 * (p["ch"] + p["frac"]) / m["total"]))
                    out.append({"id": m["id"], "title": m["title"], "author": m["author"],
                                "type": m["type"], "mode": m["mode"], "cover": bool(m["cover"]),
                                "progress": prog, "last": p["t"] if p else 0})
        out.sort(key=lambda b: (-b["last"], b["title"].lower()))
        return {"books": out, "folders": self.folders, "pdfReader": bool(fitz)}

    def get(self, bid):
        path = self.by_id.get(bid)
        if not path:
            self.list()
            path = self.by_id.get(bid)
        return self.meta(path) if path and os.path.exists(path) else None

    def info(self, bid):
        m = self.get(bid)
        if not m:
            return None
        return {"id": bid, "title": m["title"], "author": m["author"], "type": m["type"],
                "mode": m["mode"], "total": m.get("total", 0), "toc": m.get("toc", []),
                "pos": self._load_pos().get(bid)}

    def chapter(self, bid, n):
        m = self.get(bid)
        if not m or m["type"] != "epub" or not (0 <= n < len(m["chapters"])):
            return None
        href = m["chapters"][n]
        cdir = posixpath.dirname(href)

        def resolve(src):
            p = posixpath.normpath(posixpath.join(cdir, urllib.parse.unquote(src.split("#")[0])))
            return "/api/books/%s/res?p=%s" % (bid, urllib.parse.quote(p))

        with zipfile.ZipFile(m["path"]) as z:
            raw = z.read(href).decode("utf-8", "replace")
        c = Cleaner(resolve)
        c.feed(raw)
        c.close()
        return "".join(c.out)

    def resource(self, bid, p):
        m = self.get(bid)
        if not m or m["type"] != "epub":
            return None
        with zipfile.ZipFile(m["path"]) as z:
            if p not in z.namelist():
                return None
            data = z.read(p)
        return data, mimetypes.guess_type(p)[0] or "application/octet-stream"

    def cover(self, bid):
        m = self.get(bid)
        if not m or not m.get("cover"):
            return None
        return self.resource(bid, m["cover"])

    def page(self, bid, n, w):
        m = self.get(bid)
        if not fitz or not m or m["type"] != "pdf" or m["mode"] != "reader":
            return None
        w = max(400, min(int(w or 1024), 1600))
        key = (bid, n, w)
        if key in self.pages:
            return self.pages[key]
        with fitz.open(m["path"]) as d:
            if not (0 <= n < d.page_count):
                return None
            pg = d[n]
            z = w / pg.rect.width
            pix = pg.get_pixmap(matrix=fitz.Matrix(z, z), alpha=False)
            data = pix.tobytes("jpeg")
        with self.lock:
            self.pages[key] = data
            self.page_order.append(key)
            while len(self.page_order) > 30:
                self.pages.pop(self.page_order.pop(0), None)
        return data

    def file_path(self, bid):
        m = self.get(bid)
        return m["path"] if m else None
