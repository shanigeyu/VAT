"""Find illustrations published with the selected GitHub topic."""
import hashlib
import io
import json
import os
import re
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, unquote, urlencode, urljoin, urlsplit
from urllib.request import Request, urlopen

from PIL import Image, ImageOps
import task_control


IMAGE_LINK = re.compile(r'!\[([^\]]*)\]\(\s*<?([^\s)>]+)>?(?:\s+["\'][^)]*)?\)')
DOC_LINK = re.compile(r'(?<!!)\[([^\]\n]{2,90})\]\(([^():\s#]+\.md)(?:#[^)]*)?\)', re.I)
DECORATION = re.compile(r'badge|shields\.io|qr[-_ ]?code|wechat|weixin|avata[r]|contributors?|sponsors?|收款|赞赏|二维码|交流群|公众号|徽章', re.I)
RAW = "https://raw.githubusercontent.com/"


class HtmlImages(HTMLParser):
    def __init__(self):
        super().__init__()
        self.images = []

    def handle_starttag(self, tag, attrs):
        if tag == "img":
            data = dict(attrs)
            self.images.append((data.get("alt") or "", data.get("src") or ""))


def repo_name(url):
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return ""
    match = re.fullmatch(r"/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)(?:/.*)?", parts.path)
    if parts.scheme == "https" and parts.netloc.lower() == "github.com" and match:
        owner, name = match.groups()
        if owner not in {".", ".."} and name not in {".", ".."}:
            return owner + "/" + name.removesuffix(".git")
    return ""


def fetch(url, limit):
    request = Request(url, headers={"User-Agent": "VAT-video-editor", "Accept": "*/*"})
    def read():
        with urlopen(request, timeout=5) as response:
            return response.read(limit + 1)
    data = task_control.network_call(read)
    if len(data) > limit:
        raise ValueError("来源文件过大")
    return data


def pool_map(pool, function, values):
    futures = [pool.submit(copy_context().run, function, value) for value in values]
    for future in futures:
        task_control.checkpoint()
        yield future.result()


def source_url(base, target):
    url = urljoin(base, unescape(target))
    parts = urlsplit(url)
    if parts.scheme != "https":
        return ""
    host = parts.netloc.lower()
    if host == "github.com" and "/blob/" in parts.path:
        url = RAW + parts.path.lstrip("/").replace("/blob/", "/", 1)
    elif host not in {"raw.githubusercontent.com", "user-images.githubusercontent.com"}:
        if host != "github.com" or not parts.path.startswith("/user-attachments/assets/"):
            return ""
    return quote(url, safe=":/%?=&@+,-_.~")


def document_images(base, content, title):
    html = HtmlImages()
    html.feed(content)
    for alt, target in IMAGE_LINK.findall(content) + html.images:
        if not target or DECORATION.search(alt + " " + target):
            continue
        url = source_url(base, target)
        if url and not urlsplit(url).path.lower().endswith((".svg", ".ico")):
            name = re.sub(r"[_-]+", " ", Path(unquote(urlsplit(url).path)).stem)
            yield {"url": url, "label": alt.strip() or title or name, "context": title, "document": base}


def linked_images(item):
    title, url = item
    try:
        content = fetch(url, 750_000).decode("utf-8-sig")
        return list(document_images(url, content, title))[:3]
    except (OSError, ValueError):
        return []


def save_image(folder, item):
    url = item["url"]
    filename = hashlib.sha256(url.encode()).hexdigest()[:24] + ".jpg"
    path = folder / filename
    try:
        if not path.is_file():
            data = fetch(url, 9_000_000)
            with Image.open(io.BytesIO(data)) as raw:
                w, h = raw.size
                if min(w, h) < 160 or w * h > 22_000_000 or max(w / h, h / w) > 6:
                    return None
                rgb = ImageOps.exif_transpose(raw).convert("RGBA")
                rgb.thumbnail((2200, 2200), Image.Resampling.LANCZOS)
                picture = Image.new("RGB", rgb.size, "white")
                picture.paste(rgb, mask=rgb.getchannel("A"))
                with tempfile.TemporaryDirectory(dir=folder) as tmp:
                    ready = Path(tmp) / "picture.jpg"
                    picture.save(ready, "JPEG", quality=94)
                    os.replace(ready, path)
        return {**item, "file": filename}
    except (OSError, ValueError, Image.DecompressionBombError):
        return None


def cached_images(manifest):
    try:
        rows = json.loads(manifest.read_text(encoding="utf-8"))
        return [row for row in rows if isinstance(row, dict) and Path(row["file"]).name == row["file"]
                and (manifest.parent / row["file"]).is_file() and isinstance(row["url"], str)
                and isinstance(row["label"], str) and isinstance(row.get("context"), str)]
    except (OSError, KeyError, TypeError, ValueError):
        return []


def repository_images(root, repo, report):
    if not repo:
        return []
    owner, name = repo.split("/")
    folder = Path(root) / "选题配图" / owner / name
    folder.mkdir(parents=True, exist_ok=True)
    manifest = folder / "来源.json"
    pages = cached_images(folder / "网页取景.json")
    previous = cached_images(manifest)
    try:
        if previous and time.time() - manifest.stat().st_mtime < 7 * 86400:
            return [{**r, "path": folder / r["file"]} for r in pages + previous]
    except OSError:
        pass
    url = RAW + repo + "/HEAD/README.md"
    try:
        report(f"正在从 {repo} 的项目说明和关联文档收集配图")
        content = fetch(url, 900_000).decode("utf-8-sig")
        items = list(document_images(url, content, ""))[:10]
        documents = {}
        for title, target in DOC_LINK.findall(content):
            link = source_url(url, target)
            if link.startswith(RAW + repo + "/") and "/" in target and not DECORATION.search(title):
                documents.setdefault(link, title)
        with ThreadPoolExecutor(max_workers=4) as pool:
            for found in pool_map(pool, linked_images, [(t, u) for u, t in list(documents.items())[:5]]):
                items.extend(found)
            unique = {row["url"]: row for row in reversed(items)}
            chosen = [unique[u] for u in dict.fromkeys(r["url"] for r in items)][:10]
            downloaded = [r for r in pool_map(pool, lambda row: save_image(folder, row), chosen) if r]
        if downloaded:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=folder, delete=False) as tmp:
                json.dump(downloaded, tmp, ensure_ascii=False, indent=2)
            os.replace(tmp.name, manifest)
            previous = downloaded
    except (OSError, ValueError):
        report("项目配图暂时不可用，正在使用本机已缓存的图片和定稿图解")
    return [{**r, "path": folder / r["file"]} for r in pages + previous]


def public_topic_images(root, topic, report):
    title = (topic.get("title") or "").strip()[:85]
    if not title or not re.match(r"https?://", topic.get("url") or "", re.I):
        return []
    folder = Path(root) / "选题配图" / "Commons" / hashlib.sha256(title.encode()).hexdigest()[:20]
    folder.mkdir(parents=True, exist_ok=True)
    manifest = folder / "来源.json"
    pictures = cached_images(manifest)
    if not pictures:
        report("正在按公开选题标题查找 Wikimedia Commons 的公有领域和 CC0 配图")
        params = {"action": "query", "format": "json", "generator": "search", "gsrnamespace": 6,
                  "gsrsearch": title + " filetype:bitmap", "gsrlimit": 30, "prop": "imageinfo",
                  "iiprop": "mime|url|extmetadata", "iiurlwidth": 1280, "iiextmetadatafilter": "LicenseShortName"}
        try:
            data = json.loads(fetch("https://commons.wikimedia.org/w/api.php?" + urlencode(params), 750_000))
            candidates = []
            for row in sorted(data.get("query", {}).get("pages", {}).values(), key=lambda r: r.get("index", 0)):
                info = (row.get("imageinfo") or [{}])[0]
                license_name = info.get("extmetadata", {}).get("LicenseShortName", {}).get("value", "")
                url = info.get("thumburl") or info.get("url", "")
                if not re.search(r"^(?:public domain|CC0(?: 1\.0)?)$", license_name, re.I):
                    continue
                if urlsplit(url).netloc != "upload.wikimedia.org" or not url.startswith("https://"):
                    continue
                label = Path(row["title"].removeprefix("File:")).stem.replace("_", " ")
                candidates.append({"url": url, "document": info.get("descriptionurl", ""), "label": label,
                                   "context": title, "kind": "commons", "license": license_name})
            with ThreadPoolExecutor(max_workers=3) as pool:
                pictures = [r for r in pool_map(pool, lambda row: save_image(folder, row), candidates[:5]) if r]
            if pictures:
                with tempfile.TemporaryDirectory(dir=folder) as scratch:
                    ready = Path(scratch) / "source.json"
                    ready.write_text(json.dumps(pictures, ensure_ascii=False, indent=2), encoding="utf-8")
                    os.replace(ready, manifest)
        except (OSError, ValueError):
            report("公共配图暂不可用，正在用已确认的台词制作图解")
    return [{**row, "path": folder / row["file"]} for row in pictures]
