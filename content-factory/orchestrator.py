#!/usr/bin/env python3
"""
content-factory orchestrator — HTML-режим.

Генерирует ARTICLES_PER_DAY статей, подставляет в article_template.html,
пушит HTML в distill-landing через SITE_REPO_TOKEN, шлёт email-репорт.

Запуск: python orchestrator.py
"""
from __future__ import annotations

import csv
import json
import logging
import os
import re
import requests
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from anthropic import Anthropic
from dotenv import load_dotenv

from send_email import send_report

# ── Настройка ─────────────────────────────────────────────────────────
load_dotenv()

ROOT = Path(__file__).parent.resolve()
PROMPTS = ROOT / "prompts"
DATA = ROOT / "data"
LOGS = ROOT / "logs"
LOGS.mkdir(exist_ok=True)
DATA.mkdir(exist_ok=True)

BACKLOG_PATH = DATA / "topics_backlog.csv"
USED_PATH = DATA / "topics_used.csv"
TEMPLATE_PATH = ROOT / "article_template.html"

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
ARTICLES_PER_DAY = int(os.environ.get("ARTICLES_PER_DAY", "5"))
ENABLE_EDITOR_PASS = os.environ.get("ENABLE_EDITOR_PASS", "true").lower() == "true"

USE_BATCH = os.environ.get("USE_BATCH", "true").lower() == "true"
BATCH_POLL_INTERVAL = int(os.environ.get("BATCH_POLL_INTERVAL", "20"))
BATCH_MAX_WAIT = int(os.environ.get("BATCH_MAX_WAIT", "5400"))

IS_GITHUB_ACTIONS = os.environ.get("GITHUB_ACTIONS", "").lower() == "true"

# ── Site repo (distill-landing) ────────────────────────────────────────
SITE_REPO_TOKEN = os.environ.get("SITE_REPO_TOKEN", "")
SITE_REPO_URL = os.environ.get("SITE_REPO_URL", "")
SITE_REPO_BRANCH = os.environ.get("SITE_REPO_BRANCH", "main")
SITE_BLOG_DIR = os.environ.get("SITE_BLOG_DIR", "blog")

# inject token into HTTPS URL (без записи в лог)
if SITE_REPO_TOKEN and SITE_REPO_URL.startswith("https://"):
    _SITE_REPO_URL_AUTH = SITE_REPO_URL.replace(
        "https://", f"https://x-access-token:{SITE_REPO_TOKEN}@", 1
    )
else:
    _SITE_REPO_URL_AUTH = SITE_REPO_URL

if IS_GITHUB_ACTIONS:
    # клонируем landing в /tmp, не в GITHUB_WORKSPACE (там distill-seo)
    SITE_REPO_PATH = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "distill-landing"
else:
    SITE_REPO_PATH = Path(os.environ["SITE_REPO_PATH"])

# ── CTA (переопределяется через env) ──────────────────────────────────
CTA_HEADING = os.environ.get("CTA_HEADING", "Получайте разборы раз в неделю")
CTA_TEXT = os.environ.get(
    "CTA_TEXT",
    "Каждую неделю — одна большая идея из мира продуктивности, мышления и принятия решений. "
    "Без воды, только концентрат.",
)
CTA_BUTTON = os.environ.get("CTA_BUTTON", "Подписаться на дайджест")
CTA_HREF = os.environ.get("CTA_HREF", "/newsletter/")

# логирование
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOGS / f"run-{datetime.now():%Y-%m-%d}.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("orchestrator")

client = Anthropic(api_key=ANTHROPIC_API_KEY)


# ── Модели данных ─────────────────────────────────────────────────────
@dataclass
class Topic:
    topic: str
    primary_keyword: str
    secondary_keywords: str
    intent: str
    category: str
    wordstat_frequency: Optional[int]
    competitor_refs: str
    priority: int

    @classmethod
    def from_row(cls, row: dict) -> "Topic":
        freq = row.get("wordstat_frequency", "").strip()
        return cls(
            topic=row["topic"].strip(),
            primary_keyword=row["primary_keyword"].strip(),
            secondary_keywords=row.get("secondary_keywords", "").strip(),
            intent=row.get("intent", "informational").strip() or "informational",
            category=row.get("category", "thinking").strip() or "thinking",
            wordstat_frequency=int(freq) if freq.isdigit() else None,
            competitor_refs=row.get("competitor_refs", "").strip(),
            priority=int(row.get("priority", "0").strip() or "0"),
        )


@dataclass
class ArticleResult:
    topic: Topic
    slug: str = ""
    title: str = ""
    file_path: Optional[Path] = None
    extra_files: list[Path] = field(default_factory=list)  # blog/index.html, sitemap.xml
    status: str = "pending"   # pending | published | failed
    error: Optional[str] = None
    elapsed_sec: float = 0.0


@dataclass
class RunReport:
    started_at: datetime
    finished_at: Optional[datetime] = None
    results: list[ArticleResult] = field(default_factory=list)
    commit_sha: Optional[str] = None


# ── CSV ───────────────────────────────────────────────────────────────
def load_backlog() -> list[Topic]:
    if not BACKLOG_PATH.exists():
        log.error("Не найден %s", BACKLOG_PATH)
        return []
    with BACKLOG_PATH.open(encoding="utf-8-sig") as f:
        return [Topic.from_row(r) for r in csv.DictReader(f) if r.get("topic", "").strip()]


def load_used_slugs() -> set[str]:
    if not USED_PATH.exists():
        return set()
    used: set[str] = set()
    with USED_PATH.open(encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r.get("slug"):
                used.add(r["slug"].strip())
            if r.get("primary_keyword"):
                used.add(r["primary_keyword"].strip().lower())
    return used


def append_used(rows: list[dict]) -> None:
    is_new = not USED_PATH.exists()
    with USED_PATH.open("a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["date", "slug", "title", "primary_keyword", "category", "status", "url"],
        )
        if is_new:
            writer.writeheader()
        for r in rows:
            writer.writerow(r)


def remove_topics_from_backlog(consumed: list[Topic]) -> None:
    consumed_keys = {(t.topic, t.primary_keyword) for t in consumed}
    if not BACKLOG_PATH.exists():
        return
    with BACKLOG_PATH.open(encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        kept = [
            r for r in reader
            if (r.get("topic", "").strip(), r.get("primary_keyword", "").strip()) not in consumed_keys
        ]
    with BACKLOG_PATH.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(kept)


def pick_topics(n: int) -> list[Topic]:
    used = load_used_slugs()
    candidates = [
        t for t in load_backlog()
        if t.primary_keyword.strip().lower() not in used
    ]
    candidates.sort(key=lambda t: (-t.priority, -(t.wordstat_frequency or 0)))
    return candidates[:n]


# ── HTML-шаблон ───────────────────────────────────────────────────────
_MONTHS_RU = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


_CATEGORY_RU: dict[str, str] = {
    "thinking":    "мышление",
    "productivity": "продуктивность",
    "philosophy":  "философия",
}


def _localize_category(cat: str) -> str:
    return _CATEGORY_RU.get(cat.strip().lower(), cat)


def _format_date_human(date_str: str) -> str:
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return f"{dt.day} {_MONTHS_RU[dt.month - 1]} {dt.year}"


def _estimate_read_time(lead: str, body_html: str) -> int:
    text = re.sub(r"<[^>]+>", " ", lead + " " + body_html)
    words = len(text.split())
    return max(1, round(words / 200))


def fill_template(brief: dict, lead: str, body_html: str, publish_date: str) -> str:
    """Подставляет плейсхолдеры в article_template.html.

    Плейсхолдеры соответствуют файлу content-factory/article_template.html дословно.
    Не генерировать HTML самостоятельно — брать шаблон как есть.
    """
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    slug = brief.get("slug", "")
    title = brief.get("title", "")
    h1 = brief.get("h1", title)       # заголовок H1 на странице, может длиннее title
    description = brief.get("description", "")
    keywords = ", ".join(brief.get("keywords", []))
    tg_param = os.environ.get("TG_START_PARAM_PREFIX", "") + slug

    replacements = {
        # <head>
        "{{SEO_TITLE}}":        title,           # <title> тег — ≤60 символов
        "{{TITLE}}":            h1,              # <h1> на странице + schema.org headline
        "{{META_DESCRIPTION}}": description,
        "{{META_KEYWORDS}}":    keywords,
        "{{OG_TITLE}}":         title,
        "{{OG_DESCRIPTION}}":   description,
        "{{OG_IMAGE}}":         os.environ.get("OG_IMAGE_DEFAULT", ""),
        "{{SLUG}}":             slug,
        "{{DATE_ISO}}":         publish_date,
        # nav + article-cta кнопка
        "{{TG_START_PARAM}}":   tg_param,
        # article header
        "{{TAG}}":              _localize_category(brief.get("category", "")),
        "{{DATE_HUMAN}}":       _format_date_human(publish_date),
        "{{READ_MIN}}":         str(_estimate_read_time(lead, body_html)),
        # article body
        "{{LEAD}}":             lead,
        "{{BODY_HTML}}":        body_html,
        # article-cta
        "{{CTA_TITLE}}":        CTA_HEADING,
        "{{CTA_TEXT}}":         CTA_TEXT,
    }
    result = template
    for k, v in replacements.items():
        result = result.replace(k, v)
    return result


# ── Промпты и вызов Claude ────────────────────────────────────────────
def read_prompt(name: str) -> str:
    return (PROMPTS / name).read_text(encoding="utf-8")


def call_claude(system: str, user: str, max_tokens: int = 8000) -> str:
    last_err = None
    for attempt in range(3):
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            return resp.content[0].text
        except Exception as e:  # noqa: BLE001
            last_err = e
            log.warning("Claude call failed (attempt %d/3): %s", attempt + 1, e)
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Claude call failed after 3 attempts: {last_err}")


def brief_user(topic: Topic, publish_date: str) -> str:
    return (
        f"TOPIC: {topic.topic}\n"
        f"PRIMARY_KEYWORD: {topic.primary_keyword}\n"
        f"SECONDARY_KEYWORDS: {topic.secondary_keywords}\n"
        f"INTENT: {topic.intent}\n"
        f"CATEGORY: {topic.category}\n"
        f"WORDSTAT_FREQUENCY: {topic.wordstat_frequency or 'null'}\n"
        f"COMPETITOR_REFS: {topic.competitor_refs}\n"
        f"PUBLISH_DATE: {publish_date}\n"
    )


def _truncate(text: str, max_len: int) -> str:
    """Обрезает текст до max_len символов на границе слова, добавляя «…»."""
    if len(text) <= max_len:
        return text
    cut = text[:max_len - 1]
    space = cut.rfind(" ")
    if space > max_len // 2:
        cut = cut[:space]
    return cut + "…"


def _enforce_brief_limits(brief: dict) -> dict:
    """Гарантирует title ≤ 60 и description ≤ 160; пустые поля — ошибка."""
    title = (brief.get("title") or "").strip()
    desc = (brief.get("description") or "").strip()
    if not title:
        raise ValueError("brief: пустой title — нельзя публиковать")
    if not desc:
        raise ValueError("brief: пустое description — нельзя публиковать")
    if len(title) > 60:
        title = _truncate(title, 60)
        brief["title"] = title
        log.warning("brief: title обрезан до %d символов → %r", len(title), title)
    if len(desc) > 160:
        desc = _truncate(desc, 160)
        brief["description"] = desc
        log.warning("brief: description обрезана до %d символов → %r", len(desc), desc)
    return brief


def parse_brief(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```\s*$", "", raw)
    return _enforce_brief_limits(json.loads(raw))


def parse_article(raw: str) -> dict:
    """Парсит JSON {lead, body_html} от article writer / editor."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```\s*$", "", raw)
    data = json.loads(raw)
    if "lead" not in data or "body_html" not in data:
        raise ValueError(f"Ожидались ключи lead и body_html, получено: {list(data.keys())}")
    return data


def article_user(brief: dict, publish_date: str) -> str:
    return (
        f"PUBLISH_DATE: {publish_date}\n"
        f"BRIEF (JSON):\n{json.dumps(brief, ensure_ascii=False, indent=2)}\n"
    )


def edit_user(article: dict) -> str:
    return f"ARTICLE JSON:\n{json.dumps(article, ensure_ascii=False, indent=2)}"


def make_seo_brief(topic: Topic, publish_date: str) -> dict:
    raw = call_claude(read_prompt("01_seo_brief.md"), brief_user(topic, publish_date), max_tokens=4000)
    return parse_brief(raw)


def make_article_html(brief: dict, publish_date: str) -> dict:
    """Возвращает {lead: str, body_html: str}."""
    raw = call_claude(read_prompt("02_article_writer.md"), article_user(brief, publish_date), max_tokens=8000)
    return parse_article(raw.strip())


def edit_article_html(article: dict) -> dict:
    """Редактура и факт-чек. Получает и возвращает {lead, body_html}."""
    raw = call_claude(read_prompt("03_editor_factcheck.md"), edit_user(article), max_tokens=8000)
    try:
        return parse_article(raw.strip())
    except Exception:
        log.warning("Редактор вернул некорректный JSON — беру нередактированную версию")
        return article


# ── Batch API ─────────────────────────────────────────────────────────
def run_stage_batch(label: str, system_text: str, jobs: list[tuple[str, str]],
                    max_tokens: int) -> dict[str, Optional[str]]:
    if not jobs:
        return {}
    system_block = [{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}]
    requests = [
        {
            "custom_id": cid,
            "params": {
                "model": MODEL,
                "max_tokens": max_tokens,
                "system": system_block,
                "messages": [{"role": "user", "content": user}],
            },
        }
        for cid, user in jobs
    ]
    log.info("Batch[%s]: отправляю %d запросов...", label, len(requests))
    batch = client.messages.batches.create(requests=requests)

    waited = 0
    while True:
        b = client.messages.batches.retrieve(batch.id)
        if b.processing_status == "ended":
            break
        if waited >= BATCH_MAX_WAIT:
            raise RuntimeError(
                f"Batch[{label}] не завершился за {BATCH_MAX_WAIT}s (status={b.processing_status})"
            )
        time.sleep(BATCH_POLL_INTERVAL)
        waited += BATCH_POLL_INTERVAL

    out: dict[str, Optional[str]] = {}
    for r in client.messages.batches.results(batch.id):
        if r.result.type == "succeeded":
            msg = r.result.message
            out[r.custom_id] = next((blk.text for blk in msg.content if blk.type == "text"), "")
        else:
            out[r.custom_id] = None
            log.warning("Batch[%s]: %s → %s", label, r.custom_id, r.result.type)
    ok = sum(1 for v in out.values() if v)
    log.info("Batch[%s]: готово — %d/%d успешно", label, ok, len(requests))
    return out


# ── Git операции ──────────────────────────────────────────────────────
def _run(cmd: list[str], cwd: Optional[Path] = None, mask: str = "") -> str:
    display = " ".join(a.replace(mask, "***") if mask and mask in a else a for a in cmd)
    log.info("$ %s%s", display, f"  (cwd={cwd})" if cwd else "")
    result = subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)
    if result.stdout:
        log.info(result.stdout.strip())
    return result.stdout.strip()


def ensure_site_repo() -> None:
    """Клонирует или обновляет distill-landing по SITE_REPO_TOKEN."""
    if not _SITE_REPO_URL_AUTH:
        raise ValueError("SITE_REPO_URL и SITE_REPO_TOKEN не заданы — нельзя пушить статьи")
    if not SITE_REPO_PATH.exists():
        SITE_REPO_PATH.parent.mkdir(parents=True, exist_ok=True)
        _run(
            ["git", "clone", "--branch", SITE_REPO_BRANCH, "--depth", "1",
             _SITE_REPO_URL_AUTH, str(SITE_REPO_PATH)],
            mask=SITE_REPO_TOKEN,
        )
    else:
        _run(["git", "fetch", "origin"], cwd=SITE_REPO_PATH)
        _run(["git", "checkout", SITE_REPO_BRANCH], cwd=SITE_REPO_PATH)
        _run(["git", "reset", "--hard", f"origin/{SITE_REPO_BRANCH}"], cwd=SITE_REPO_PATH)


def commit_and_push(written_files: list[Path], date_str: str) -> Optional[str]:
    """Коммит HTML-статей + blog/index.html + sitemap.xml в distill-landing и пуш."""
    if not written_files:
        return None
    # Дедуплицируем: index.html и sitemap.xml могут попасть по разу на каждую статью
    unique_files = list(dict.fromkeys(written_files))
    paths = [str(p.relative_to(SITE_REPO_PATH)) for p in unique_files]
    _run(["git", "add", "--", *paths], cwd=SITE_REPO_PATH)
    diff = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=SITE_REPO_PATH, capture_output=True, text=True, check=True,
    )
    if not diff.stdout.strip():
        log.warning("Нет изменений для коммита в distill-landing")
        return None
    msg = f"content-factory: {len(written_files)} статей за {date_str}"
    _run(
        ["git", "-c", "user.email=bot@example.com", "-c", "user.name=content-factory",
         "commit", "-m", msg],
        cwd=SITE_REPO_PATH,
    )
    _run(["git", "push", "origin", SITE_REPO_BRANCH], cwd=SITE_REPO_PATH)
    return _run(["git", "rev-parse", "HEAD"], cwd=SITE_REPO_PATH)


def commit_factory_csv(date_str: str) -> None:
    """В GitHub Actions — коммитит обновлённые CSV обратно в distill-seo."""
    if not IS_GITHUB_ACTIONS:
        return
    workspace = Path(os.environ.get("GITHUB_WORKSPACE", "."))
    cf_data = "content-factory/data"
    for csv_name in ("topics_backlog.csv", "topics_used.csv"):
        rel = f"{cf_data}/{csv_name}"
        if (workspace / rel).exists():
            subprocess.run(["git", "add", rel], cwd=workspace, check=False, capture_output=True)
    diff = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=workspace, capture_output=True, text=True, check=True,
    )
    if not diff.stdout.strip():
        return
    _run(
        ["git", "-c", "user.email=bot@example.com", "-c", "user.name=content-factory",
         "commit", "-m", f"content-factory: update topics csv {date_str}"],
        cwd=workspace,
    )
    _run(["git", "push", "origin", "main"], cwd=workspace)


# ── Обновление blog/index.html и sitemap.xml ─────────────────────────
def _update_blog_index(blog_dir: Path, brief: dict, lead: str, body_html: str,
                       publish_date: str) -> Optional[Path]:
    """Вставить карточку новой статьи первой в <div class="blog-grid">."""
    index_path = blog_dir / "index.html"
    if not index_path.exists():
        log.warning("blog/index.html не найден в %s — пропускаю карточку", blog_dir)
        return None
    slug = brief.get("slug", "")
    title = brief.get("title", "")
    description = brief.get("description", "")
    category = brief.get("category", "")
    category_ru = _localize_category(category)
    read_min = _estimate_read_time(lead, body_html)
    date_human = _format_date_human(publish_date)
    utm_term = slug.replace("-", "_")
    utm = f"?utm_source=seo&utm_medium=blog&utm_campaign={category}&utm_term={utm_term}"
    card = (
        f'\n    <!-- {title} -->\n'
        f'    <a class="blog-card" href="/blog/{slug}.html{utm}">\n'
        f'      <span class="card-tag">{category_ru}</span>\n'
        f'      <h2 class="card-title">{title}</h2>\n'
        f'      <p class="card-desc">{description}</p>\n'
        f'      <div class="card-meta">\n'
        f'        <span>{date_human}</span>\n'
        f'        <span>{read_min} мин</span>\n'
        f'        <span class="card-read-link">читать →</span>\n'
        f'      </div>\n'
        f'    </a>\n'
    )
    html = index_path.read_text(encoding="utf-8")
    marker = '<div class="blog-grid">'
    if marker not in html:
        log.warning("Маркер '<div class=\"blog-grid\">' не найден в blog/index.html")
        return None
    html = html.replace(marker, marker + card, 1)
    index_path.write_text(html, encoding="utf-8")
    log.info("blog/index.html: карточка '%s' добавлена первой", title)
    return index_path


def _update_sitemap(site_repo_path: Path, slug: str, publish_date: str) -> Optional[Path]:
    """Добавить URL статьи в sitemap.xml перед </urlset>."""
    sitemap_path = site_repo_path / "sitemap.xml"
    if not sitemap_path.exists():
        log.warning("sitemap.xml не найден в %s — пропускаю", site_repo_path)
        return None
    site_base = os.environ.get("SITE_URL_BASE", "https://www.distill-school.ru").rstrip("/")
    entry = (
        f'\n  <url>\n'
        f'    <loc>{site_base}/blog/{slug}.html</loc>\n'
        f'    <lastmod>{publish_date}</lastmod>\n'
        f'    <changefreq>monthly</changefreq>\n'
        f'    <priority>0.8</priority>\n'
        f'  </url>'
    )
    xml = sitemap_path.read_text(encoding="utf-8")
    if "</urlset>" not in xml:
        log.warning("sitemap.xml: тег </urlset> не найден — пропускаю")
        return None
    xml = xml.replace("</urlset>", entry + "\n</urlset>")
    sitemap_path.write_text(xml, encoding="utf-8")
    log.info("sitemap.xml: добавлен %s/blog/%s.html", site_base, slug)
    return sitemap_path


# ── Telegram + URL-файл для шага пинга ──────────────────────────────
def notify_telegram_published(results: list[ArticleResult], publish_date: str) -> None:
    """Отправить уведомление в Telegram сразу после пуша в distill-landing."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        log.info("TELEGRAM_BOT_TOKEN/CHAT_ID не заданы — пропускаю Telegram")
        return
    site_base = os.environ.get("SITE_URL_BASE", "https://www.distill-school.ru").rstrip("/")
    published = [r for r in results if r.status == "published" and r.slug]
    if not published:
        return
    lines = [f"✅ Контент-фабрика DISTILL — {publish_date}", ""]
    for r in published:
        url = f"{site_base}/blog/{r.slug}.html"
        lines.append(f"📄 {r.title}")
        lines.append(url)
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": "\n".join(lines), "disable_web_page_preview": False},
            timeout=15,
        )
        if resp.status_code == 200:
            log.info("Telegram: уведомление отправлено")
        else:
            log.warning("Telegram: %d — %s", resp.status_code, resp.text[:200])
    except Exception as e:  # noqa: BLE001
        log.warning("Telegram: сетевая ошибка — %s", e)


def write_published_urls(results: list[ArticleResult]) -> Optional[Path]:
    """Записать опубликованные URL в published_urls.txt для шага пинга в workflow."""
    site_base = os.environ.get("SITE_URL_BASE", "https://www.distill-school.ru").rstrip("/")
    published = [r for r in results if r.status == "published" and r.slug]
    if not published:
        return None
    urls = [f"{site_base}/blog/{r.slug}.html" for r in published]
    workspace = Path(os.environ.get("GITHUB_WORKSPACE", str(ROOT.parent)))
    urls_file = workspace / "content-factory" / "published_urls.txt"
    urls_file.write_text("\n".join(urls), encoding="utf-8")
    log.info("published_urls.txt: %d URL → %s", len(urls), urls_file)
    return urls_file


# ── Основной поток ────────────────────────────────────────────────────
def finalize_article(res: ArticleResult, brief: dict, article: dict,
                     blog_dir: Path, publish_date: str) -> None:
    lead = article.get("lead", "")
    body_html = article.get("body_html", "")

    html = fill_template(brief, lead, body_html, publish_date)

    target = blog_dir / f"{res.slug}.html"
    target.write_text(html, encoding="utf-8")
    res.file_path = target
    res.status = "published"

    # Обновляем blog/index.html (карточка) и sitemap.xml
    idx = _update_blog_index(blog_dir, brief, lead, body_html, publish_date)
    if idx:
        res.extra_files.append(idx)
    smp = _update_sitemap(blog_dir.parent, res.slug, publish_date)
    if smp:
        res.extra_files.append(smp)

    (LOGS / f"brief-{publish_date}-{res.slug}.json").write_text(
        json.dumps(brief, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def process_topic(topic: Topic, publish_date: str, blog_dir: Path) -> ArticleResult:
    res = ArticleResult(topic=topic)
    t0 = time.monotonic()
    try:
        log.info("→ Тема: %s", topic.topic)
        brief = make_seo_brief(topic, publish_date)
        res.slug = brief["slug"]
        res.title = brief["title"]

        if (blog_dir / f"{res.slug}.html").exists():
            res.slug = f"{res.slug}-{publish_date}"
            brief["slug"] = res.slug

        article = make_article_html(brief, publish_date)
        if ENABLE_EDITOR_PASS:
            article = edit_article_html(article)

        finalize_article(res, brief, article, blog_dir, publish_date)
        log.info("✓ %s → %s", topic.primary_keyword, res.file_path.name)
    except Exception as e:  # noqa: BLE001
        log.exception("✗ Ошибка на теме %s", topic.topic)
        res.status = "failed"
        res.error = str(e)
    finally:
        res.elapsed_sec = round(time.monotonic() - t0, 2)
    return res


def generate_articles_batched(topics: list[Topic], publish_date: str,
                              blog_dir: Path) -> list[ArticleResult]:
    results: list[ArticleResult] = []
    states: dict[int, dict] = {}

    # ── Стадия 1: брифы
    briefs_raw = run_stage_batch(
        "brief", read_prompt("01_seo_brief.md"),
        [(f"a{i}", brief_user(t, publish_date)) for i, t in enumerate(topics)],
        max_tokens=4000,
    )
    for i, topic in enumerate(topics):
        res = ArticleResult(topic=topic)
        raw = briefs_raw.get(f"a{i}")
        if not raw:
            res.status, res.error = "failed", "бриф не сгенерирован (batch)"
            results.append(res)
            continue
        try:
            brief = parse_brief(raw)
            res.slug = brief["slug"]
            res.title = brief["title"]
        except Exception as e:  # noqa: BLE001
            res.status, res.error = "failed", f"бриф не распарсился: {e}"
            results.append(res)
            continue
        if (blog_dir / f"{res.slug}.html").exists():
            res.slug = f"{res.slug}-{publish_date}"
            brief["slug"] = res.slug
        states[i] = {"topic": topic, "res": res, "brief": brief}

    # ── Стадия 2: тексты (JSON {lead, body_html})
    arts_raw = run_stage_batch(
        "article", read_prompt("02_article_writer.md"),
        [(f"a{i}", article_user(s["brief"], publish_date)) for i, s in states.items()],
        max_tokens=8000,
    )
    for i in list(states.keys()):
        raw = arts_raw.get(f"a{i}")
        if not raw:
            s = states.pop(i)
            s["res"].status, s["res"].error = "failed", "текст не сгенерирован (batch)"
            results.append(s["res"])
            continue
        try:
            states[i]["article"] = parse_article(raw.strip())
        except Exception as e:  # noqa: BLE001
            s = states.pop(i)
            s["res"].status, s["res"].error = "failed", f"JSON статьи не распарсился: {e}"
            results.append(s["res"])

    # ── Стадия 3: редактура
    if ENABLE_EDITOR_PASS and states:
        eds_raw = run_stage_batch(
            "edit", read_prompt("03_editor_factcheck.md"),
            [(f"a{i}", edit_user(s["article"])) for i, s in states.items()],
            max_tokens=8000,
        )
        for i, s in states.items():
            edited = eds_raw.get(f"a{i}")
            if edited:
                try:
                    s["article"] = parse_article(edited.strip())
                except Exception:
                    log.warning("Batch[edit]: a%d — JSON редактора некорректен, беру оригинал", i)

    # ── Финализация
    for i, s in states.items():
        res = s["res"]
        try:
            finalize_article(res, s["brief"], s["article"], blog_dir, publish_date)
            log.info("✓ %s → %s", res.topic.primary_keyword, res.file_path.name)
        except Exception as e:  # noqa: BLE001
            log.exception("✗ Финализация провалилась: %s", res.topic.topic)
            res.status, res.error = "failed", str(e)
        results.append(res)

    results.sort(key=lambda r: topics.index(r.topic))
    return results


def main() -> int:
    started = datetime.now(timezone.utc)
    publish_date = datetime.now().strftime("%Y-%m-%d")
    report = RunReport(started_at=started)

    log.info("=== content-factory: запуск %s ===", publish_date)

    try:
        ensure_site_repo()
    except (subprocess.CalledProcessError, ValueError) as e:
        log.exception("Не удалось подготовить site-репо")
        send_report(
            subject=f"[content-factory] FAIL git: {publish_date}",
            html=f"<p>Не удалось клонировать/обновить distill-landing.</p><pre>{e}</pre>",
        )
        return 2

    blog_dir = SITE_REPO_PATH / SITE_BLOG_DIR
    blog_dir.mkdir(parents=True, exist_ok=True)

    topics = pick_topics(ARTICLES_PER_DAY)
    if not topics:
        log.warning("Backlog пуст — заполните data/topics_backlog.csv")
        send_report(
            subject=f"[content-factory] backlog empty {publish_date}",
            html="<p>В topics_backlog.csv нет неиспользованных тем. Пополните пул.</p>",
        )
        return 1

    log.info("Выбрано %d тем", len(topics))

    successful_topics: list[Topic] = []
    used_rows: list[dict] = []
    written_files: list[Path] = []

    if USE_BATCH:
        try:
            results = generate_articles_batched(topics, publish_date, blog_dir)
        except Exception:  # noqa: BLE001
            log.exception("Batch-режим упал — откатываюсь на последовательный")
            results = [process_topic(t, publish_date, blog_dir) for t in topics]
    else:
        results = [process_topic(t, publish_date, blog_dir) for t in topics]

    for res in results:
        report.results.append(res)
        if res.status == "published" and res.file_path:
            written_files.append(res.file_path)
            written_files.extend(res.extra_files)  # blog/index.html + sitemap.xml
            successful_topics.append(res.topic)
            used_rows.append({
                "date": publish_date,
                "slug": res.slug,
                "title": res.title,
                "primary_keyword": res.topic.primary_keyword,
                "category": res.topic.category,
                "status": res.status,
                "url": f"{os.environ.get('SITE_URL_BASE', 'https://www.distill-school.ru')}/blog/{res.slug}.html",
            })

    if successful_topics:
        append_used(used_rows)
        remove_topics_from_backlog(successful_topics)

    push_ok = False
    if written_files:
        try:
            sha = commit_and_push(written_files, publish_date)
            report.commit_sha = sha
            push_ok = True
            # Уведомление в Telegram + файл с URL для шага пинга
            notify_telegram_published(report.results, publish_date)
            write_published_urls(report.results)
        except subprocess.CalledProcessError:
            log.exception("Git push в distill-landing провалился — откатываю CSV")
            report.commit_sha = None
            try:
                workspace = Path(os.environ.get("GITHUB_WORKSPACE", "."))
                cf_data = "content-factory/data"
                subprocess.run(
                    ["git", "checkout", "--",
                     f"{cf_data}/topics_backlog.csv", f"{cf_data}/topics_used.csv"],
                    cwd=workspace, check=False, capture_output=True,
                )
            except Exception:  # noqa: BLE001
                log.exception("Не удалось откатить CSV — backlog может быть повреждён")

    # CSV (backlog/used) → обратно в distill-seo
    if successful_topics and push_ok:
        try:
            commit_factory_csv(publish_date)
        except Exception:  # noqa: BLE001
            log.exception("CSV-коммит в distill-seo не удался")

    report.finished_at = datetime.now(timezone.utc)

    send_report(
        subject=f"[content-factory] {publish_date}: {len(written_files)}/{len(topics)} статей",
        html=_render_email_html(report, publish_date),
    )

    failed = sum(1 for r in report.results if r.status == "failed")
    return 0 if failed == 0 else 1


# ── Email HTML ────────────────────────────────────────────────────────
def _render_email_html(report: RunReport, date_str: str) -> str:
    site_base = os.environ.get("SITE_URL_BASE", "https://example.com")
    rows = []
    for r in report.results:
        color = {"published": "#0a7", "failed": "#c33"}.get(r.status, "#666")
        url = f"{site_base}/blog/{r.slug}/" if r.slug else ""
        url_link = f'<a href="{url}">{url}</a>' if url else "—"
        rows.append(f"""
        <tr>
          <td style="padding:8px;border-bottom:1px solid #eee;">{r.topic.primary_keyword}</td>
          <td style="padding:8px;border-bottom:1px solid #eee;">{r.title or '—'}</td>
          <td style="padding:8px;border-bottom:1px solid #eee;color:{color};font-weight:600;">{r.status}</td>
          <td style="padding:8px;border-bottom:1px solid #eee;font-size:13px;">{url_link}</td>
          <td style="padding:8px;border-bottom:1px solid #eee;font-size:13px;color:#999;">{r.elapsed_sec}s</td>
        </tr>""")
        if r.error:
            rows.append(f"""
            <tr><td colspan="5" style="padding:6px 8px;background:#fff5f5;color:#c33;font-size:12px;">{r.error}</td></tr>""")

    sha_block = (
        f'<p style="color:#666;">Коммит distill-landing: <code>{report.commit_sha[:7]}</code></p>'
        if report.commit_sha else
        '<p style="color:#c33;">⚠ Коммит/push не выполнен — проверьте логи.</p>'
    )

    return f"""
    <html><body style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:760px;margin:0 auto;padding:24px;">
      <h2 style="margin:0 0 8px;">Контент-фабрика — {date_str}</h2>
      <p style="color:#666;margin:0 0 16px;">
        Сгенерировано: {len(report.results)} •
        Опубликовано: {sum(1 for r in report.results if r.status == 'published')} •
        Ошибок: {sum(1 for r in report.results if r.status == 'failed')}
      </p>
      {sha_block}
      <table style="width:100%;border-collapse:collapse;font-size:14px;">
        <thead>
          <tr style="background:#fafafa;text-align:left;">
            <th style="padding:8px;border-bottom:2px solid #ddd;">Ключ</th>
            <th style="padding:8px;border-bottom:2px solid #ddd;">Title</th>
            <th style="padding:8px;border-bottom:2px solid #ddd;">Статус</th>
            <th style="padding:8px;border-bottom:2px solid #ddd;">URL</th>
            <th style="padding:8px;border-bottom:2px solid #ddd;">⏱</th>
          </tr>
        </thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
    </body></html>
    """


if __name__ == "__main__":
    sys.exit(main())
