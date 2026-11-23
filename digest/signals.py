"""Dated HN discussions, GitHub release history and honest popularity snapshots."""
from datetime import datetime, timedelta, timezone
import json
import os
from urllib.parse import urlencode, urlsplit

from .feeds import article_id, collect, parse_date, plain_text, canonical_url
from .net import http


def github_http(url, **kwargs):
    token = os.environ.get("GITHUB_TOKEN")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    return http(url, headers=headers, **kwargs)


def item(source, url, title, published, now, text, **extra):
    url = canonical_url(url)
    return {"id": article_id(url, extra.pop("revision", "")), "url": url, "title": title[:400],
            "source": source["name"], "source_id": source["id"], "group": source["group"],
            "published_at": published, "discovered_at": now.isoformat(),
            "text": text[:6000], "text_kind": "feed", **extra}


def hn(source, now):
    # Ranking is an attention signal observed now; it cannot reconstruct historical scores.
    start = now - timedelta(days=source.get("days", 2))
    params = {"tags": "story", "query": source.get("query", ""), "hitsPerPage": 100,
              "numericFilters": f"created_at_i>{int(start.timestamp())},created_at_i<={int(now.timestamp())},points>={source.get('min_points', 30)}"}
    data = json.loads(http("https://hn.algolia.com/api/v1/search?" + urlencode(params)))
    result = []
    for row in data.get("hits", []):
        discussion = "https://news.ycombinator.com/item?id=" + row["objectID"]
        target = row.get("url") or discussion
        text = (f"HN discussion: {discussion}. Linked project/article: {target}. "
                f"Attention observed at collection: {row.get('points', 0)} points, "
                f"{row.get('num_comments', 0)} comments. These are NOT historical-day scores. "
                + plain_text(row.get("story_text") or ""))
        result.append(item(source, discussion, row["title"], row["created_at"], now, text))
    return result


def github_projects(source, now):
    query = source.get("query", "topic:ai-agents") + " archived:false stars:>=100"
    if source.get("created_days"):
        query += " created:>=" + (now - timedelta(days=source["created_days"])).date().isoformat()
    query += " pushed:>=" + (now - timedelta(days=7)).date().isoformat()
    data = json.loads(github_http("https://api.github.com/search/repositories?" + urlencode(
        {"q": query, "sort": "stars", "order": "desc", "per_page": 15})))
    return [item(source, row["html_url"], row["full_name"], now.isoformat(), now,
                 f"Repository description: {row.get('description') or ''}. "
                 f"Topics: {', '.join(row.get('topics', []))}. "
                 f"Observed stars: {row['stargazers_count']}. Last push: {row['pushed_at']}. "
                 "This is an active popular repository snapshot, NOT a new release or evidence of growth. "
                 "Only call it gaining popularity when a previous snapshot gives a positive delta.",
                 revision=now.date().isoformat(), stars=row["stargazers_count"],
                 date_kind="discovery") for row in data.get("items", [])]


def github_releases(source, now):
    parts = urlsplit(source["url"]).path.strip("/").split("/")
    repo = "/".join(parts[:2])
    result = []
    if os.environ.get("GITHUB_TOKEN"):
        owner, name = parts[:2]
        query = ("query($owner:String!,$name:String!){repository(owner:$owner,name:$name){"
                 "releases(first:100,orderBy:{field:CREATED_AT,direction:DESC}){nodes{"
                 "name tagName url publishedAt description isDraft}}}}")
        data = json.loads(github_http("https://api.github.com/graphql", payload={
            "query": query, "variables": {"owner": owner, "name": name}}))
        if data.get("errors") or not data.get("data", {}).get("repository"):
            raise RuntimeError("GitHub release query failed")
        for row in data["data"]["repository"]["releases"]["nodes"]:
            published = parse_date(row["publishedAt"])
            if not row["isDraft"] and published and published <= now:
                result.append(item(source, row["url"], row["name"] or row["tagName"],
                                   published.isoformat(), now, plain_text(row.get("description", ""))))
        return result
    # Bounded pagination is needed for projects with many daily prereleases.
    for page in range(1, 7):
        rows = json.loads(github_http(f"https://api.github.com/repos/{repo}/releases?per_page=20&page={page}", limit=6_000_000))
        for row in rows:
            if row.get("draft") or not row.get("published_at"):
                continue
            published = parse_date(row["published_at"])
            if published <= now:
                result.append(item(source, row["html_url"], row["name"] or row["tag_name"],
                                   published.isoformat(), now, plain_text(row.get("body", ""))))
        if not rows or any(parse_date(row.get("published_at")) and
                           parse_date(row["published_at"]) < now - timedelta(days=source.get("history_days", 2)) for row in rows):
            break
    return result


def fetch_signal(source, now):
    if source["type"] == "hn":
        return hn(source, now)
    if source["type"] == "github-projects":
        return github_projects(source, now)
    raise ValueError("Unknown signal type")


def collect_historical(sources, now):
    dated = []
    for source in sources:
        if source.get("type") == "github-projects":
            continue  # Today's stars cannot be assigned to a past day.
        dated.append(source)
    return collect(dated, now, historical=True)
