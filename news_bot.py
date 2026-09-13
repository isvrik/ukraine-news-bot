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
from pathlib import Path
from urllib.parse import quote

import feedparser
import requests
from deep_translator import GoogleTranslator

# ========== НАСТРОЙКИ ==========

# Токен бота и канал берутся из переменных окружения (секретов GitHub),
# а не хранятся в коде напрямую — это безопаснее.
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CHANNEL_ID = os.environ.get("CHANNEL_ID", "@news_isv_bot")

# Ключевые слова, по которым фильтруются новости.
# Новость публикуется, если хотя бы одно слово из любой группы найдено
# И хотя бы одно слово, связанное с "переговоры" ИЛИ явно про Украину-мир.
KEYWORDS = [
    "переговоры по украине",
    "переговоры украина",
    "мирный план украина",
    "мирное соглашение украина",
    "прекращение огня украина",
    "перемирие украина",
    "украина сша переговоры",
    "зеленский трамп переговоры",
    "путин переговоры украина",
    "кушнер украина",
    "вэнс украина",
    "рубио украина",
    "ukraine peace talks",
    "ukraine ceasefire",
    "ukraine negotiations",
    "ukraine peace deal",
]

# Поисковый запрос для Google News на русском (агрегирует много источников сразу)
GOOGLE_NEWS_QUERY_RU = "переговоры Украина"
GOOGLE_NEWS_RSS_RU = (
    f"https://news.google.com/rss/search?q={quote(GOOGLE_NEWS_QUERY_RU)}"
    "&hl=ru&gl=RU&ceid=RU:ru"
)

# Поисковый запрос для Google News на английском (Reuters, AP, BBC, Bloomberg и т.д.)
# Без кавычек — совпадение по словам, а не только по точной фразе (шире охват)
GOOGLE_NEWS_QUERY_EN = (
    "Ukraine negotiations OR Ukraine peace talks OR Ukraine ceasefire "
    "OR Ukraine peace deal OR Zelensky Trump talks OR Putin Ukraine deal "
    "OR Ukraine truce OR Witkoff Ukraine OR Kushner Ukraine "
    "OR Vance Ukraine OR Rubio Ukraine"
)
GOOGLE_NEWS_RSS_EN = (
    f"https://news.google.com/rss/search?q={quote(GOOGLE_NEWS_QUERY_EN)}"
    "&hl=en&gl=US&ceid=US:en"
)

# Отдельный прицельный поиск конкретно по Bloomberg через Google News
# (у Bloomberg нет стабильной публичной общей RSS-ленты, поэтому используем
# фильтр site: — это надёжно возвращает именно статьи с bloomberg.com)
GOOGLE_NEWS_QUERY_BLOOMBERG = (
    "(Ukraine negotiations OR Ukraine peace OR Ukraine ceasefire OR Ukraine truce) "
    "site:bloomberg.com"
)
GOOGLE_NEWS_RSS_BLOOMBERG = (
    f"https://news.google.com/rss/search?q={quote(GOOGLE_NEWS_QUERY_BLOOMBERG)}"
    "&hl=en&gl=US&ceid=US:en"
)

# Ленты, которые уже отфильтрованы самим поисковым запросом Google News —
# для них не нужна повторная проверка по ключевым словам
PRE_FILTERED_FEEDS = {
    GOOGLE_NEWS_RSS_RU,
    GOOGLE_NEWS_RSS_EN,
    GOOGLE_NEWS_RSS_BLOOMBERG,
}

# Дополнительные прямые RSS-ленты (можно добавлять свои)
DIRECT_FEEDS = [
    "https://tass.ru/rss/v2.xml",
    "https://ria.ru/export/rss2/archive/index.xml",
]

ALL_FEEDS = [
    GOOGLE_NEWS_RSS_RU,
    GOOGLE_NEWS_RSS_EN,
    GOOGLE_NEWS_RSS_BLOOMBERG,
] + DIRECT_FEEDS

SEEN_FILE = Path(__file__).parent / "seen_links.json"

# ========== ЛОГИКА ==========


def load_seen() -> set:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()


def save_seen(seen: set) -> None:
    # Храним не более 2000 последних ссылок, чтобы файл не рос бесконечно
    trimmed = list(seen)[-2000:]
    SEEN_FILE.write_text(
        json.dumps(trimmed, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def is_relevant(title: str, summary: str) -> bool:
    text = f"{title} {summary}".lower()
    return any(kw.lower() in text for kw in KEYWORDS)


def translate_to_russian(text: str) -> str | None:
    """
    Переводит текст на русский, если он на другом языке.
    Возвращает None, если перевод не удался или язык уже русский
    (чтобы не отправлять дубли для русскоязычных заголовков).
    """
    if not text:
        return None
    # Простая проверка: если в тексте уже много кириллицы, перевод не нужен
    cyrillic_chars = sum(1 for ch in text if "а" <= ch.lower() <= "я")
    if cyrillic_chars > len(text) * 0.3:
        return None
    try:
        translated = GoogleTranslator(source="auto", target="ru").translate(text)
        if translated and translated.strip().lower() != text.strip().lower():
            return translated
    except Exception as exc:
        print(f"Не удалось перевести заголовок: {exc}")
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
    seen = load_seen()
    new_items_sent = 0

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

            if not link or link in seen:
                continue

            # Для Google News ссылки уже отфильтрованы поисковым запросом,
            # но для прямых лент (ТАСС/РИА) нужна собственная фильтрация.
            if feed_url in PRE_FILTERED_FEEDS or is_relevant(title, summary):
                sent = send_to_telegram(title, link)
                if sent:
                    new_items_sent += 1
                    print(f"Отправлено: {title}")
                    time.sleep(1)  # небольшая пауза, чтобы не спамить API

            seen.add(link)

    save_seen(seen)
    print(f"Готово. Новых новостей отправлено: {new_items_sent}")


if __name__ == "__main__":
    check_feeds()
