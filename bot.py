#!/usr/bin/env python3
"""Posts new items from RSS/Atom feeds (F1 YouTube, F1.com, Motorsport.com...) to a Telegram channel.

No extra packages needed. Runs on GitHub Actions every 15 minutes.
Settings come from environment variables:
  TELEGRAM_BOT_TOKEN  - token from @BotFather
  TELEGRAM_CHAT_ID    - e.g. @mychannel (public) or -100123456789 (private)
  DRY_RUN=1           - print what would be sent instead of sending
"""
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

FEEDS_FILE = "feeds.txt"
STATE_FILE = "seen.json"
MAX_POSTS_PER_RUN = 8        # safety limit so you never get flooded
KEEP_SEEN = 3000             # how many old links to remember
UA = {"User-Agent": "Mozilla/5.0 (compatible; telegram-feed-bot/1.0)"}

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DRY_RUN = os.environ.get("DRY_RUN") == "1"
LOOP_MINUTES = float(os.environ.get("LOOP_MINUTES", "0"))   # how long to keep watching (0 = check once)
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "60"))   # how often to check
SIGNATURE = "🏁 @Live_F1 🏁"   # line shown at the bottom of every post


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def local(tag):
    return tag.rsplit("}", 1)[-1]


def clean_link(url):
    """Remove tracking junk (utm_...) so the same story isn't posted twice."""
    p = urllib.parse.urlsplit(url.strip())
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query) if not k.startswith("utm_")]
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, urllib.parse.urlencode(q), ""))


def resolve_youtube(handle):
    """Turn @formula1 into the channel's hidden RSS feed address."""
    page = fetch(f"https://www.youtube.com/{handle}").decode("utf-8", "ignore")
    m = re.search(r'"channelId":"(UC[\w-]{22})"', page) or re.search(
        r'channel_id=(UC[\w-]{22})', page)
    if not m:
        raise RuntimeError(f"Could not find channel id for {handle}")
    return f"https://www.youtube.com/feeds/videos.xml?channel_id={m.group(1)}"


def load_sources():
    """feeds.txt lines look like:  Label | address   (address may be youtube:@handle)"""
    sources = []
    with open(FEEDS_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            label, _, addr = line.partition("|")
            sources.append((label.strip(), addr.strip()))
    return sources


def parse_feed(data):
    """Return a list of (title, link) from an RSS or Atom feed, newest first as in the feed."""
    root = ET.fromstring(data)
    items = []
    for el in root.iter():
        if local(el.tag) not in ("item", "entry"):
            continue
        title, link = "", ""
        for child in el:
            name = local(child.tag)
            if name == "title":
                title = (child.text or "").strip()
            elif name == "link":
                href = child.attrib.get("href")
                if href:
                    if child.attrib.get("rel", "alternate") == "alternate" or not link:
                        link = href
                elif child.text and not link:
                    link = child.text
        if title and link:
            items.append((html.unescape(title), clean_link(link)))
    return items


def send(label, title, link):
    # Layout: title, link, empty line, channel signature (plain text, like the IFTTT version)
    text = f"{title}\n{link}\n\n{SIGNATURE}"
    if DRY_RUN:
        print("WOULD SEND:\n" + text + "\n")
        return True
    payload = urllib.parse.urlencode({
        "chat_id": CHAT_ID, "text": text,
        # Web page preview ON, always built from the video/article link
        "link_preview_options": json.dumps({"is_disabled": False, "url": link}),
    }).encode()
    try:
        urllib.request.urlopen(
            urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/sendMessage", data=payload),
            timeout=30)
        return True
    except urllib.error.HTTPError as e:
        print(f"Telegram error {e.code}: {e.read().decode('utf-8', 'ignore')}")
        if e.code == 429:
            time.sleep(10)
        return False


_yt_cache = {}


def save_state(seen):
    with open(STATE_FILE, "w") as f:
        json.dump(seen[-KEEP_SEEN:], f, indent=0)


def push_state():
    """On GitHub, save the memory file so a restart never re-posts old items."""
    if DRY_RUN or not os.environ.get("GITHUB_ACTIONS"):
        return
    try:
        subprocess.run(["git", "config", "user.name", "github-actions"], check=True)
        subprocess.run(["git", "config", "user.email", "github-actions@users.noreply.github.com"], check=True)
        subprocess.run(["git", "add", STATE_FILE], check=True)
        if subprocess.run(["git", "diff", "--staged", "--quiet"]).returncode != 0:
            subprocess.run(["git", "commit", "-m", "update seen list"], check=True)
            subprocess.run(["git", "pull", "--rebase", "-q"], check=False)
            subprocess.run(["git", "push", "-q"], check=True)
    except Exception as e:
        print(f"[warn] could not save memory file: {e}")


def check_once(seen, first_run):
    """One pass over all feeds. Posts anything new. Returns True if `seen` changed."""
    seen_set = set(seen)
    new_items = []  # (label, title, link)
    for label, addr in load_sources():
        try:
            if addr.startswith("youtube:"):
                handle = addr.split(":", 1)[1]
                if handle not in _yt_cache:
                    _yt_cache[handle] = resolve_youtube(handle)
                addr = _yt_cache[handle]
            items = parse_feed(fetch(addr))
        except Exception as e:  # one broken website must not stop the others
            print(f"[skip] {label}: {e}")
            continue
        fresh = [(label, t, l) for t, l in items if l not in seen_set]
        if fresh:
            print(f"{label}: {len(items)} items, {len(fresh)} new")
        for item in reversed(fresh):  # oldest first
            if item[2] not in {i[2] for i in new_items}:
                new_items.append(item)

    changed = False
    if first_run:
        print("First run: remembering current items without posting them.")
        seen += [i[2] for i in new_items]
        return True
    if len(new_items) > MAX_POSTS_PER_RUN:
        for item in new_items[:-MAX_POSTS_PER_RUN]:
            seen.append(item[2])  # skipped extras are marked as seen
            changed = True
        new_items = new_items[-MAX_POSTS_PER_RUN:]
    for label, title, link in new_items:
        if send(label, title, link):
            seen.append(link)
            changed = True
            if not DRY_RUN:
                time.sleep(2)
    return changed


def main():
    if not DRY_RUN and (not TOKEN or not CHAT_ID):
        sys.exit("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID secrets.")

    first_run = not os.path.exists(STATE_FILE)
    seen = []
    if not first_run:
        with open(STATE_FILE) as f:
            seen = json.load(f)

    deadline = time.time() + LOOP_MINUTES * 60   # LOOP_MINUTES=0 -> single pass
    while True:
        try:
            changed = check_once(seen, first_run)
        except Exception as e:  # never die because of a temporary problem
            print(f"[error] {e}")
            changed = False
        first_run = False
        if changed and not DRY_RUN:
            save_state(seen)
            push_state()
        if time.time() + POLL_SECONDS >= deadline:
            break
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
