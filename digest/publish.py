"""Export only public archive data; deliver an optional bounded Telegram message."""
from datetime import datetime, timezone
from hashlib import sha256
from zoneinfo import ZoneInfo
from html import escape, unescape
import re
import json
import os
from pathlib import Path
import shutil
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

from .net import post_json
from .pipeline import issue_files, load_config, read_json, write_json


def markdown(issue):
    parts = [f"# Утро · {issue['date']}", "", issue["intro"], ""]
    if issue.get("coverage_note"):
        parts += ["> " + issue["coverage_note"], ""]
    for group in issue["groups"]:
        stories = [s for s in issue["stories"] if s["group"] == group["id"]]
        if not stories:
            continue
        parts += ["## " + group["name"], ""]
        for story in stories:
            parts += ["### " + ("🔥 " if story.get("important") else "") + story["title"], "", story["summary"], ""]
            if story["why_it_matters"]:
                parts += [story["why_it_matters"], ""]
            parts += [f"- [{s['source']}]({s['url']})" for s in story["sources"]]
            for previous in story.get("previous_stories", []):
                parts.append(f"Ранее: [{previous['date']} · {previous['title']}]({previous['issue_id']}.html#story-{previous['story_id']})")
            parts.append("")
    return "\n".join(parts)


def issue_html(issue):
    sections = []
    for group in issue["groups"]:
        stories = [s for s in issue["stories"] if s["group"] == group["id"]]
        if not stories:
            continue
        cards = []
        for story in stories:
            links = " · ".join(f'<a href="{escape(s["url"], quote=True)}" rel="noreferrer">'
                               f'{escape(s["source"])}</a>' for s in story["sources"])
            earlier = " ".join(f'<a href="{escape(p["issue_id"])}.html#story-{escape(p["story_id"])}">Ранее: {escape(p["date"])} · {escape(p["title"])}</a>' for p in story.get("previous_stories", []))
            badge = "🔥 Важное · " if story.get("important") else ""
            cards.append(f'<article id="story-{escape(story["id"])}" class="{"important" if story.get("important") else ""}"><h3>{badge}{escape(story["title"])}</h3><p>{escape(story["summary"])}</p>'
                         f'<p>{escape(story["why_it_matters"])}</p><p>{links}</p><p>{earlier}</p></article>')
        sections.append(f'<section><h2>{escape(group["name"])}</h2>{"".join(cards)}</section>')
    return ('<!doctype html><html lang="ru"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>Утро · {escape(issue["date"])}</title><link rel="stylesheet" href="../style.css">'
            '<body class="permalink"><main><a href="../">Все выпуски</a>'
            f'<h1>Утро · {escape(issue["date"])}</h1><p>{escape(issue["intro"])}</p>'
            f'<p>{escape(issue.get("coverage_note", ""))}</p>{"".join(sections)}</main></body></html>')


def build(root, output=None):
    config = load_config(root)
    output = output or root / "site"
    output.mkdir(parents=True, exist_ok=True)
    for path in (root / "web").iterdir():
        if path.is_file():
            shutil.copy2(path, output / path.name)
    (output / ".nojekyll").touch()
    issues = sorted([read_json(path) for path in issue_files(root)],
                    key=lambda issue: issue["created_at"], reverse=True)
    telegraph = read_json(root / "data/telegraph.json", {})
    for issue in issues:
        if issue["id"] in telegraph:
            issue["instant_view_url"] = telegraph[issue["id"]]["url"]
    # No state, preferences prompt, credentials, or full article texts in the site artifact.
    public = {"title": config["title"], "description": config["description"],
              "timezone": config["timezone"], "morning_hour": config["morning_hour"],
              "groups": config["groups"], "issues": issues,
              "sources": [{key: s[key] for key in ["id", "name", "url", "group"]}
                          for s in config["sources"]],
              "status": read_json(root / "data/status.json", {})}
    write_json(output / "archive.json", public)
    (output / "issues").mkdir(exist_ok=True)
    for issue in issues:
        (output / "issues" / (issue["id"] + ".md")).write_text(markdown(issue), encoding="utf-8")
        (output / "issues" / (issue["id"] + ".html")).write_text(issue_html(issue), encoding="utf-8")
    feed = {"version": "https://jsonfeed.org/version/1.1", "title": config["title"],
            "description": config["description"],
            "items": [{"id": issue["id"], "title": f"Утро · {issue['date']}",
                       "date_published": issue["created_at"], "content_text": markdown(issue)}
                      for issue in issues[:30]]}
    write_json(output / "feed.json", feed)
    print(f"Built static site with {len(issues)} issue(s).")


def telegram_text(issue, site_url="", instant_view_url=""):
    """Telegram's limit applies after HTML entities are parsed, not to URL markup."""
    def visible_length(value):
        return len(unescape(re.sub(r"<[^>]*>", "", value)).encode("utf-16-le")) // 2

    for title_limit, summary_limit in [(160, 140), (110, 70), (100, 0)]:
        parts = [f"<b>Утро · {escape(issue['date'])}</b>"]
        if not issue["stories"]:
            parts.append(escape(issue["intro"][:1000]))
        for group in issue["groups"]:
            stories = [story for story in issue["stories"] if story["group"] == group["id"]]
            if not stories:
                continue
            parts.append("<b>" + escape(group["name"]) + "</b>")
            for story in stories:
                title = escape(story["title"][:title_limit])
                url = escape(story["sources"][0]["url"], quote=True)
                line = ("🔥 " if story.get("important") else "") + f'<a href="{url}">{title}</a>'
                if summary_limit:
                    line += "\n" + escape(story["summary"][:summary_limit])
                parts.append(line)
        if instant_view_url:
            parts.append(f'<a href="{escape(instant_view_url, quote=True)}">Читать полный выпуск · Instant View</a>')
        if site_url:
            url = escape(site_url + "?issue=" + issue["id"], quote=True)
            parts.append(f'<a href="{url}">Архив, темы и все источники</a>')
        text = "\n\n".join(parts)
        if visible_length(text) <= 3500:
            return text
    raise ValueError("Telegram message too large; reduce max_stories")


def deliver(root):
    config = load_config(root)
    if not config.get("telegram", {}).get("enabled"):
        print("Telegram is disabled; issues are published on the website.")
        return
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("::notice::Telegram awaits TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID secrets; no message sent.")
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as output:
                output.write("## Telegram: setup required\nAdd TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID repository secrets. No message was sent.\n")
        return
    receipts_path = root / "data/deliveries.json"
    receipts = read_json(receipts_path, {})
    pages = read_json(root / "data/telegraph.json", {})
    # Do not unexpectedly send an entire old archive when Telegram is first enabled.
    issues = [read_json(path) for path in issue_files(root) if path.stem[:10] ==
              datetime.now(ZoneInfo(config["timezone"])).date().isoformat()]
    for issue in reversed(issues):
        if issue.get("retrospective"):
            continue
        if issue["provider"] == "links" and not config.get("telegram", {}).get("allow_links", False):
            print("::notice::Telegram is waiting for an AI provider; raw feed excerpts are not sent.")
            continue
        iv_url = pages.get(issue["id"], {}).get("url", "")
        if config.get("telegram", {}).get("instant_view") and not iv_url:
            raise ValueError("Publish Instant View before Telegram delivery")
        message = telegram_text(issue, os.environ.get("DIGEST_SITE_URL", ""), iv_url)
        fingerprint = sha256(message.encode()).hexdigest()
        previous = receipts.get(issue["id"], {})
        if previous.get("hash") == fingerprint:
            continue
        method = "editMessageText" if previous.get("message_id") else "sendMessage"
        payload = {"chat_id": chat, "text": message, "parse_mode": "HTML",
                   "link_preview_options": ({"url": iv_url, "prefer_small_media": True}
                                            if iv_url else {"is_disabled": True})}
        if method == "editMessageText":
            payload["message_id"] = previous["message_id"]
        response = post_json("https://api.telegram.org/bot" + token + "/" + method,
                             payload, {}, retries=0)
        if response.get("ok") is not True:
            raise RuntimeError("Telegram did not confirm delivery")
        result = response.get("result")
        message_id = result["message_id"] if isinstance(result, dict) else previous["message_id"]
        receipts[issue["id"]] = {**previous,
            "sent_at": previous.get("sent_at", datetime.now(timezone.utc).isoformat()),
            "message_id": message_id, "hash": fingerprint}
        write_json(receipts_path, receipts)
        print(f"Delivered issue {issue['id']}.")
