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
import calendar
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import quote, urlsplit

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
    "мирный план украина",
    "мирное соглашение украина",
    "прекращение огня украина",
    "перемирие украина",
    "зеленский трамп переговоры",
    "путин переговоры украина",
    "ukraine peace talks",
    "ukraine ceasefire",
]

# Поисковый запрос для Google News на русском (агрегирует много источников сразу)
# Сужено: требуется слово "Украина" ВМЕСТЕ с конкретной темой переговоров/мира,
# а не любое упоминание слова "переговоры" рядом с Украиной
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
# Сужено: каждая ветка OR отдельно требует слово "Ukraine" ВМЕСТЕ с конкретным
# словом/именем — вместо общей группы условий, которую Google иногда трактует
# нестрого и пропускает не связанные с Украиной новости (например, про
# автомобильный рынок из-за одного лишь слова "negotiations").
# Источники сужены до тех, кто обычно первым сообщает о дипломатических
# новостях (Reuters, AP, Axios) — остальные СМИ чаще просто перепечатывают их.
GOOGLE_NEWS_QUERY_EN = (
    '((Trump Ukraine) OR (Witkoff Ukraine) OR (Kushner Ukraine) OR '
    '(Ukraine "peace talks") OR (Ukraine ceasefire) OR (Ukraine "peace deal")) '
    "(site:reuters.com OR site:apnews.com OR site:axios.com)"
)
GOOGLE_NEWS_RSS_EN = (
    f"https://news.google.com/rss/search?q={quote(GOOGLE_NEWS_QUERY_EN)}"
    "&hl=en&gl=US&ceid=US:en"
)

# Отдельный прицельный поиск конкретно по Bloomberg через Google News
# (у Bloomberg нет стабильной публичной общей RSS-ленты, поэтому используем
# фильтр site: — это надёжно возвращает именно статьи с bloomberg.com)
GOOGLE_NEWS_QUERY_BLOOMBERG = (
    '((Trump Ukraine) OR (Witkoff Ukraine) OR (Kushner Ukraine) OR '
    '(Ukraine "peace talks") OR (Ukraine ceasefire) OR (Ukraine "peace deal")) '
    "site:bloomberg.com"
)
GOOGLE_NEWS_RSS_BLOOMBERG = (
    f"https://news.google.com/rss/search?q={quote(GOOGLE_NEWS_QUERY_BLOOMBERG)}"
    "&hl=en&gl=US&ceid=US:en"
)

# Иностранные ленты (для них действует общий лимит числа новостей за один
# запуск — см. MAX_FOREIGN_ITEMS_PER_RUN ниже). Русскоязычные ленты в лимит
# не входят и отправляются в текущем объёме.
FOREIGN_FEEDS = {GOOGLE_NEWS_RSS_EN, GOOGLE_NEWS_RSS_BLOOMBERG}

# Не более скольких иностранных новостей отправлять за один запуск скрипта
# (примерно раз в 5 минут). Из всех подходящих кандидатов за этот период
# выбираются лучшие по совокупному рейтингу (см. ниже), остальные отбрасываются.
MAX_FOREIGN_ITEMS_PER_RUN = 10

# --- Рейтинг источников: насколько оперативно каждый обычно публикует
# новости такого рода. Чем выше число — тем больше приоритет при равной
# релевантности и свежести. Значения условные, основаны на общей репутации
# информагентств как "первыми сообщающих" — можно скорректировать вручную.
SOURCE_PRIORITY = {
    "reuters.com": 5,
    "apnews.com": 5,
    "axios.com": 4,
    "bloomberg.com": 4,
}
DEFAULT_SOURCE_PRIORITY = 2

# --- Слова/имена, по которым считается "степень релевантности" заголовка:
# чем больше таких слов встречается — тем более значимой считается новость
# (например, заголовок с "Trump" и "ceasefire" одновременно важнее, чем
# просто "Ukraine talks").
RELEVANCE_TERMS = [
    "trump", "witkoff", "kushner", "vance", "rubio", "zelensky", "putin",
    "peace talks", "peace deal", "ceasefire", "truce", "negotiations",
]

# Веса при расчёте итогового рейтинга новости (можно настраивать баланс).
# Релевантность важнее репутации источника: точное попадание в тему должно
# перевешивать то, что новость просто от "быстрого" агентства.
WEIGHT_SOURCE = 2
WEIGHT_RELEVANCE = 3
WEIGHT_RECENCY = 1
# Сколько минут считается "полностью свежим" (максимальный балл за скорость)
RECENCY_FULL_SCORE_MINUTES = 5

# Для каждого из этих источников гарантируется как минимум одно место в
# топ-10 (если за прошедшие 5 минут у него была хоть одна подходящая
# новость) — чтобы в канале были разные точки зрения, а не только те
# источники, что обычно выигрывают по чистому рейтингу.
DIVERSITY_GUARANTEED_DOMAINS = ["axios.com", "bloomberg.com"]

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

# Сколько последних отправленных заголовков хранить для проверки на похожесть
# (чтобы не присылать одну и ту же новость дважды от разных источников)
RECENT_TITLES_LIMIT = 150

# Порог похожести заголовков (0.0-1.0). Чем выше — тем более похожими должны
# быть заголовки, чтобы считаться дублем. 0.6 ловит переформулировки одной
# и той же новости разными изданиями.
DUPLICATE_SIMILARITY_THRESHOLD = 0.6

# ========== ЛОГИКА ==========


def load_seen() -> dict:
    """
    Возвращает {"links": [...], "titles": [...]}.
    Поддерживает старый формат файла (просто список ссылок) для плавного
    перехода на новую структуру.
    """
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
    # Храним не более 2000 последних ссылок и последние RECENT_TITLES_LIMIT
    # нормализованных заголовков, чтобы файл не рос бесконечно
    data = {
        "links": list(seen_links)[-2000:],
        "titles": recent_titles[-RECENT_TITLES_LIMIT:],
    }
    SEEN_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def normalize_title(title: str) -> str:
    """
    Приводит заголовок к простому виду для сравнения на похожесть:
    убирает приписку источника в конце (Google News добавляет " - Название СМИ"),
    убирает пунктуацию и лишние пробелы, переводит в нижний регистр.
    """
    # Google News обычно добавляет " - Источник" в самом конце заголовка
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
    """
    Переводит текст на русский, если он на другом языке.
    Возвращает None, если перевод не удался или язык уже русский
    (чтобы не отправлять дубли для русскоязычных заголовков).
    Делает до 3 попыток с паузой, если сервис перевода временно
    отвечает ошибкой "слишком много запросов".
    """
    if not text:
        return None
    # Простая проверка: если в тексте уже много кириллицы, перевод не нужен
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
                time.sleep(5 * attempt)  # 5с, затем 10с перед повтором
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


def get_published_timestamp(entry) -> float:
    """
    Возвращает время публикации записи как unix-timestamp для оценки
    свежести. Если время неизвестно — возвращает 0 (считается самым старым).
    """
    for field in ("published_parsed", "updated_parsed"):
        value = entry.get(field)
        if value:
            try:
                return calendar.timegm(value)
            except Exception:
                pass
    return 0.0


def get_source_domain(link: str) -> str:
    try:
        netloc = urlsplit(link).netloc.lower()
        return netloc[4:] if netloc.startswith("www.") else netloc
    except Exception:
        return ""


def score_candidate(title: str, link: str, timestamp: float, now: float) -> float:
    """
    Считает совокупный рейтинг новости: приоритет источника + релевантность
    заголовка теме + свежесть публикации. Чем выше итоговое число — тем
    выше новость в списке "лучших" за этот запуск.
    """
    domain = get_source_domain(link)
    source_score = SOURCE_PRIORITY.get(domain, DEFAULT_SOURCE_PRIORITY)

    title_lower = title.lower()
    relevance_score = sum(1 for term in RELEVANCE_TERMS if term in title_lower)

    if timestamp > 0:
        minutes_ago = max(0.0, (now - timestamp) / 60)
        recency_score = max(0.0, RECENCY_FULL_SCORE_MINUTES - minutes_ago)
    else:
        recency_score = 0.0

    return (
        source_score * WEIGHT_SOURCE
        + relevance_score * WEIGHT_RELEVANCE
        + recency_score * WEIGHT_RECENCY
    )


def check_feeds() -> None:
    seen_data = load_seen()
    seen_links = set(seen_data["links"])
    recent_titles = list(seen_data["titles"])  # уже нормализованные заголовки
    new_items_sent = 0
    duplicates_skipped = 0
    foreign_dropped_over_cap = 0

    # --- Русскоязычные и прямые ленты (ТАСС/РИА) — без ограничения по числу,
    # отправляются как и раньше, в порядке появления в ленте.
    ru_and_direct_feeds = [f for f in ALL_FEEDS if f not in FOREIGN_FEEDS]
    for feed_url in ru_and_direct_feeds:
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
            seen_links.add(link)

            if not (feed_url in PRE_FILTERED_FEEDS or is_relevant(title, summary)):
                continue

            if is_duplicate_title(title, recent_titles):
                duplicates_skipped += 1
                print(f"Пропущено как дубль: {title}")
                continue

            sent = send_to_telegram(title, link)
            if sent:
                new_items_sent += 1
                recent_titles.append(normalize_title(title))
                print(f"Отправлено: {title}")
                time.sleep(1)

    # --- Иностранные ленты (Reuters/AP/Axios через Google News + Bloomberg).
    # Сначала собираем ВСЕХ подходящих кандидатов из всех иностранных лент,
    # затем сортируем по времени публикации и отправляем только самые
    # свежие MAX_FOREIGN_ITEMS_PER_RUN — остальные отбрасываются полностью
    # (а не откладываются на потом, чтобы не присылать устаревшее).
    foreign_candidates = []
    for feed_url in FOREIGN_FEEDS:
        print(f"Проверяю ленту: {feed_url}")
        try:
            feed = feedparser.parse(feed_url)
        except Exception as exc:
            print(f"Не удалось прочитать ленту {feed_url}: {exc}")
            continue

        for entry in feed.entries:
            link = entry.get("link", "")
            title = clean_html(entry.get("title", ""))

            if not link or link in seen_links:
                continue
            seen_links.add(link)  # решение принимается сейчас и окончательно

            if is_duplicate_title(title, recent_titles):
                duplicates_skipped += 1
                print(f"Пропущено как дубль: {title}")
                continue

            foreign_candidates.append(
                {
                    "title": title,
                    "link": link,
                    "timestamp": get_published_timestamp(entry),
                }
            )

    # Считаем итоговый рейтинг для каждого кандидата
    now = time.time()
    for item in foreign_candidates:
        item["domain"] = get_source_domain(item["link"])
        item["score"] = score_candidate(item["title"], item["link"], item["timestamp"], now)
    foreign_candidates.sort(key=lambda item: item["score"], reverse=True)

    # Отбор с гарантией разнообразия: сначала резервируем по одному месту
    # для источников из DIVERSITY_GUARANTEED_DOMAINS (берём их лучшую по
    # рейтингу новость за этот запуск, если она есть), затем добираем
    # оставшиеся места просто по убыванию общего рейтинга.
    selected = []
    selected_links = set()

    for domain in DIVERSITY_GUARANTEED_DOMAINS:
        if len(selected) >= MAX_FOREIGN_ITEMS_PER_RUN:
            break
        best_for_domain = next(
            (c for c in foreign_candidates if c["domain"] == domain), None
        )
        if best_for_domain and best_for_domain["link"] not in selected_links:
            selected.append(best_for_domain)
            selected_links.add(best_for_domain["link"])
            print(
                f"Зарезервировано место для {domain} (рейтинг "
                f"{best_for_domain['score']:.1f}): {best_for_domain['title']}"
            )

    for item in foreign_candidates:
        if len(selected) >= MAX_FOREIGN_ITEMS_PER_RUN:
            break
        if item["link"] in selected_links:
            continue
        selected.append(item)
        selected_links.add(item["link"])

    dropped = [c for c in foreign_candidates if c["link"] not in selected_links]
    foreign_dropped_over_cap = len(dropped)
    for item in dropped:
        print(
            f"Отброшено (рейтинг {item['score']:.1f}, не вошло в топ-"
            f"{MAX_FOREIGN_ITEMS_PER_RUN}): {item['title']}"
        )

    # Отправляем в порядке убывания рейтинга
    selected.sort(key=lambda item: item["score"], reverse=True)
    for item in selected:
        sent = send_to_telegram(item["title"], item["link"])
        if sent:
            new_items_sent += 1
            recent_titles.append(normalize_title(item["title"]))
            print(f"Отправлено: {item['title']}")
            time.sleep(1)

    save_seen(seen_links, recent_titles)
    print(
        f"Готово. Новых новостей отправлено: {new_items_sent}, "
        f"пропущено как дубли: {duplicates_skipped}, "
        f"отброшено иностранных сверх лимита: {foreign_dropped_over_cap}"
    )


if __name__ == "__main__":
    check_feeds()
