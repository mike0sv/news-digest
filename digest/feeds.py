"""RSS/Atom parsing and URL normalization, without external packages."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
from html.parser import HTMLParser
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
import xml.etree.ElementTree as ET

from .net import http


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "svg"}:
            self.hidden += 1
        if tag in {"p", "br", "div", "li", "h1", "h2", "h3"} and not self.hidden:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg"} and self.hidden:
            self.hidden -= 1
        if tag in {"p", "div", "li"}:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain_text(value):
    parser = TextExtractor()
    parser.feed(value or "")
    return re.sub(r"\s+", " ", "".join(parser.parts)).strip()


def canonical_url(url):
    p = urlsplit(url.strip())
    if p.scheme not in {"http", "https"} or not p.hostname or p.username:
        raise ValueError("Invalid article URL")
    ignored = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref_src"}
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in ignored]
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path or "/",
                       urlencode(sorted(query)), ""))


def article_id(url, revision=""):
    identity = canonical_url(url) + ("\n" + revision if revision else "")
    return sha256(identity.encode()).hexdigest()[:20]


def parse_date(raw):
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        try:
            value = parsedate_to_datetime(raw.strip())
        except (ValueError, TypeError, IndexError):
            return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def local_name(tag):
    return tag.rsplit("}", 1)[-1]


def child_text(node, names):
    for name in names:
        for child in node:
            if local_name(child.tag) == name:
                value = "".join(child.itertext()).strip()
                if value:
                    return value
    return ""


def parse_feed(xml, source, now):
    # ElementTree does not fetch external entities. Reject declarations as well.
    if "<!ENTITY" in xml.upper():
        raise ValueError("XML declarations are not allowed")
    root = ET.fromstring(xml)
    result = []
    for item in root.iter():
        if local_name(item.tag) not in {"item", "entry"}:
            continue
        title = plain_text(child_text(item, ["title"]))
        url = ""
        for link in item:
            if local_name(link.tag) != "link":
                continue
            if link.attrib.get("rel", "alternate") == "alternate":
                url = link.attrib.get("href") or (link.text or "").strip()
                if url:
                    break
        if not title or not url:
            continue
        try:
            url = canonical_url(urljoin(source["url"], url))
        except ValueError:
            continue
        date_fields = (["updated", "published", "pubDate", "date"] if source.get("track_updates")
                       else ["published", "pubDate", "date", "updated"])
        published = parse_date(child_text(item, date_fields))
        revision = published.isoformat() if source.get("track_updates") and published else ""
        text = plain_text(child_text(item, ["encoded", "content", "description", "summary"]))
        result.append({
            "id": article_id(url, revision), "url": url, "title": title[:400],
            "source": source["name"], "source_id": source["id"], "group": source["group"],
            "published_at": published.isoformat() if published else None,
            "discovered_at": now.isoformat(), "text": text[:6000], "text_kind": "feed",
        })
    return result


def collect(sources, now, *, historical=False):
    def fetch(source):
        try:
            if source.get("type") in {"hn", "github-projects"}:
                from .signals import fetch_signal
                articles = fetch_signal(source, now)
            elif (historical or source.get("release_api")) and source["url"].startswith("https://github.com/") and source["url"].endswith("releases.atom"):
                from .signals import github_releases
                articles = github_releases(source, now)
            else:
                payload = http(source["url"])
                articles = (parse_html_source(payload, source, now) if source.get("type") == "html"
                            else parse_feed(payload, source, now))
            if historical:
                for article in articles:
                    article["discovered_at"] = datetime.now(timezone.utc).isoformat()
            if not articles and source.get("type") not in {"hn", "github-projects"}:
                raise ValueError("Source returned no articles")
            return articles, {"id": source["id"], "name": source["name"], "ok": True,
                              "count": len(articles), "error": None}
        except (RuntimeError, ValueError, ET.ParseError, KeyError, TypeError) as error:
            # Feed contents and URLs are deliberately not logged.
            return [], {"id": source["id"], "name": source["name"], "ok": False,
                        "count": 0, "error": type(error).__name__}
    articles, health = [], []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for batch, status in pool.map(fetch, sources):
            articles.extend(batch)
            health.append(status)
    return articles, health


class PageParser(TextExtractor):
    def __init__(self):
        super().__init__()
        self.links = []
        self.current_link = None
        self.link_parts = []
        self.title_parts = []
        self.in_h1 = False
        self.published = None
        self.main_depth = 0
        self.main_parts = []

    def handle_starttag(self, tag, attrs):
        super().handle_starttag(tag, attrs)
        attrs = dict(attrs)
        if tag == "a":
            self.current_link = attrs.get("href")
            self.link_parts = []
        if tag == "h1":
            self.in_h1 = True
        if tag == "main" or tag == "article":
            self.main_depth += 1
        if tag == "meta" and attrs.get("property") in {"article:published_time", "datePublished"}:
            self.published = parse_date(attrs.get("content")) or self.published
        if tag == "time" and not self.published:
            self.published = parse_date(attrs.get("datetime"))

    def handle_endtag(self, tag):
        super().handle_endtag(tag)
        if tag == "a" and self.current_link:
            self.links.append((self.current_link, " ".join(self.link_parts).strip()))
            self.current_link = None
        if tag == "h1":
            self.in_h1 = False
        if tag in {"main", "article"} and self.main_depth:
            self.main_depth -= 1

    def handle_data(self, data):
        super().handle_data(data)
        if not self.hidden:
            if self.current_link:
                self.link_parts.append(data)
            if self.in_h1:
                self.title_parts.append(data)
            if self.main_depth:
                self.main_parts.append(data)


def parse_html_source(payload, source, now):
    listing = PageParser()
    listing.feed(payload)
    result, seen = [], set()
    pattern = re.compile(source["link_pattern"])
    for href, label in listing.links:
        try:
            url = canonical_url(urljoin(source["url"], href))
        except ValueError:
            continue
        if urlsplit(url).hostname != urlsplit(source["url"]).hostname:
            continue
        if (not pattern.search(urlsplit(url).path) or url in seen
                or urlsplit(url).path in source.get("exclude_paths", [])):
            continue
        seen.add(url)
        # A listing has no standardized feed dates: inspect its article metadata.
        try:
            page = http(url, timeout=15, retries=0)
            parser = PageParser()
            parser.feed(page)
            published = parser.published
            match = re.search(r'"datePublished"\s*:\s*"([^"\n]+)"', page)
            if match and not published:
                published = parse_date(match.group(1))
            title = " ".join(parser.title_parts).strip() or label
            text = re.sub(r"\s+", " ", " ".join(parser.main_parts or parser.parts)).strip()
        except (RuntimeError, ValueError):
            # Without article evidence we do not invent a news item from a navigation link.
            continue
        if not title:
            continue
        result.append({"id": article_id(url), "url": url, "title": title[:400],
                       "source": source["name"], "source_id": source["id"], "group": source["group"],
                       "published_at": published.isoformat() if published else None,
                       "discovered_at": now.isoformat(), "text": text[:6000], "text_kind": "page"})
        if len(result) >= source.get("max_items", 5):
            break
    return result
