#!/usr/bin/env python3
"""Keep the static Theology and AI site in sync with the Blogger blog.

What it does, in order:
  1. Reads the blog's Atom feed and finds posts that are new or edited.
  2. Saves each post as content/posts/<year>-<month>-<slug>.json.
  3. Rebuilds every generated page (index.html, the blog folder and so on) from those files, so the home page,
     blog index, topic pages, previous/next links, feed.xml and sitemap.xml all pick
     up the new post at once.
  4. Checks every internal link, image and sitemap entry and reports problems.

Usage:
  python tools/update_site.py              fetch, add new posts, rebuild, verify
  python tools/update_site.py --check      only report what is new (changes nothing)
  python tools/update_site.py --rebuild    rebuild from content/ without network
  python tools/update_site.py --verify     only run the link checker
  python tools/update_site.py --external   also check external links (needs network)
  python tools/update_site.py --feed-file saved_feed.xml   use a saved feed file

Only the Python standard library is used. Built for Python 3.15 (also tested on 3.13).
"""
from __future__ import annotations

import argparse
import calendar
import html
import json
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlparse

if sys.version_info < (3, 9):
    sys.exit("This script needs Python 3.9 or newer. Python 3.15 is recommended.")

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / "config.json"
POSTS_DIR = ROOT / "content" / "posts"
MANIFEST = ROOT / "content" / ".generated.json"
OUT = ROOT

NS = {
    "a": "http://www.w3.org/2005/Atom",
    "media": "http://search.yahoo.com/mrss/",
    "app": "http://www.w3.org/2007/app",
}
USER_AGENT = "theologyai-static-sync/1.0"


# ---------------------------------------------------------------- helpers

def load_config() -> dict:
    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    cfg["site_url"] = cfg["site_url"].rstrip("/")
    cfg["base_path"] = cfg.get("base_path", "").rstrip("/")
    return cfg


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or "post"


def parse_dt(value: str) -> datetime:
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    return datetime.fromisoformat(v)


def fmt_date(value: str) -> str:
    d = parse_dt(value)
    return f"{calendar.month_name[d.month]} {d.day}, {d.year}"


def esc(text: str) -> str:
    return html.escape(text, quote=True)


def strip_tags(markup: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", markup, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def excerpt(markup: str, limit: int = 210) -> str:
    paras = re.findall(r"<p[^>]*>(.*?)</p>", markup, flags=re.S | re.I)
    text = strip_tags(" ".join(paras)) if paras else ""
    if len(text) < 40:
        text = strip_tags(markup)
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(",;:.")
    return cut + "…"


def host_of(url: str) -> str:
    h = urlparse(url).netloc.lower()
    return h[4:] if h.startswith("www.") else h


# ---------------------------------------------------------------- feed sync

def parse_entry(e: ET.Element):
    def text(path):
        el = e.find(path, NS)
        return el.text.strip() if el is not None and el.text else ""

    draft = e.find("app:control/app:draft", NS)
    if draft is not None and (draft.text or "").strip().lower() == "yes":
        return None
    alt = None
    for link in e.findall("a:link", NS):
        if link.get("rel") == "alternate":
            alt = link.get("href")
    if not alt:
        return None
    published = text("a:published")
    path = urlparse(alt).path
    if not re.match(r"^/\d{4}/\d{2}/[^/]+\.html$", path):
        d = parse_dt(published)
        path = f"/{d.year}/{d.month:02d}/{slugify(text('a:title'))}.html"
    labels = [
        c.get("term")
        for c in e.findall("a:category", NS)
        if c.get("term") and "blogger.com/atom/ns#" in (c.get("scheme") or "")
    ]
    thumb = e.find("media:thumbnail", NS)
    return {
        "id": text("a:id"),
        "sample": False,
        "title": text("a:title") or "Untitled",
        "published": published,
        "updated": text("a:updated") or published,
        "url_path": path,
        "labels": labels,
        "thumb": thumb.get("url") if thumb is not None else "",
        "html": text("a:content"),
    }


class FeedError(Exception):
    """The blog did not give us a usable Atom feed."""


def fetch_feed_page(url: str):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/atom+xml, application/xml;q=0.9, */*;q=0.5",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read()
        ctype = resp.headers.get("Content-Type", "unknown")
    if b"<feed" not in data[:3000]:
        start = data.lstrip(b"\xef\xbb\xbf \r\n\t")[:150].decode("utf-8", "replace")
        start = re.sub(r"\s+", " ", start)
        raise FeedError(
            f"{url} did not return an Atom feed (content type {ctype}). "
            f"The response began with: {start!r}"
        )
    try:
        return ET.fromstring(data)
    except ET.ParseError as e:
        raise FeedError(f"{url} returned a feed that could not be parsed: {e}") from e


def read_feed_url(base_url: str) -> list:
    roots, start = [], 1
    while True:
        root = fetch_feed_page(f"{base_url}?alt=atom&max-results=150&start-index={start}")
        roots.append(root)
        if len(root.findall("a:entry", NS)) < 150:
            return roots
        start += 150


def read_feed(source: str, fallbacks=()) -> list:
    """source is a URL or a path to a saved feed file. Fallback URLs are tried in order."""
    if Path(source).exists():
        try:
            roots = [ET.fromstring(Path(source).read_bytes())]
        except ET.ParseError as e:
            raise FeedError(f"{source} is not a valid feed file: {e}") from e
    else:
        problems, roots = [], None
        for url in [source, *fallbacks]:
            try:
                roots = read_feed_url(url)
                if url != source:
                    print(f"  (used fallback feed {url})")
                break
            except (FeedError, urllib.error.URLError, OSError) as e:
                problems.append(f"{e}")
        if roots is None:
            raise FeedError("\n  ".join(problems))
    posts = []
    for root in roots:
        for e in root.findall("a:entry", NS):
            rec = parse_entry(e)
            if rec:
                posts.append(rec)
    return posts


def load_posts() -> dict:
    """Return {filename: record} for every saved post."""
    posts = {}
    for f in sorted(POSTS_DIR.glob("*.json")):
        posts[f.name] = json.loads(f.read_text(encoding="utf-8"))
    return posts


def post_filename(rec: dict) -> str:
    m = re.match(r"^/(\d{4})/(\d{2})/(.+)\.html$", rec["url_path"])
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}.json"


def sync(source: str, dry_run: bool, fallbacks=()) -> dict:
    fetched = read_feed(source, fallbacks)
    existing = load_posts()
    by_id = {rec["id"]: name for name, rec in existing.items()}
    new, changed = [], []
    for rec in fetched:
        name = by_id.get(rec["id"])
        if name is None:
            new.append(rec)
        elif existing[name]["updated"] != rec["updated"] or existing[name]["url_path"] != rec["url_path"]:
            changed.append((name, rec))
    report = {"fetched": len(fetched), "new": new, "changed": changed, "samples_removed": 0}
    if dry_run or not fetched:
        return report
    POSTS_DIR.mkdir(parents=True, exist_ok=True)
    for name, rec in existing.items():
        if rec.get("sample"):
            (POSTS_DIR / name).unlink()
            report["samples_removed"] += 1
    for rec in new:
        write_json(POSTS_DIR / post_filename(rec), rec)
    for name, rec in changed:
        (POSTS_DIR / name).unlink()
        write_json(POSTS_DIR / post_filename(rec), rec)
    return report


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- post body cleaning

ALLOWED = {
    "p", "br", "hr", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "strong", "em", "u", "s",
    "blockquote", "table", "thead", "tbody", "tfoot", "tr", "td", "th", "caption", "pre", "code",
    "sup", "sub", "a", "img", "div", "figure", "figcaption", "small",
}
RENAME = {"b": "strong", "i": "em", "h1": "h2", "center": "div", "strike": "s"}
DROP_WITH_CONTENT = {"script", "style", "noscript", "head", "object", "embed", "form", "button", "select", "textarea", "iframe"}
VOID = {"br", "hr", "img"}
VIDEO_HOSTS = {"youtube.com", "youtube-nocookie.com", "player.vimeo.com"}


def post_site_path(cfg: dict, blogger_path: str) -> str:
    """Map a Blogger address (/2026/10/name.html) to this site's address (/blog/2026/name.html)."""
    m = re.match(r"^/(\d{4})/\d{2}/([^/]+\.html)$", blogger_path)
    blog = "/" + cfg.get("blog_dir", "blog").strip("/")
    return f"{blog}/{m.group(1)}/{m.group(2)}" if m else blogger_path


def rewrite_url(url: str, cfg: dict, absolute: bool = False) -> str:
    """Point links that belong to this blog at the matching page on the new site."""
    url = url.strip()
    u = urlparse(url)
    own_hosts = {host_of(cfg["site_url"])} | {h.lower() for h in cfg.get("old_hosts", [])}
    is_own = (u.scheme in ("http", "https") and host_of(url) in own_hosts) or (
        not u.scheme and not u.netloc and url.startswith("/") and not url.startswith("//")
    )
    if not is_own:
        return url
    path = u.path or "/"
    blog = "/" + cfg.get("blog_dir", "blog").strip("/")
    m = re.match(r"^/search/label/([^/]+)", path)
    if m:
        path = f"/topics/{slugify(unquote(m.group(1)))}/"
    elif re.match(r"^/\d{4}/\d{2}/[^/]+\.html$", path):
        path = post_site_path(cfg, path)
    else:
        m = re.match(r"^/(\d{4})(?:/\d{2})?/?$", path)
        if m:
            path = f"{blog}/#{m.group(1)}"
    frag = f"#{u.fragment}" if u.fragment and "#" not in path else ""
    prefix = (cfg["site_url"] if absolute else "") + cfg["base_path"]
    return prefix + path + frag


class Cleaner(HTMLParser):
    def __init__(self, cfg: dict, absolute: bool):
        super().__init__(convert_charrefs=True)
        self.cfg, self.absolute = cfg, absolute
        self.out, self.stack, self.skip = [], [], 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "iframe":
            src = a.get("src") or ""
            if src.startswith("//"):
                src = "https:" + src
            if host_of(src) in VIDEO_HOSTS:
                src = src.replace("://youtube.com", "://www.youtube-nocookie.com").replace(
                    "://www.youtube.com", "://www.youtube-nocookie.com"
                )
                title = esc(a.get("title") or "Embedded video")
                self.out.append(
                    f'<div class="video"><iframe src="{esc(src)}" title="{title}" loading="lazy" '
                    f'allowfullscreen referrerpolicy="strict-origin-when-cross-origin"></iframe></div>'
                )
            self.skip += 1
            return
        if tag in DROP_WITH_CONTENT:
            self.skip += 1
            return
        if self.skip:
            return
        tag = RENAME.get(tag, tag)
        if tag not in ALLOWED:
            return
        keep = []
        if tag == "a":
            href = a.get("href") or ""
            if href and not href.lower().startswith("javascript:"):
                new = rewrite_url(href, self.cfg, self.absolute)
                keep.append(("href", new))
                if re.match(r"^https?://", new) and host_of(new) not in {host_of(self.cfg["site_url"])}:
                    keep.append(("rel", "noopener"))
            if a.get("title"):
                keep.append(("title", a["title"]))
        elif tag == "img":
            src = a.get("src") or ""
            if src.startswith("//"):
                src = "https:" + src
            if not src:
                return
            keep.append(("src", src))
            keep.append(("alt", a.get("alt") or ""))
            for dim in ("width", "height"):
                if (a.get(dim) or "").isdigit():
                    keep.append((dim, a[dim]))
            keep += [("loading", "lazy"), ("decoding", "async")]
        elif tag in ("td", "th"):
            for k in ("colspan", "rowspan"):
                if (a.get(k) or "").isdigit():
                    keep.append((k, a[k]))
        anchor_id = a.get("id") or (a.get("name") if tag == "a" else None)
        if anchor_id and tag != "img":
            keep.append(("id", anchor_id))
        attr_text = "".join(f' {k}="{esc(v)}"' for k, v in keep)
        if tag == "table":
            self.out.append(f'<div class="tw"><table{attr_text}>')
        else:
            self.out.append(f"<{tag}{attr_text}>")
        if tag not in VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in DROP_WITH_CONTENT:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        tag = RENAME.get(tag, tag)
        if tag not in ALLOWED or tag in VOID or tag not in self.stack:
            return
        while self.stack:
            top = self.stack.pop()
            self.out.append("</table></div>" if top == "table" else f"</{top}>")
            if top == tag:
                break

    def handle_data(self, data):
        if not self.skip:
            self.out.append(esc(data))

    def result(self) -> str:
        while self.stack:
            top = self.stack.pop()
            self.out.append("</table></div>" if top == "table" else f"</{top}>")
        return "".join(self.out)


def clean_html(markup: str, cfg: dict, absolute: bool = False) -> str:
    c = Cleaner(cfg, absolute)
    c.feed(markup)
    c.close()
    out = c.result()
    out = re.sub(r"<(p|div)>(\s|<br>)*</\1>", "", out)
    return out


# ---------------------------------------------------------------- templates

CSS = """
@font-face{font-family:Fraunces;src:url(%FONT_FRAUNCES%) format("woff2");font-weight:100 900;font-display:swap}
@font-face{font-family:Inter;src:url(%FONT_INTER%) format("woff2");font-weight:100 900;font-display:swap}
@font-face{font-family:"Inter Fallback";src:local("Arial");size-adjust:107.4%;ascent-override:90.2%;descent-override:22.5%;line-gap-override:0%}
:root{--bg:#F7E3CE;--card:#FCEFDF;--ink:#3B2A20;--muted:#6B5445;--brand:#A8452A;--link:#8E3A22;--line:#E2C9AE;--chip:#EBCFB0;--serif:Fraunces,Georgia,serif;--sans:Inter,"Inter Fallback",system-ui,sans-serif}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;min-height:100vh;display:flex;flex-direction:column;background:var(--bg);color:var(--ink);font:400 16px/1.65 var(--sans)}
main{flex:1 0 auto}
h1,h2,h3,h4{font-family:var(--serif);font-weight:500;line-height:1.15;letter-spacing:-.015em;margin:0}
a{color:var(--link)}
a:focus-visible,input:focus-visible,button:focus-visible{outline:3px solid var(--ink);outline-offset:2px}
.skip{position:absolute;left:-999px;top:0}
.skip:focus{left:16px;top:8px;background:#fff;color:var(--ink);padding:8px 14px;z-index:9;border-radius:8px}
.wrap{max-width:1120px;margin:0 auto;padding:0 24px}
.top{background:var(--brand);color:#fff}
.top .wrap{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:4px 24px;padding-top:10px;padding-bottom:10px}
.bw{display:flex;flex-direction:column;padding:6px 0}
.brand{font:500 22px/1.2 var(--serif);color:#fff;text-decoration:none}
.tag{font-size:13px;line-height:1.3;color:#F7E3CE}
nav ul{list-style:none;display:flex;flex-wrap:wrap;align-items:center;gap:0 22px;margin:0;padding:0}
nav a{color:#fff;text-decoration:none;font-weight:500;font-size:15px;display:inline-block;padding:12px 0}
nav a[aria-current]{text-decoration:underline;text-underline-offset:6px}
nav a.sub{background:var(--bg);color:var(--ink);padding:10px 18px;border-radius:999px}
.kicker{margin:0 0 14px;font-size:13px;font-weight:600;letter-spacing:.12em;text-transform:uppercase;color:var(--link)}
.hero{padding:64px 0 36px}
.hero h1{font-size:clamp(36px,6vw,60px);line-height:1.05;letter-spacing:-.025em;max-width:16em}
.hero .lead{margin:20px 0 0;max-width:36em;font-size:19px;color:var(--muted)}
.feature{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);background:var(--card);border:1px solid var(--line);border-radius:22px;overflow:hidden}
.feature .pic{min-height:300px;background:var(--brand);position:relative}
.feature .pic::before{content:"";position:absolute;right:14%;top:20%;width:38%;aspect-ratio:1;border-radius:50%;background:#E2A66F}
.feature .pic img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.feature .txt{padding:40px;display:flex;flex-direction:column;justify-content:center;gap:14px}
.feature h2{font-size:clamp(28px,4vw,38px)}
.feature h2 a,.row h3 a{color:var(--ink);text-decoration:none}
.feature h2 a:hover,.row h3 a:hover{text-decoration:underline}
.muted{color:var(--muted)}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin:0;padding:0;list-style:none}
.chips a,.chips span{display:inline-block;background:var(--chip);color:var(--ink);text-decoration:none;font-size:13px;font-weight:500;padding:5px 13px;border-radius:999px}
.chips a:hover{background:var(--brand);color:#fff}
.more{font-weight:600;text-decoration:none}
.more:hover{text-decoration:underline}
.layout{display:grid;grid-template-columns:minmax(0,1fr) 320px;gap:56px;padding:48px 0 72px}
.row{display:flex;gap:24px;padding:28px 0;border-top:1px solid var(--line)}
.row .ph,.row img{flex:0 0 140px;width:140px;height:140px;border-radius:14px;object-fit:cover;background:var(--brand)}
.row .ph{position:relative;overflow:hidden}
.row .ph::after{content:"";position:absolute;left:34px;top:34px;width:72px;height:72px;border-radius:50%;background:#E2A66F}
.row h3{font-size:26px;margin:4px 0 8px}
.row p{margin:0 0 12px}
.date{margin:0;font-size:13px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
aside section{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:24px;margin-bottom:20px}
aside h2{font-size:21px;margin-bottom:12px}
aside p{margin:0 0 10px;font-size:15px}
aside ul.plain{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:10px;font-size:15px}
aside ul.plain a{font-weight:500}
.years{display:flex;flex-wrap:wrap;gap:6px 16px;font-weight:500}
form.search label{display:block;font-size:13px;font-weight:600;color:var(--muted);margin-bottom:6px}
form.search input[type=search]{width:100%;font:inherit;padding:11px 14px;border:1px solid #CDAE8D;border-radius:12px;background:#FFF8EE;color:var(--ink)}
.tl h1{font-size:clamp(34px,5vw,48px)}
.tlist{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:4px}
.tlist a{display:flex;justify-content:space-between;gap:12px;padding:10px 14px;border-radius:12px;text-decoration:none;color:var(--ink);font-weight:500}
.tlist a:hover{background:var(--chip)}
.tlist a[aria-current]{background:var(--brand);color:#fff}
.tlist span{color:var(--muted);font-weight:400}
.tlist a[aria-current] span{color:#fff}
.page{padding:48px 0 72px}
.page h1{font-size:clamp(34px,5vw,52px);margin-bottom:16px}
.narrow{max-width:720px;margin:0 auto}
.crumb{display:inline-block;padding:8px 0;font-weight:500;text-decoration:none}
.post h1{font-size:clamp(34px,5.5vw,54px);line-height:1.07;letter-spacing:-.025em;margin:16px 0}
.body{font-size:18px;line-height:1.75;overflow-wrap:anywhere}
.body h2{font-size:30px;margin:2em 0 .5em}
.body h3{font-size:24px;margin:1.8em 0 .5em}
.body h4,.body h5,.body h6{font-size:20px;margin:1.6em 0 .4em}
.body p,.body ul,.body ol{margin:0 0 1.15em}
.body img{max-width:100%;height:auto;border-radius:10px}
.body blockquote{margin:1.6em 0;padding:2px 0 2px 24px;border-left:4px solid var(--brand);font-family:var(--serif);font-size:22px;line-height:1.45}
.body blockquote p:last-child{margin-bottom:0}
.body figure{margin:1.6em 0}
.body figcaption{font-size:14px;color:var(--muted)}
.body pre{overflow-x:auto;background:var(--card);padding:16px;border-radius:12px;font-size:15px}
.tw{overflow-x:auto;margin:1.4em 0}
.tw table{border-collapse:collapse;min-width:100%;font-size:16px}
.tw th,.tw td{border:1px solid var(--line);padding:8px 12px;text-align:left;vertical-align:top}
.tw th{background:var(--card)}
.video{aspect-ratio:16/9;margin:1.6em 0}
.video iframe{width:100%;height:100%;border:0;border-radius:12px}
.share{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin-top:40px;padding-top:24px;border-top:1px solid var(--line)}
.share b{font-size:14px;color:var(--muted);margin-right:6px}
.share a{border:1.5px solid #CDAE8D;color:var(--ink);text-decoration:none;font-size:14px;font-weight:500;padding:9px 16px;border-radius:999px}
.share a:hover{background:var(--ink);color:#fff;border-color:var(--ink)}
.pn{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;margin-top:40px}
.pn a{display:block;border:1px solid var(--line);background:var(--card);border-radius:14px;padding:16px 20px;text-decoration:none;color:var(--ink)}
.pn a:hover{border-color:var(--brand)}
.pn small{display:block;font-size:12px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
.pn span{font:500 19px/1.3 var(--serif)}
.pn .next{text-align:right}
.arch h2{font-size:30px;margin:40px 0 4px;padding-top:8px}
.arch h3{font-size:15px;font-family:var(--sans);letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin:20px 0 6px}
.arch ul{list-style:none;margin:0;padding:0}
.arch li{padding:6px 0}
.arch time{color:var(--muted);font-size:14px;margin-right:10px}
.tcloud{display:flex;flex-wrap:wrap;gap:10px}
.tcloud a{background:var(--card);border:1px solid var(--line);padding:10px 18px;border-radius:999px;text-decoration:none;font-weight:500;color:var(--ink)}
.tcloud a:hover{border-color:var(--brand)}
.prose p,.prose ul{margin:0 0 1.1em;max-width:42em}
.prose h2{font-size:26px;margin:1.8em 0 .5em}
.sub-band{background:var(--brand);color:#fff}
.foot{background:var(--brand);color:#fff;margin-top:0}
.foot .wrap{display:flex;flex-wrap:wrap;justify-content:space-between;gap:10px 24px;padding-top:26px;padding-bottom:26px;font-size:14px}
.foot a{color:#fff}
.foot ul{list-style:none;display:flex;flex-wrap:wrap;gap:8px 20px;margin:0;padding:0}
@media (max-width:880px){.tl aside{order:-1}.tlist{flex-direction:row;flex-wrap:wrap}.tlist a{border:1px solid var(--line);background:var(--card)}.tlist a[aria-current]{background:var(--brand);border-color:var(--brand)}.layout{grid-template-columns:minmax(0,1fr);gap:24px}.feature{grid-template-columns:minmax(0,1fr)}.feature .pic{min-height:200px}.feature .txt{padding:28px}}
@media (max-width:560px){.row{flex-direction:column;gap:14px}.row .ph,.row img{width:100%;flex-basis:auto;height:160px}.row .ph::after{left:24px;top:24px}.pn{grid-template-columns:minmax(0,1fr)}.hero{padding-top:40px}.wrap{padding:0 18px}}
@media (prefers-reduced-motion:reduce){*{scroll-behavior:auto!important}}
"""


def minify(css: str) -> str:
    css = re.sub(r"\s*([{};,>])\s*", r"\1", css)
    return re.sub(r"\n+", "", css).strip()


IMG_EXT = re.compile(r"\.(?:jpe?g|png|gif|webp|avif)(?:[?#].*)?$", re.I)
IMG_HOST = re.compile(r"(?:googleusercontent\.com|blogspot\.com|bp\.blogspot\.com|ggpht\.com)", re.I)


def post_image(rec: dict) -> str:
    """Best picture for a post card: the full-size image inside the post, else the feed thumbnail."""
    body = rec.get("html", "")
    for m in re.finditer(r"<img\b[^>]*>", body, flags=re.I):
        tag = m.group(0)
        src = re.search(r'\bsrc\s*=\s*["\']([^"\']+)["\']', tag, flags=re.I)
        if not src:
            continue
        url = html.unescape(src.group(1))
        if url.startswith("//"):
            url = "https:" + url
        if not url.startswith("http"):
            continue
        # a link around the picture usually points at the full-size original
        before = body[max(0, m.start() - 600) : m.start()]
        link = re.findall(r'<a\b[^>]*\bhref\s*=\s*["\']([^"\']+)["\']', before, flags=re.I)
        if link:
            href = html.unescape(link[-1])
            if href.startswith("//"):
                href = "https:" + href
            if href.startswith("http") and (IMG_EXT.search(href) or IMG_HOST.search(href)):
                return href
        return url
    return rec.get("thumb", "")


class Site:
    def __init__(self, cfg: dict, posts: list):
        self.cfg = cfg
        self.base = cfg["base_path"]
        self.blog = "/" + cfg.get("blog_dir", "blog").strip("/")
        self.posts = sorted(posts, key=lambda p: parse_dt(p["published"]), reverse=True)
        self.written = {}
        self.css = minify(
            CSS.replace("%FONT_FRAUNCES%", self.u("/assets/fonts/fraunces.woff2")).replace(
                "%FONT_INTER%", self.u("/assets/fonts/inter.woff2")
            )
        )
        self._paths, seen = {}, set()
        for p in self.posts:
            path = post_site_path(cfg, p["url_path"])
            if path in seen:
                m = re.match(r"^/(\d{4})/(\d{2})/(.+)$", p["url_path"])
                path = f"{self.blog}/{m.group(1)}/{m.group(2)}/{m.group(3)}" if m else path
            seen.add(path)
            self._paths[p["id"]] = path
        self.topics = {}
        for p in self.posts:
            for label in p["labels"]:
                self.topics.setdefault(slugify(label), {"name": label, "posts": []})["posts"].append(p)

    # -- url helpers
    def u(self, path: str) -> str:
        return self.base + path

    def abs(self, path: str) -> str:
        return self.cfg["site_url"] + self.base + path

    def path_of(self, p) -> str:
        """Where a post lives on this site: /blog/<year>/<slug>.html"""
        return self._paths[p["id"]]

    def post_url(self, p) -> str:
        return self.u(self.path_of(p))

    def write(self, path: str, text: str) -> None:
        rel = path + "index.html" if path.endswith("/") else path
        self.written[rel.lstrip("/")] = text

    # -- page shell
    def shell(self, *, title, desc, path, body, current="", og="website", head_extra="", noindex=False):
        cfg = self.cfg
        if title == cfg["site_name"]:
            full_title = cfg.get("full_title") or title
        else:
            full_title = f"{title} | {cfg['site_name']}"
        icon = (
            "data:image/svg+xml,"
            + quote(
                "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><circle cx='16' cy='16' r='16' "
                "fill='#A8452A'/><rect x='14.5' y='7' width='3' height='18' fill='#F7E3CE'/>"
                "<rect x='9' y='12' width='14' height='3' fill='#F7E3CE'/></svg>"
            )
        )

        def nav(label, href, key):
            cur = ' aria-current="page"' if current == key else ""
            cls = ' class="sub"' if key == "sub" else ""
            return f'<li><a{cls} href="{href}"{cur}>{label}</a></li>'

        items = "".join(
            [
                nav("Home", self.u("/"), "home"),
                nav("Topics", self.u("/topics/"), "topics"),
                nav("Blog", self.u(self.blog + "/"), "blog"),
                nav("About", self.u("/about/"), "about"),
                nav("Subscribe", self.u("/feed.xml"), "sub"),
            ]
        )
        robots = '<meta name="robots" content="noindex">' if noindex else ""
        tagline_html = f'<span class="tag">{esc(cfg["header_tagline"])}</span>' if cfg.get("header_tagline") else ""
        return (
            '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{esc(full_title)}</title>"
            f'<meta name="description" content="{esc(desc)}">{robots}'
            f'<link rel="canonical" href="{esc(self.abs(path))}">'
            '<meta name="theme-color" content="#A8452A">'
            f'<link rel="icon" href="{icon}">'
            f'<link rel="alternate" type="application/atom+xml" title="{esc(cfg["site_name"])}" href="{self.u("/feed.xml")}">'
            f'<link rel="preload" href="{self.u("/assets/fonts/inter.woff2")}" as="font" type="font/woff2" crossorigin>'
            f'<link rel="preload" href="{self.u("/assets/fonts/fraunces.woff2")}" as="font" type="font/woff2" crossorigin>'
            f'<meta property="og:type" content="{og}"><meta property="og:site_name" content="{esc(cfg["site_name"])}">'
            f'<meta property="og:title" content="{esc(full_title)}"><meta property="og:description" content="{esc(desc)}">'
            f'<meta property="og:url" content="{esc(self.abs(path))}"><meta name="twitter:card" content="summary">'
            f"{head_extra}<style>{self.css}</style></head><body>"
            '<a class="skip" href="#main">Skip to content</a>'
            f'<header class="top"><div class="wrap"><div class="bw"><a class="brand" href="{self.u("/")}">{esc(cfg["site_name"])}</a>{tagline_html}</div>'
            f'<nav aria-label="Main"><ul>{items}</ul></nav></div></header>'
            f'<main id="main">{body}</main>'
            f'<footer class="foot"><div class="wrap"><span>{esc(cfg["site_name"])}. Views are the author\'s own.</span>'
            f'<ul><li><a href="{self.u("/")}">Home</a></li><li><a href="{self.u(self.blog + "/")}">Blog</a></li>'
            f'<li><a href="{self.u("/privacy/")}">Privacy</a></li><li><a href="{self.u("/feed.xml")}">Feed</a></li></ul>'
            "</div></footer></body></html>"
        )

    # -- small components
    def chips(self, post) -> str:
        if not post["labels"]:
            return ""
        li = "".join(
            f'<li><a href="{self.u("/topics/" + slugify(l) + "/")}">{esc(l)}</a></li>' for l in post["labels"]
        )
        return f'<ul class="chips">{li}</ul>'

    @staticmethod
    def thumb(raw: str, w: int, h: int) -> str:
        """Ask Blogger's image server for a bigger crop than the tiny 72px feed thumbnail."""
        if not raw:
            return ""
        size = f"w{w}-h{h}-c"
        patterns = [
            (r"=s\d+(-[a-z]+)*$", f"={size}"),
            (r"=w\d+-h\d+[^/?]*$", f"={size}"),
            (r"/s\d+(-[a-z]+)*/", f"/{size}/"),
            (r"/w\d+-h\d+[^/]*/", f"/{size}/"),
        ]
        for pattern, repl in patterns:
            if re.search(pattern, raw):
                return re.sub(pattern, repl, raw, count=1)
        return raw

    def srcset(self, raw: str, sizes) -> str:
        """Build a srcset from (width, height) pairs, or nothing if the image can't be resized."""
        if not raw or self.thumb(raw, 1, 1) == raw:
            return ""
        return ", ".join(f"{esc(self.thumb(raw, w, h))} {w}w" for w, h in sizes)

    def row(self, p) -> str:
        raw = post_image(p)
        t = self.thumb(raw, 320, 320)
        ss = self.srcset(raw, [(160, 160), (320, 320), (480, 480), (800, 800)])
        ss_attr = f' srcset="{ss}" sizes="(max-width:560px) 100vw, 140px"' if ss else ""
        pic = (
            f'<img src="{esc(t)}"{ss_attr} alt="" width="140" height="140" loading="lazy" decoding="async">'
            if t
            else '<div class="ph" aria-hidden="true"></div>'
        )
        return (
            f'<article class="row">{pic}<div><p class="date"><time datetime="{esc(p["published"][:10])}">'
            f'{fmt_date(p["published"])}</time></p><h3><a href="{self.post_url(p)}">{esc(p["title"])}</a></h3>'
            f'<p class="muted">{esc(excerpt(p["html"]))}</p>{self.chips(p)}</div></article>'
        )

    def sidebar(self) -> str:
        cfg = self.cfg
        popular = []
        by_title = {p["title"].strip().lower(): p for p in self.posts}
        for title in cfg.get("popular_titles", []):
            p = by_title.get(title.strip().lower())
            if p:
                popular.append(f'<li><a href="{self.post_url(p)}">{esc(p["title"])}</a></li>')
        pop = (
            f'<section><h2>Popular posts</h2><ul class="plain">{"".join(popular)}</ul></section>' if popular else ""
        )
        topics = "".join(
            f'<li><a href="{self.u("/topics/" + s + "/")}">{esc(t["name"])}</a></li>'
            for s, t in sorted(self.topics.items(), key=lambda kv: kv[1]["name"].lower())
        )
        years = sorted({parse_dt(p["published"]).year for p in self.posts}, reverse=True)
        year_links = "".join(f'<a href="{self.u(self.blog + "/")}#{y}">{y}</a>' for y in years)
        return (
            f'<aside aria-label="Sidebar"><section><h2>About the author</h2><p>{esc(cfg["author_bio"])}</p>'
            f'<p class="muted">Thesis: {esc(cfg["thesis"])}</p><a class="more" href="{self.u("/about/")}">Read more</a></section>'
            '<section><form class="search" action="https://duckduckgo.com/" method="get" role="search">'
            '<h2>Search</h2><label for="q">Search this blog</label>'
            '<input id="q" type="search" name="q" placeholder="Search posts" autocomplete="off">'
            f'<input type="hidden" name="sites" value="{esc(host_of(cfg["site_url"]))}"></form></section>'
            f'<section><h2>Topics</h2><ul class="chips">{topics}</ul></section>{pop}'
            f'<section><h2>By year</h2><div class="years">{year_links}</div></section></aside>'
        )

    # -- pages
    def build_home(self):
        cfg = self.cfg
        feat, rest = (self.posts[0] if self.posts else None), self.posts[1 : 1 + cfg.get("home_recent_count", 6)]
        feature = ""
        if feat:
            raw = post_image(feat)
            t = self.thumb(raw, 1200, 900)
            ss = self.srcset(raw, [(640, 480), (1200, 900)])
            ss_attr = f' srcset="{ss}" sizes="(max-width:880px) 100vw, 560px"' if ss else ""
            pic = (
                f'<img src="{esc(t)}"{ss_attr} alt="" width="800" height="600" fetchpriority="high" decoding="async">'
                if t
                else ""
            )
            feature = (
                f'<article class="feature"><div class="pic" aria-hidden="true">{pic}</div><div class="txt">'
                f'<p class="kicker" style="margin:0">Latest, {fmt_date(feat["published"])}</p>'
                f'<h2><a href="{self.post_url(feat)}">{esc(feat["title"])}</a></h2>'
                f'<p class="muted" style="margin:0">{esc(excerpt(feat["html"], 260))}</p>{self.chips(feat)}'
                f'<a class="more" href="{self.post_url(feat)}">Read the post &rarr;</a></div></article>'
            )
        rows = "".join(self.row(p) for p in rest)
        older = f'<p><a class="more" href="{self.u(self.blog + "/")}">Browse all posts &rarr;</a></p>' if self.posts else ""
        empty = '<p class="muted">No posts yet. Run tools/update_site.py to import them.</p>' if not self.posts else ""
        body = (
            f'<div class="wrap"><section class="hero"><p class="kicker">A blog by {esc(cfg["author"])}</p>'
            f'<h1>{esc(cfg["hero_title"])}</h1><p class="lead">{esc(cfg["hero_text"])}</p></section>{feature}'
            f'<div class="layout"><section aria-labelledby="recent"><h2 id="recent" style="font-size:30px;margin-bottom:8px">Recent writing</h2>'
            f"{rows}{empty}{older}</section>{self.sidebar()}</div></div>"
        )
        self.write("/", self.shell(title=cfg["site_name"], desc=cfg["tagline"], path="/", body=body, current="home"))

    def build_post(self, i: int, p):
        cfg = self.cfg
        newer = self.posts[i - 1] if i > 0 else None
        older = self.posts[i + 1] if i + 1 < len(self.posts) else None
        pn = ""
        if newer or older:
            o = (
                f'<a class="prev" href="{self.post_url(older)}"><small>Earlier, {fmt_date(older["published"])}</small><span>{esc(older["title"])}</span></a>'
                if older
                else "<span></span>"
            )
            n = (
                f'<a class="next" href="{self.post_url(newer)}"><small>Next, {fmt_date(newer["published"])}</small><span>{esc(newer["title"])}</span></a>'
                if newer
                else "<span></span>"
            )
            pn = f'<nav class="pn" aria-label="More posts">{o}{n}</nav>'
        path = self.path_of(p)
        url = self.abs(path)
        enc_url, enc_title = quote(url, safe=""), quote(p["title"], safe="")
        share = (
            '<div class="share"><b>Share</b>'
            f'<a href="mailto:?subject={enc_title}&amp;body={enc_url}">Email</a>'
            f'<a rel="noopener" href="https://x.com/intent/post?url={enc_url}&amp;text={enc_title}">X</a>'
            f'<a rel="noopener" href="https://www.facebook.com/sharer/sharer.php?u={enc_url}">Facebook</a></div>'
        )
        contact = ""
        if cfg.get("contact_email"):
            contact = (
                f'<p class="muted" style="margin-top:28px">Thoughts on this post? '
                f'<a href="mailto:{esc(cfg["contact_email"])}?subject={enc_title}">Send an email</a>.</p>'
            )
        ld = {
            "@context": "https://schema.org",
            "@type": "BlogPosting",
            "headline": p["title"],
            "datePublished": p["published"],
            "dateModified": p["updated"],
            "author": {"@type": "Person", "name": cfg["author"]},
            "mainEntityOfPage": url,
        }
        ld_tag = '<script type="application/ld+json">' + json.dumps(ld).replace("</", "<\\/") + "</script>"
        body = (
            f'<div class="wrap"><article class="post narrow" style="padding:32px 0 72px">'
            f'<a class="crumb" href="{self.u(self.blog + "/")}">&larr; All posts</a>{self.chips(p)}'
            f'<h1>{esc(p["title"])}</h1><p class="muted" style="margin:0 0 32px">By {esc(cfg["author"])} &middot; '
            f'<time datetime="{esc(p["published"][:10])}">{fmt_date(p["published"])}</time></p>'
            f'<div class="body">{clean_html(p["html"], cfg)}</div>{share}{contact}{pn}</article></div>'
        )
        self.write(
            path,
            self.shell(
                title=p["title"],
                desc=excerpt(p["html"], 155) or cfg["tagline"],
                path=path,
                body=body,
                og="article",
                head_extra=ld_tag,
            ),
        )

    def build_blog_index(self):
        by_year = {}
        for p in self.posts:
            d = parse_dt(p["published"])
            by_year.setdefault(d.year, {}).setdefault(d.month, []).append(p)
        parts = []
        for y in sorted(by_year, reverse=True):
            parts.append(f'<h2 id="{y}">{y}</h2>')
            for m in sorted(by_year[y], reverse=True):
                lis = "".join(
                    f'<li><time datetime="{esc(p["published"][:10])}">{parse_dt(p["published"]).day}</time>'
                    f'<a href="{self.post_url(p)}">{esc(p["title"])}</a></li>'
                    for p in by_year[y][m]
                )
                parts.append(f"<h3>{calendar.month_name[m]}</h3><ul>{lis}</ul>")
        body = (
            f'<div class="wrap"><div class="page narrow arch"><h1>Blog</h1>'
            f'<p class="muted">Every post, newest first.</p>{"".join(parts)}</div></div>'
        )
        self.write(self.blog + "/", self.shell(title="Blog", desc="All posts on " + self.cfg["site_name"], path=self.blog + "/", body=body, current="blog"))

    def topics_sidebar(self, current=None) -> str:
        items = sorted(self.topics.items(), key=lambda kv: kv[1]["name"].lower())
        cur = ' aria-current="page"' if current is None else ""
        lis = [f'<li><a href="{self.u("/topics/")}"{cur}>All posts <span>{len(self.posts)}</span></a></li>']
        for slug, t in items:
            cur = ' aria-current="page"' if slug == current else ""
            lis.append(
                f'<li><a href="{self.u("/topics/" + slug + "/")}"{cur}>{esc(t["name"])} <span>{len(t["posts"])}</span></a></li>'
            )
        return f'<aside aria-label="Topics"><section><h2>Topics</h2><ul class="tlist">{"".join(lis)}</ul></section></aside>'

    def topics_page(self, slug, title, count, posts, path, desc):
        rows = "".join(self.row(p) for p in posts)
        body = (
            f'<div class="wrap"><div class="layout tl"><section aria-labelledby="t"><h1 id="t">{esc(title)}</h1>'
            f'<p class="muted" style="margin:6px 0 8px">{count} post{"s" if count != 1 else ""}</p>{rows}</section>'
            f"{self.topics_sidebar(slug)}</div></div>"
        )
        self.write(path, self.shell(title="Topics" if slug is None else title, desc=desc, path=path, body=body, current="topics"))

    def build_topics(self):
        self.topics_page(None, "All topics", len(self.posts), self.posts, "/topics/", "Browse posts by topic")
        for slug, t in self.topics.items():
            self.topics_page(slug, t["name"], len(t["posts"]), t["posts"], f"/topics/{slug}/", f"Posts about {t['name']}")

    def build_about(self):
        cfg = self.cfg
        contact = (
            f'<p>Email: <a href="mailto:{esc(cfg["contact_email"])}">{esc(cfg["contact_email"])}</a></p>'
            if cfg.get("contact_email")
            else ""
        )
        body = (
            f'<div class="wrap"><div class="page narrow prose"><h1>About</h1><h2>{esc(cfg["author"])}</h2>'
            f'<p class="muted">{esc(cfg["author_title"])}</p><p>{esc(cfg["author_bio"])}</p>'
            f'<h2>Thesis</h2><p>{esc(cfg["thesis"])}</p>{contact}</div></div>'
        )
        self.write("/about/", self.shell(title="About", desc=cfg["author_bio"], path="/about/", body=body, current="about"))

    def build_privacy(self):
        cfg = self.cfg
        d = parse_dt(cfg["privacy_updated"] + "T00:00:00")
        contact = (
            f'<p>Questions? Email <a href="mailto:{esc(cfg["contact_email"])}">{esc(cfg["contact_email"])}</a>.</p>'
            if cfg.get("contact_email")
            else ""
        )
        body = (
            '<div class="wrap"><div class="page narrow prose"><h1>Privacy policy</h1>'
            f'<p class="muted">Last updated {calendar.month_name[d.month]} {d.day}, {d.year}</p>'
            f"<p>{esc(cfg['site_name'])} is a reading site. It does not ask you to create an account, "
            "and it does not collect personal information.</p>"
            "<h2>What this site does not do</h2><ul><li>No cookies.</li><li>No analytics or tracking scripts.</li>"
            "<li>No advertising.</li><li>No JavaScript.</li><li>No forms that store what you type.</li></ul>"
            "<h2>Hosting</h2><p>The site is hosted on GitHub Pages. Like any web host, GitHub receives your IP address "
            'and browser details when your browser requests a page, and may keep server logs. See '
            '<a rel="noopener" href="https://docs.github.com/en/site-policy/privacy-policies/github-general-privacy-statement">GitHub\'s privacy statement</a>.</p>'
            "<h2>Images and videos</h2><p>Some older posts show images that are still hosted by Google (Blogger). "
            "Some posts embed YouTube videos using YouTube's privacy-enhanced mode, which loads only when you scroll to them. "
            "Your browser contacts those services directly when it loads that content.</p>"
            "<h2>Search and sharing</h2><p>The search box sends your query to DuckDuckGo, limited to this site. "
            "The Email, X and Facebook buttons are plain links. Nothing is sent to those services unless you click one.</p>"
            f"<h2>Changes</h2><p>If this policy changes, the date above will change.</p>{contact}</div></div>"
        )
        self.write("/privacy/", self.shell(title="Privacy policy", desc="How this site handles your data", path="/privacy/", body=body))

    def build_404(self):
        body = (
            f'<div class="wrap"><div class="page narrow prose"><h1>Page not found</h1>'
            f'<p>That page does not exist or has moved. Try the <a href="{self.u("/")}">home page</a>, '
            f'the <a href="{self.u(self.blog + "/")}">blog</a> or <a href="{self.u("/topics/")}">topics</a>.</p></div></div>'
        )
        self.write("/404.html", self.shell(title="Page not found", desc="Page not found", path="/404.html", body=body, noindex=True))

    def build_legacy_redirects(self):
        """Optional: leave a small redirect page at each old Blogger address."""
        if not self.cfg.get("legacy_redirects"):
            return
        for p in self.posts:
            new = self.abs(self.path_of(p))
            self.write(
                p["url_path"],
                '<!doctype html><html lang="en"><head><meta charset="utf-8">'
                f'<title>Redirecting</title><meta http-equiv="refresh" content="0; url={esc(new)}">'
                f'<link rel="canonical" href="{esc(new)}"></head>'
                f'<body><p>This post moved to <a href="{esc(new)}">{esc(new)}</a>.</p></body></html>',
            )

    def build_feed(self):
        cfg = self.cfg
        latest = self.posts[:20]
        updated = latest[0]["updated"] if latest else "2026-01-01T00:00:00Z"
        entries = []
        for p in latest:
            cats = "".join(f'<category term="{esc(l)}"/>' for l in p["labels"])
            entries.append(
                f"<entry><id>{esc(self.abs(self.path_of(p)))}</id><title>{esc(p['title'])}</title>"
                f'<link rel="alternate" href="{esc(self.abs(self.path_of(p)))}"/>'
                f"<published>{esc(p['published'])}</published><updated>{esc(p['updated'])}</updated>"
                f"<author><name>{esc(cfg['author'])}</name></author>{cats}"
                f'<content type="html">{esc(clean_html(p["html"], cfg, absolute=True))}</content></entry>'
            )
        xml = (
            '<?xml version="1.0" encoding="utf-8"?><feed xmlns="http://www.w3.org/2005/Atom">'
            f"<id>{esc(self.abs('/'))}</id><title>{esc(cfg['site_name'])}</title><subtitle>{esc(cfg['tagline'])}</subtitle>"
            f'<link rel="self" href="{esc(self.abs("/feed.xml"))}"/><link rel="alternate" href="{esc(self.abs("/"))}"/>'
            f"<updated>{esc(updated)}</updated>{''.join(entries)}</feed>\n"
        )
        self.write("/feed.xml", xml)

    def build_sitemap(self):
        newest = self.posts[0]["updated"][:10] if self.posts else "2026-01-01"
        urls = [("/", newest), (self.blog + "/", newest), ("/topics/", newest), ("/about/", newest), ("/privacy/", self.cfg["privacy_updated"])]
        urls += [(f"/topics/{s}/", t["posts"][0]["updated"][:10]) for s, t in sorted(self.topics.items())]
        urls += [(self.path_of(p), p["updated"][:10]) for p in self.posts]
        body = "".join(f"<url><loc>{esc(self.abs(path))}</loc><lastmod>{lm}</lastmod></url>" for path, lm in urls)
        self.write(
            "/sitemap.xml",
            '<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + body
            + "</urlset>\n",
        )
        self.write(
            "/robots.txt",
            f"User-agent: *\nAllow: /\n\nSitemap: {self.cfg['site_url']}{self.base}/sitemap.xml\n",
        )

    def build_all(self):
        self.build_home()
        for i, p in enumerate(self.posts):
            self.build_post(i, p)
        self.build_blog_index()
        self.build_legacy_redirects()
        self.build_topics()
        self.build_about()
        self.build_privacy()
        self.build_404()
        self.build_feed()
        self.build_sitemap()
        self.write("/.nojekyll", "")
        if self.cfg.get("cname"):
            self.write("/CNAME", self.cfg["cname"] + "\n")


def rebuild(cfg: dict) -> dict:
    posts = list(load_posts().values())
    site = Site(cfg, posts)
    site.build_all()
    previous = set(json.loads(MANIFEST.read_text())) if MANIFEST.exists() else set()
    changed = 0
    for rel, text in site.written.items():
        f = OUT / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        if not f.exists() or f.read_text(encoding="utf-8") != text:
            f.write_text(text, encoding="utf-8")
            changed += 1
    removed = 0
    for rel in previous - set(site.written):
        f = OUT / rel
        if f.exists():
            f.unlink()
            removed += 1
            parent = f.parent
            while parent != OUT and parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent
    MANIFEST.write_text(json.dumps(sorted(site.written), indent=1) + "\n")
    return {"pages": len(site.written), "changed": changed, "removed": removed, "posts": len(posts)}


# ---------------------------------------------------------------- link checker

class RefParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.refs, self.ids = [], set()

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("id"):
            self.ids.add(a["id"])
        for key, kinds in (("href", {"a", "link"}), ("src", {"img", "script", "source", "iframe"})):
            if tag in kinds and a.get(key):
                self.refs.append((tag, a[key]))


def verify(cfg: dict, external: bool = False):
    """Return (errors, warnings). Errors fail the run; warnings are shown only."""
    errors, warnings, cache = [], [], {}
    base, site_host = cfg["base_path"], host_of(cfg["site_url"])

    def parse(f: Path):
        if f not in cache:
            p = RefParser()
            p.feed(f.read_text(encoding="utf-8"))
            cache[f] = p
        return cache[f]

    def resolve(page_rel: str, ref: str):
        """Return (file, fragment) for an internal ref, or None if external."""
        u = urlparse(ref)
        if u.scheme in ("mailto", "tel", "data", "javascript"):
            return None
        if u.scheme in ("http", "https"):
            if host_of(ref) != site_host:
                return None
            path = u.path or "/"
        elif u.netloc:
            return None
        else:
            path = u.path
            if path and not path.startswith("/"):
                path = urljoin("/" + page_rel, path)
            elif not path:
                path = "/" + page_rel
        if base and path.startswith(base + "/"):
            path = path[len(base):]
        elif base and path == base:
            path = "/"
        target = OUT / unquote(path.lstrip("/"))
        if path.endswith("/") or target.is_dir():
            target = target / "index.html"
        return target, u.fragment

    generated = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else []
    pages = sorted(OUT / r for r in generated if r.endswith(".html"))
    if not pages:
        return ["No generated pages found. Run the build first."], []
    externals = {}
    for f in pages:
        rel = f.relative_to(OUT).as_posix()
        parser = parse(f)
        for tag, ref in parser.refs:
            if ref.startswith("data:"):
                continue
            res = resolve(rel, ref)
            if res is None:
                if ref.startswith("http"):
                    externals.setdefault(ref, rel)
                continue
            target, frag = res
            if not target.exists():
                errors.append(f"{rel}: broken {tag} -> {ref}")
            elif frag and target.suffix == ".html" and frag not in parse(target).ids:
                warnings.append(f"{rel}: missing anchor #{frag} in {target.relative_to(OUT).as_posix()}")
    # sitemap and robots
    sm = OUT / "sitemap.xml"
    if sm.exists():
        for loc in re.findall(r"<loc>([^<]+)</loc>", sm.read_text(encoding="utf-8")):
            res = resolve("sitemap.xml", html.unescape(loc))
            if res is None or not res[0].exists():
                errors.append(f"sitemap.xml: {loc} has no page")
    else:
        errors.append("sitemap.xml is missing")
    rb = OUT / "robots.txt"
    if not rb.exists() or "Sitemap:" not in rb.read_text(encoding="utf-8"):
        errors.append("robots.txt is missing or has no Sitemap line")
    # canonical must match the file location
    for f in pages:
        text = f.read_text(encoding="utf-8")
        if 'http-equiv="refresh"' in text:
            continue
        m = re.search(r'<link rel="canonical" href="([^"]+)"', text)
        rel = f.relative_to(OUT).as_posix()
        if not m:
            errors.append(f"{rel}: missing canonical link")
            continue
        res = resolve(rel, html.unescape(m.group(1)))
        if res is None or res[0] != f:
            errors.append(f"{rel}: canonical points to {m.group(1)}")
    if external:
        for url, where in sorted(externals.items()):
            try:
                req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
                urllib.request.urlopen(req, timeout=10)
            except urllib.error.HTTPError as e:
                if e.code not in (403, 405, 429):
                    errors.append(f"{where}: external link {url} returned {e.code}")
            except Exception as e:  # noqa: BLE001
                errors.append(f"{where}: external link {url} failed ({e.__class__.__name__})")
    return errors, warnings


# ---------------------------------------------------------------- main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Sync the static site with the Blogger blog.")
    ap.add_argument("--check", action="store_true", help="report new posts without changing anything")
    ap.add_argument("--rebuild", action="store_true", help="rebuild pages from content/ (no network)")
    ap.add_argument("--verify", action="store_true", help="only run the link checker")
    ap.add_argument("--external", action="store_true", help="also check external links")
    ap.add_argument("--feed-file", help="use a saved Atom feed file instead of the live blog")
    args = ap.parse_args(argv)
    cfg = load_config()

    if not args.verify and not args.rebuild:
        source = args.feed_file or cfg["blog_feed"]
        try:
            report = sync(source, dry_run=args.check, fallbacks=cfg.get("blog_feed_fallbacks", []))
        except (FeedError, urllib.error.URLError, OSError, ET.ParseError) as e:
            print(f"Could not read the blog feed:\n  {e}")
            print("Nothing was changed. Check your connection or the blog_feed value in config.json.")
            return 2
        print(f"Feed: {report['fetched']} posts found")
        for rec in report["new"]:
            print(f"  new      {post_site_path(cfg, rec['url_path'])}  ({rec['title']})")
        for _, rec in report["changed"]:
            print(f"  updated  {post_site_path(cfg, rec['url_path'])}  ({rec['title']})")
        if not report["new"] and not report["changed"]:
            print("  no new or edited posts")
        if args.check:
            return 0
        if report["samples_removed"]:
            print(f"  removed {report['samples_removed']} sample placeholder posts")

    if not args.verify:
        r = rebuild(cfg)
        print(f"Built {r['pages']} files from {r['posts']} posts ({r['changed']} written, {r['removed']} removed)")

    errors, warnings = verify(cfg, external=args.external)
    if warnings:
        print(f"Link check: {len(warnings)} warning(s), these do not stop the build")
        for w in warnings:
            print("  " + w)
    if errors:
        print(f"Link check: {len(errors)} problem(s)")
        for e in errors:
            print("  " + e)
        return 1
    if warnings:
        print("Link check: no broken links, images, sitemap entries or canonicals (anchor warnings above)")
    else:
        print("Link check: all internal links, images, anchors, sitemap entries and canonicals are valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
