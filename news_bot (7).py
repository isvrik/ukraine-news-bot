"""
Бот для отслеживания новостей по теме "переговоры по Украине"
и отправки ссылок в Telegram-канал.

Как это работает:
1. Забирает новости из Google News RSS по заданным ключевым словам
   (Google News сам агрегирует ТАСС, РИА, Reuters, Bloomberg и тысячи
   других источников — не нужно искать RSS каждого сайта отдельно).
2. Дополнительно проверяет пару прямых RSS-лент (ТАСС, РИА) для надёжности.
3. Отбирает только те новости, где есть нужные ключевые слова.
4. Не отправляет повторно то, что уже было отправлено (хранит список
   отправленных ссылок в файле seen_links.json).
5. Отправляет заголовок + ссылку в Telegram-канал.
"""

import json
import os
import re
import time
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import quote

import feedparser
import requests
from deep_translator import GoogleTranslator

# ========== НАСТРОЙКИ ==========

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CHANNEL_ID = os.environ.get("CHANNEL_ID", "@news_isv_bot")

KEYWORDS = [
    "переговоры по украине",
    "мирный план украина",
    "мирное соглашение украина",
    "прекращение огня украина",
    "перемирие украина",
    "зеленский трамп переговоры",
    "путин переговоры украина",
    "ukraine peace talks",
    "ukraine ceasefire",
]

GOOGLE_NEWS_QUERY_RU = (
    'Украина ("мирные переговоры" OR "переговоры по Украине" OR '
    '"прекращение огня" OR "мирный план" OR "мирное соглашение" OR '
    "Уиткофф OR Кушнер)"
)
GOOGLE_NEWS_RSS_RU = (
    f"https://news.google.com/rss/search?q={quote(GOOGLE_NEWS_QUERY_RU)}"
    "&hl=ru&gl=RU&ceid=RU:ru"
)

# Поисковый запрос для Google News на английском.
# Сделан упор на источники, которые в основном БЕСПЛАТНЫ для чтения:
# AP (Associated Press) и Axios почти всегда открыты полностью.
# Reuters оставлен, так как тоже даёт первичную информацию быстро, но часть
# его статей может быть частично ограничена — при желании его можно убрать
# из списка ниже (просто удалите "site:reuters.com OR ").
GOOGLE_NEWS_QUERY_EN = (
    'Ukraine ("peace talks" OR ceasefire OR "peace deal" OR negotiations OR '
    "Witkoff OR Kushner) (site:apnews.com OR site:axios.com OR site:reuters.com)"
)
GOOGLE_NEWS_RSS_EN = (
    f"https://news.google.com/rss/search?q={quote(GOOGLE_NEWS_QUERY_EN)}"
    "&hl=en&gl=US&ceid=US:en"
)

PRE_FILTERED_FEEDS = {
    GOOGLE_NEWS_RSS_RU,
    GOOGLE_NEWS_RSS_EN,
}

DIRECT_FEEDS = [
    "https://tass.ru/rss/v2.xml",
    "https://ria.ru/export/rss2/archive/index.xml",
]

ALL_FEEDS = [
    GOOGLE_NEWS_RSS_RU,
    GOOGLE_NEWS_RSS_EN,
] + DIRECT_FEEDS

SEEN_FILE = Path(__file__).parent / "seen_links.json"

RECENT_TITLES_LIMIT = 150
DUPLICATE_SIMILARITY_THRESHOLD = 0.6

# ========== ЛОГИКА ==========


def load_seen() -> dict:
    if SEEN_FILE.exists():
        try:
            data = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return {"links": data, "titles": []}
            return {
                "links": data.get("links", []),
                "titles": data.get("titles", []),
            }
        except Exception:
            return {"links": [], "titles": []}
    return {"links": [], "titles": []}


def save_seen(seen_links: set, recent_titles: list) -> None:
    data = {
        "links": list(seen_links)[-2000:],
        "titles": recent_titles[-RECENT_TITLES_LIMIT:],
    }
    SEEN_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def normalize_title(title: str) -> str:
    core = re.split(r"\s+-\s+[^-]+$", title)[0]
    core = re.sub(r"[^\w\s]", "", core, flags=re.UNICODE).lower()
    core = re.sub(r"\s+", " ", core).strip()
    return core


def is_duplicate_title(title: str, recent_titles: list) -> bool:
    normalized = normalize_title(title)
    if not normalized:
        return False
    for prev in recent_titles:
        if SequenceMatcher(None, normalized, prev).ratio() >= DUPLICATE_SIMILARITY_THRESHOLD:
            return True
    return False


def is_relevant(title: str, summary: str) -> bool:
    text = f"{title} {summary}".lower()
    return any(kw.lower() in text for kw in KEYWORDS)


def translate_to_russian(text: str) -> str | None:
    if not text:
        return None
    cyrillic_chars = sum(1 for ch in text if "а" <= ch.lower() <= "я")
    if cyrillic_chars > len(text) * 0.3:
        return None
    for attempt in range(1, 4):
        try:
            translated = GoogleTranslator(source="auto", target="ru").translate(text)
            if translated and translated.strip().lower() != text.strip().lower():
                return translated
            return None
        except Exception as exc:
            print(f"Попытка перевода {attempt}/3 не удалась: {exc}")
            if attempt < 3:
                time.sleep(5 * attempt)
    return None


def send_to_telegram(title: str, link: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    translation = translate_to_russian(title)
    if translation:
        text = f"{title}\nПеревод: {translation}\n{link}"
    else:
        text = f"{title}\n{link}"
    try:
        resp = requests.post(
            url,
            data={
                "chat_id": CHANNEL_ID,
                "text": text,
                "disable_web_page_preview": False,
            },
            timeout=15,
        )
        if resp.status_code != 200:
            print(f"Ошибка отправки в Telegram: {resp.status_code} {resp.text}")
            return False
        return True
    except requests.RequestException as exc:
        print(f"Сетевая ошибка при отправке в Telegram: {exc}")
        return False


def clean_html(raw: str) -> str:
    return re.sub("<[^<]+?>", "", raw or "").strip()


def check_feeds() -> None:
    seen_data = load_seen()
    seen_links = set(seen_data["links"])
    recent_titles = list(seen_data["titles"])
    new_items_sent = 0
    duplicates_skipped = 0

    for feed_url in ALL_FEEDS:
        print(f"Проверяю ленту: {feed_url}")
        try:
            feed = feedparser.parse(feed_url)
        except Exception as exc:
            print(f"Не удалось прочитать ленту {feed_url}: {exc}")
            continue

        for entry in feed.entries:
            link = entry.get("link", "")
            title = clean_html(entry.get("title", ""))
            summary = clean_html(entry.get("summary", ""))

            if not link or link in seen_links:
                continue

            if feed_url in PRE_FILTERED_FEEDS or is_relevant(title, summary):
                if is_duplicate_title(title, recent_titles):
                    duplicates_skipped += 1
                    print(f"Пропущено как дубль: {title}")
                else:
                    sent = send_to_telegram(title, link)
                    if sent:
                        new_items_sent += 1
                        recent_titles.append(normalize_title(title))
                        print(f"Отправлено: {title}")
                        time.sleep(1)

            seen_links.add(link)

    save_seen(seen_links, recent_titles)
    print(
        f"Готово. Новых новостей отправлено: {new_items_sent}, "
        f"пропущено как дубли: {duplicates_skipped}"
    )


if __name__ == "__main__":
    check_feeds()
