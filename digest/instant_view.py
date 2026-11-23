"""Publish complete, public issues to Telegraph for Telegram Instant View."""
from hashlib import sha256
import json
import os
from urllib.parse import urlsplit

from .net import post_json
from .pipeline import issue_files, load_config, read_json, write_json


def content(issue, site_url):
    def text(tag, value):
        return {"tag": tag, "children": [value]}
    nodes = [text("p", issue["intro"])]
    if issue.get("coverage_note"):
        nodes.append(text("blockquote", issue["coverage_note"]))
    for group in issue["groups"]:
        stories = [s for s in issue["stories"] if s["group"] == group["id"]]
        if not stories:
            continue
        nodes.append(text("h3", group["name"]))
        for story in stories:
            nodes += [text("h4", ("🔥 " if story.get("important") else "") + story["title"]),
                      text("p", story["summary"])]
            if story["why_it_matters"]:
                nodes.append(text("p", story["why_it_matters"]))
            for source in story["sources"]:
                nodes.append({"tag": "p", "children": [{"tag": "a", "attrs": {"href": source["url"]},
                                                        "children": [source["source"]]}]})
            for previous in story.get("previous_stories", []):
                url = site_url + "?issue=" + previous["issue_id"] + "&story=" + previous["story_id"]
                nodes.append({"tag": "p", "children": ["Ранее: ", {"tag": "a", "attrs": {"href": url},
                    "children": [previous["date"] + " · " + previous["title"]]}]})
    if site_url:
        nodes.append({"tag": "p", "children": [{"tag": "a", "attrs": {"href": site_url + "?issue=" + issue["id"]},
                                               "children": ["Архив, темы и фильтры"]}]})
    if len(json.dumps(nodes, ensure_ascii=False).encode()) > 64000:
        raise ValueError("Issue exceeds Telegraph's 64 KB content limit")
    return nodes


def publish(root):
    config = load_config(root)
    if not config.get("telegram", {}).get("instant_view"):
        return
    token = os.environ.get("TELEGRAPH_ACCESS_TOKEN")
    if not token:
        raise ValueError("Instant View requires TELEGRAPH_ACCESS_TOKEN repository secret")
    site_url = os.environ.get("DIGEST_SITE_URL", "https://mike0sv.github.io/news-digest/").rstrip('/') + '/'
    path = root / "data/telegraph.json"
    pages = read_json(path, {})
    for issue_path in reversed(issue_files(root)):
        issue = read_json(issue_path)
        nodes = content(issue, site_url)
        fingerprint = sha256(json.dumps(nodes, ensure_ascii=False).encode()).hexdigest()
        previous = pages.get(issue["id"], {})
        if previous.get("hash") == fingerprint:
            continue
        payload = {"access_token": token, "title": "Утро · " + issue["date"],
                   "author_name": config["title"], "author_url": site_url, "content": nodes,
                   "return_content": False}
        method = "createPage"
        if previous.get("path"):
            method = "editPage"
            payload["path"] = previous["path"]
        response = post_json("https://api.telegra.ph/" + method, payload, {}, retries=0)
        if response.get("ok") is not True:
            raise RuntimeError("Telegraph did not confirm publication")
        result = response["result"]
        if urlsplit(result["url"]).hostname != "telegra.ph":
            raise ValueError("Invalid Telegraph response URL")
        pages[issue["id"]] = {"url": result["url"], "path": result["path"], "hash": fingerprint}
        write_json(path, pages)  # No account token or editing/auth URL is stored here.
        print(f"Instant View published for {issue['id']}.")
