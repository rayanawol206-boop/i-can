"""Open the most-watched YouTube video for a scanned topic, and close that tab."""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from urllib.parse import quote_plus


log = logging.getLogger("ican.youtube")

APP_DIR = Path(__file__).resolve().parent
TOPICS_PATH = APP_DIR / "youtube_topics.json"

OFFLINE_EN = "Internet connection required for YouTube."
OFFLINE = {
    "en": OFFLINE_EN,
    "am": "ዩቲዩብ ለመክፈት ኢንተርኔት ያስፈልጋል።",
    "om": "YouTube banuuf interneetii barbaachisa.",
    "ti": "ዩቲዩብ ንምኽፋት ኢንተርነት የድሊ።",
}
CLOSE_LABEL = {
    "en": "CLOSE YOUTUBE / BACK TO APP\nOr move your mouth twice",
    "am": "CLOSE YOUTUBE / BACK TO APP\nዩቲዩብ ዝጋ",
    "om": "CLOSE YOUTUBE / BACK TO APP\nYOUTUBE CULI",
    "ti": "CLOSE YOUTUBE / BACK TO APP\nዩቲዩብ ዕጸው",
}
OPENING = {
    "en": "Opening the most-watched video…",
    "am": "በጣም የታየውን ቪዲዮ እየከፈትን ነው…",
    "om": "Viidiyoo baay'ee ilaalame banuu…",
    "ti": "ዝያዳ ዝተራእየ ቪድዮ ንኸፍት ኣለና…",
}
OPENED = {
    "en": "Video is open. Select CLOSE YOUTUBE when you want to come back.",
    "am": "ቪዲዮው ተከፍቷል። ለመመለስ CLOSE YOUTUBE ይምረጡ።",
    "om": "Viidiyoon banameera. Yoo deebi'u barbaadde CLOSE YOUTUBE filadhu.",
    "ti": "ቪድዮ ተኸፊቱ ኣሎ። ክትምለስ ምስ እትደሊ CLOSE YOUTUBE ምረጽ።",
}


def load_topics(path: Path | None = None) -> dict:
    source = path or TOPICS_PATH
    return json.loads(source.read_text(encoding="utf-8"))


def internet_ok(timeout: float = 3.0) -> bool:
    """True when a short connection to YouTube succeeds."""
    import socket

    try:
        with socket.create_connection(("www.youtube.com", 443), timeout=timeout):
            return True
    except OSError:
        return False


def offline_message(language: str) -> str:
    local = OFFLINE.get(language, OFFLINE_EN)
    if language == "en" or local == OFFLINE_EN:
        return OFFLINE_EN
    return f"{OFFLINE_EN} {local}"


def search_url(query: str) -> str:
    """YouTube results sorted by view count. CAM= is the view-count sort token."""
    return f"https://www.youtube.com/results?search_query={quote_plus(query)}&sp=CAM%3D"


def direct_watch_url(query: str) -> str:
    """Open the first video on a view-sorted search, without bringing another app forward."""
    import re
    import urllib.request

    page = search_url(query)
    try:
        request = urllib.request.Request(
            page,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(request, timeout=8) as response:
            html = response.read().decode("utf-8", "replace")
        seen: list[str] = []
        for video_id in re.findall(r'"videoId":"([0-9A-Za-z_-]{11})"', html):
            if video_id not in seen:
                seen.append(video_id)
        if seen:
            return f"https://www.youtube.com/watch?v={seen[0]}"
    except Exception:
        log.info("direct video lookup missed; opening the view-sorted search")
    return page


def top_video_url(query: str) -> str:
    """Most-viewed watch URL, or a view-sorted search page if the lookup fails."""
    found = _ytdlp_top(query)
    return found or search_url(query)


def open_video(url: str) -> None:
    import webbrowser

    webbrowser.open(url, new=2)


def close_browser_tab() -> bool:
    """Send Ctrl+W to the YouTube or browser window. Returns False if none is found."""
    if __import__("sys").platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    hwnd = _find_browser(user32, wintypes)
    if not hwnd:
        return False
    user32.keybd_event(0x12, 0, 0, 0)
    user32.ShowWindow(hwnd, 9)
    user32.SetForegroundWindow(hwnd)
    user32.keybd_event(0x12, 0, 0x0002, 0)
    time.sleep(0.25)
    for vk, flags in ((0x11, 0), (0x57, 0), (0x57, 0x0002), (0x11, 0x0002)):
        user32.keybd_event(vk, 0, flags, 0)
        time.sleep(0.04)
    return True


def category_board(language: str, topics: dict, back: str, caption: str) -> tuple[list, list]:
    items = [("ytcat", cat["id"], _label(cat, language)) for cat in topics["categories"]]
    rows = _pairs(items)
    rows.append([("back", "", back), ("language", "", caption)])
    return rows, _names(rows)


def topic_board(language: str, topics: dict, category_id: str, back: str, caption: str) -> tuple[list, list]:
    category = _category(topics, category_id)
    items = [("watch", topic["id"], _label(topic, language)) for topic in category["topics"]]
    rows = _pairs(items)
    rows.append([("back", "", back), ("language", "", caption)])
    return rows, _names(rows)


def close_board(language: str) -> tuple[list, list]:
    label = CLOSE_LABEL.get(language, CLOSE_LABEL["en"])
    rows = [[("close", "", label)]]
    return rows, ["close YouTube"]


def topic_query(topics: dict, topic_id: str) -> str:
    for category in topics["categories"]:
        for topic in category["topics"]:
            if topic["id"] == topic_id:
                return str(topic["query"])
    return ""


def _category(topics: dict, category_id: str) -> dict:
    for category in topics["categories"]:
        if category["id"] == category_id:
            return category
    return topics["categories"][0]


def _label(item: dict, language: str) -> str:
    labels = item["label"]
    return str(labels.get(language) or labels.get("en") or "")


def _pairs(items: list) -> list:
    rows = []
    chunk: list = []
    for item in items:
        chunk.append(item)
        if len(chunk) == 2:
            rows.append(chunk)
            chunk = []
    if chunk:
        rows.append(chunk)
    return rows


def _names(rows: list) -> list[str]:
    return [" · ".join(label for _kind, _value, label in row) for row in rows]


def _ytdlp_top(query: str) -> str:
    holder: dict[str, str] = {}

    def run() -> None:
        try:
            import yt_dlp
        except Exception:
            log.info("yt-dlp is not installed; opening a view-sorted search")
            return
        try:
            options = {
                "quiet": True,
                "no_warnings": True,
                "skip_download": True,
                "noplaylist": True,
                "ignoreerrors": True,
                "socket_timeout": 10,
                "retries": 1,
            }
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(f"ytsearch6:{query}", download=False)
            entries = [item for item in (info or {}).get("entries") or [] if item]
            if not entries:
                return
            entries.sort(key=lambda item: item.get("view_count") or 0, reverse=True)
            best = entries[0]
            video_id = str(best.get("id") or "")
            if video_id and " " not in video_id and not video_id.startswith("http"):
                holder["url"] = f"https://www.youtube.com/watch?v={video_id}"
                return
            page = str(best.get("webpage_url") or best.get("url") or "")
            if page.startswith("http"):
                holder["url"] = page
        except Exception:
            log.exception("yt-dlp search failed")

    worker = threading.Thread(target=run, name="ican-ytdlp", daemon=True)
    worker.start()
    worker.join(18)
    return holder.get("url", "")


def _find_browser(user32, wintypes):
    import ctypes

    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def _each(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.lower()
        if "youtube" in title:
            found.append((0, hwnd))
        elif any(name in title for name in ("google chrome", "microsoft edge", "firefox", "opera", "brave")):
            found.append((1, hwnd))
        return True

    user32.EnumWindows(_each, 0)
    if not found:
        return None
    found.sort(key=lambda item: item[0])
    return found[0][1]
