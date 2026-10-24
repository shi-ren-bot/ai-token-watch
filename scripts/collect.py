#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抓取公开来源，筛选「AI 免费 Token / 额度」相关线索，生成 Markdown 日报。

仅使用标准库，便于在 GitHub Actions 上零依赖运行。
用法：python scripts/collect.py --out reports [--summary 路径]
"""

import argparse
import datetime as dt
import html
import json
import os
import re
import smtplib
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.message import EmailMessage
from pathlib import Path

TZ = dt.timezone(dt.timedelta(hours=8))
UA = "Mozilla/5.0 (compatible; ai-token-watch/1.0; +https://github.com)"
TIMEOUT = 25
MAX_AGE_DAYS = 14

# 必须命中的 AI 相关词（英文按整词匹配，避免 api/app 之类的误命中）
AI_WORDS = [
    "ai", "人工智能", "大模型", "模型", "gpt", "chatgpt", "openai", "claude", "gemini",
    "llm", "token", "deepseek", "kimi", "qwen", "通义", "文心", "豆包", "智谱",
    "glm", "moonshot", "grok", "llama", "mistral", "sora", "midjourney", "cursor",
    "api key", "api 额度",
]

# 免费额度相关词（命中越多排序越前）
FREE_WORDS = [
    "免费", "赠送", "白嫖", "送额度", "白给", "免费额度", "免费试用", "试用",
    "兑换码", "优惠码", "邀请码", "注册送", "限时", "公益", "免费领取",
    "free credit", "free credits", "free token", "free tokens", "free tier",
    "free trial", "credits", "coupon", "gift", "no cost",
]

# 更强的「确有免费额度」信号，用于过滤泛泛提到 AI 的新闻
FREE_STRONG = [
    "免费额度", "免费领取", "免费试用", "注册送", "赠送", "白嫖", "兑换码", "邀请码",
    "free credit", "free credits", "free token", "free tokens", "free tier",
    "free trial", "credits", "限时免费", "免费 api", "免费 llm", "免费 gpt", "免费 token",
]

NOISE_WORDS = ["招聘", "求职", "接单", "外包", "课程报名", "培训招生", "overcharged", "overcharge", "扣费", "误扣", "计费错误"]

RELAY_WORDS = ["中转", "镜像", "代理站", "公益站", "共享", "relay", "one-api", "new-api", "反向代理", "中转站"]
OFFICIAL_WORDS = ["官方", "新用户", "官网", "发布", "上线", "开放", "免费试用", "free tier", "beta", "公测"]

FEEDS = [
    ("Solidot", "https://www.solidot.org/index.rss"),
    ("少数派", "https://sspai.com/feed"),
    ("开源中国", "https://www.oschina.net/news/rss"),
]

HN_QUERIES = ["free credits", "free tokens", "free tier LLM", "API credits"]


def now():
    return dt.datetime.now(TZ)


def fetch(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8", "replace")


def strip_html(text):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()


def localname(tag):
    return tag.split("}")[-1]


def child_text(node, names):
    for child in node:
        if localname(child.tag) in names and (child.text or "").strip():
            return child.text
    return ""


def parse_date(value):
    if not value:
        return None
    value = value.strip()
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(TZ)
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(value).astimezone(TZ)
    except Exception:
        return None


def add(items, title, url, source, published=None, summary=""):
    if not title or not url:
        return
    items.append({
        "title": strip_html(title),
        "url": url.strip(),
        "source": source,
        "published": published,
        "summary": strip_html(summary)[:240],
    })


def from_v2ex(items):
    data = json.loads(fetch("https://www.v2ex.com/api/topics/latest.json"))
    for t in data:
        published = None
        if t.get("created"):
            published = dt.datetime.fromtimestamp(int(t["created"]), TZ)
        add(items, t.get("title"), t.get("url"), "V2EX", published, t.get("content", ""))


def from_hn(items):
    for q in HN_QUERIES:
        url = (
            "https://hn.algolia.com/api/v1/search_by_date?tags=story&hitsPerPage=25&query="
            + urllib.parse.quote(q)
        )
        for hit in json.loads(fetch(url)).get("hits", []):
            published = parse_date(hit.get("created_at"))
            target = hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
            add(items, hit.get("title"), target, "Hacker News", published, hit.get("story_text") or "")


def from_feeds(items):
    for name, feed_url in FEEDS:
        try:
            root = ET.fromstring(fetch(feed_url))
        except ET.ParseError:
            continue
        for node in root.iter():
            if localname(node.tag) not in ("item", "entry"):
                continue
            title = child_text(node, {"title"})
            link = child_text(node, {"link"})
            if not link:
                for child in node:
                    if localname(child.tag) == "link" and child.get("href"):
                        link = child.get("href")
                        break
            published = parse_date(child_text(node, {"pubDate", "published", "updated"}))
            summary = child_text(node, {"description", "summary", "content"})
            add(items, title, link, name, published, summary)


def from_github(items):
    token = os.environ.get("GITHUB_TOKEN")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    query = urllib.parse.quote('"free api" OR "free gpt" OR "free llm" in:name,description')
    url = f"https://api.github.com/search/repositories?q={query}&sort=updated&order=desc&per_page=20"
    for repo in json.loads(fetch(url, headers)).get("items", []):
        desc = repo.get("description") or ""
        if not repo.get("archived") and repo.get("pushed_at"):
            published = parse_date(repo["pushed_at"])
            add(items, repo.get("full_name"), repo.get("html_url"), "GitHub", published, desc)


def has(text, word):
    """英文词按整词边界匹配，中文按子串匹配。"""
    if word.isascii():
        return re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", text) is not None
    return word in text


def any_hit(text, words):
    return any(has(text, w) for w in words)


def relevant(item):
    text = f"{item['title']} {item['summary']}".lower()
    if not any_hit(text, AI_WORDS):
        return False
    if any_hit(text, NOISE_WORDS):
        return False
    return any_hit(text, FREE_STRONG)


def score(item):
    text = f"{item['title']} {item['summary']}".lower()
    return sum(1 for w in FREE_WORDS if has(text, w))


def classify(item):
    text = f"{item['title']} {item['summary']}"
    if any_hit(text, RELAY_WORDS):
        return "relay"
    if any_hit(text, OFFICIAL_WORDS):
        return "official"
    return "other"


def dedup(items):
    seen_url, seen_title, result = set(), set(), []
    for item in items:
        url_key = re.sub(r"[?#].*$", "", item["url"].lower().rstrip("/"))
        title_key = re.sub(r"\W+", "", item["title"].lower())[:40]
        if url_key in seen_url or (title_key and title_key in seen_title):
            continue
        seen_url.add(url_key)
        seen_title.add(title_key)
        result.append(item)
    return result


def sorted_items(items, cutoff):
    kept = []
    for item in items:
        published = item["published"]
        if published and published < cutoff:
            continue
        kept.append(item)
    kept.sort(key=lambda x: (-score(x), x["published"] or dt.datetime.min.replace(tzinfo=TZ)))
    return kept


def fmt_time(value):
    return value.strftime("%m-%d %H:%M") if value else "时间未知"


def render(groups, generated_at, total, errors):
    lines = [
        f"# AI 免费额度日报 · {generated_at:%Y-%m-%d}",
        "",
        f"> 生成时间：{generated_at:%Y-%m-%d %H:%M} (UTC+8)　有效线索：{total} 条",
        "> 来源：V2EX、Hacker News、Solidot、少数派、开源中国、GitHub 公开检索",
        "> 自动抓取结果，请自行核实活动真实性与有效期，谨防钓鱼站点。",
        "",
    ]

    highlights = [i for group in groups.values() for i in group][:5]
    lines.append("## 今日重点")
    if highlights:
        for idx, item in enumerate(highlights, 1):
            lines.append(f"{idx}. [{item['title']}]({item['url']}) — {item['source']} · {fmt_time(item['published'])}")
    else:
        lines.append("今日未检索到符合条件的新线索。")
    lines.append("")

    sections = [
        ("relay", "## 中转站 / 公益站"),
        ("official", "## 官方活动 / 厂商免费额度"),
        ("other", "## 其他线索"),
    ]
    for key, heading in sections:
        group = groups.get(key) or []
        lines.append(heading)
        if not group:
            lines.append("无。")
            lines.append("")
            continue
        for item in group:
            lines.append(f"- **[{item['title']}]({item['url']})** — {item['source']} · {fmt_time(item['published'])}")
            if item["summary"]:
                lines.append(f"  {item['summary']}")
        lines.append("")

    if errors:
        lines.append("## 抓取异常")
        for err in errors:
            lines.append(f"- {err}")
        lines.append("")

    return "\n".join(lines)


def md_to_html(text):
    """把本脚本产出的有限 Markdown 语法转成邮件可读的 HTML。"""
    blocks, list_buf = [], []

    def flush_list():
        if list_buf:
            blocks.append("<ul>" + "".join(f"<li>{i}</li>" for i in list_buf) + "</ul>")
            list_buf.clear()

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            flush_list()
            continue
        escaped = html.escape(line, quote=False)
        escaped = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r'<a href="\2">\1</a>', escaped)
        escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)

        if escaped.startswith("## "):
            flush_list()
            blocks.append(f"<h2>{escaped[3:]}</h2>")
        elif escaped.startswith("# "):
            flush_list()
            blocks.append(f"<h1>{escaped[2:]}</h1>")
        elif escaped.startswith("&gt; "):
            flush_list()
            blocks.append(f'<p class="meta">{escaped[5:]}</p>')
        elif escaped.startswith("- "):
            list_buf.append(escaped[2:])
        elif re.match(r"^\d+\. ", escaped):
            list_buf.append(re.sub(r"^\d+\. ", "", escaped))
        elif escaped.startswith("  "):
            blocks.append(f'<p class="desc">{escaped.strip()}</p>')
        else:
            flush_list()
            blocks.append(f"<p>{escaped}</p>")
    flush_list()

    style = (
        "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC',sans-serif;"
        "max-width:720px;margin:0 auto;color:#222;line-height:1.65;font-size:14px"
    )
    css = (
        "<style>h1{font-size:20px;margin:0 0 12px}h2{font-size:16px;margin:24px 0 8px;"
        "border-bottom:1px solid #eee;padding-bottom:6px}a{color:#0366d6;text-decoration:none}"
        ".meta{color:#888;font-size:12px;margin:2px 0}.desc{color:#555;margin:2px 0 8px 14px;"
        "font-size:13px}li{margin:4px 0}ul{padding-left:20px}</style>"
    )
    return f'<html><head><meta charset="utf-8">{css}</head><body><div style="{style}">' \
        + "".join(blocks) + "</div></body></html>"


def send_mail(report):
    """通过 SMTP 发送日报；未配置收件人时跳过。"""
    to_addr = os.environ.get("MAIL_TO", "").strip()
    if not to_addr:
        print("[mail] 未配置 MAIL_TO，跳过邮件发送")
        return False

    host = os.environ.get("SMTP_HOST", "").strip() or "smtp.qq.com"
    port = int(os.environ.get("SMTP_PORT", "").strip() or 465)
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASS", "").strip()
    if not user or not password:
        raise RuntimeError("已配置 MAIL_TO，但缺少 SMTP_USER 或 SMTP_PASS")

    sender = os.environ.get("MAIL_FROM", "").strip() or user
    recipients = [addr.strip() for addr in re.split(r"[,;]", to_addr) if addr.strip()]
    date_str = now().strftime("%Y-%m-%d")

    msg = EmailMessage()
    msg["Subject"] = f"AI 免费额度日报 · {date_str}"
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content(report)
    msg.add_alternative(md_to_html(report), subtype="html")

    if port == 587:
        with smtplib.SMTP(host, port, timeout=40) as server:
            server.starttls()
            server.login(user, password)
            server.send_message(msg)
    else:
        with smtplib.SMTP_SSL(host, port, timeout=40) as server:
            server.login(user, password)
            server.send_message(msg)

    print(f"[mail] 已发送至 {', '.join(recipients)}")
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="reports", help="日报输出目录")
    parser.add_argument("--summary", default="", help="同时写入该文件（如 GitHub Step Summary）")
    parser.add_argument("--mail", action="store_true", help="通过 SMTP 把日报发送到邮箱")
    args = parser.parse_args()

    items, errors = [], []
    collectors = [
        ("V2EX", from_v2ex),
        ("Hacker News", from_hn),
        ("RSS 订阅", from_feeds),
        ("GitHub", from_github),
    ]
    for name, func in collectors:
        try:
            func(items)
        except Exception as exc:  # 单个来源失败不影响整体
            errors.append(f"{name}: {type(exc).__name__} {exc}")

    generated_at = now()
    cutoff = generated_at - dt.timedelta(days=MAX_AGE_DAYS)
    candidates = dedup([i for i in items if relevant(i)])
    kept = sorted_items(candidates, cutoff)

    groups = {"relay": [], "official": [], "other": []}
    for item in kept:
        groups[classify(item)].append(item)

    report = render(groups, generated_at, len(kept), errors)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    dated = out_dir / f"report-{generated_at:%Y-%m-%d}.md"
    latest = out_dir / "latest.md"
    dated.write_text(report, encoding="utf-8")
    latest.write_text(report, encoding="utf-8")

    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as fh:
            fh.write(report + "\n")

    print(report)
    print(f"\n[ok] 已生成 {dated}，共 {len(kept)} 条线索，异常 {len(errors)} 个")

    if args.mail:
        send_mail(report)


if __name__ == "__main__":
    main()
