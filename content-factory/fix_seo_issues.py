#!/usr/bin/env python3
"""
fix_seo_issues.py — одноразовый скрипт для исправления SEO-проблем M2 в distill-landing.

Что делает:
  1. Клонирует/обновляет distill-landing по SITE_REPO_TOKEN.
  2. Для каждого HTML-файла:
     - <title>     обрезает до ≤60 символов на границе слова.
     - meta desc   обрезает до ≤160 символов.
     - Синхронизирует og:description, twitter:description, JSON-LD description.
  3. Для страниц без JSON-LD (например /game/) — добавляет WebPage-схему.
  4. Коммитит все изменения и пушит в main.

ENV: те же, что у orchestrator.py.
    SITE_REPO_TOKEN, SITE_REPO_URL, SITE_REPO_BRANCH, SITE_BLOG_DIR,
    SITE_URL_BASE, SITE_REPO_PATH (для локального запуска)
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("fix-seo")

SITE_REPO_TOKEN   = os.environ.get("SITE_REPO_TOKEN", "")
SITE_REPO_URL     = os.environ.get("SITE_REPO_URL", "")
SITE_REPO_BRANCH  = os.environ.get("SITE_REPO_BRANCH", "main")
SITE_BLOG_DIR     = os.environ.get("SITE_BLOG_DIR", "blog")
SITE_URL_BASE     = os.environ.get("SITE_URL_BASE", "https://www.distill-school.ru").rstrip("/")
IS_GITHUB_ACTIONS = os.environ.get("GITHUB_ACTIONS", "").lower() == "true"

if SITE_REPO_TOKEN and SITE_REPO_URL.startswith("https://"):
    _AUTH_URL = SITE_REPO_URL.replace("https://", f"https://x-access-token:{SITE_REPO_TOKEN}@", 1)
else:
    _AUTH_URL = SITE_REPO_URL

if IS_GITHUB_ACTIONS:
    SITE_REPO_PATH = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "distill-landing-fix"
else:
    SITE_REPO_PATH = Path(os.environ.get("SITE_REPO_PATH", "/tmp/distill-landing-fix"))

TITLE_MAX    = 60
DESC_MAX     = 160
ORG_NAME     = "DISTILL — Школа метанавыков"
ORG_URL      = SITE_URL_BASE


# ── Хелперы ─────────────────────────────────────────────────────────────────

def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    cut = text[:max_len - 1]
    space = cut.rfind(" ")
    if space > max_len // 2:
        cut = cut[:space]
    return cut + "…"


def _run(cmd: list[str], cwd: Optional[Path] = None) -> str:
    log.info("$ %s", " ".join(cmd))
    result = subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


# ── Git ──────────────────────────────────────────────────────────────────────

def ensure_repo() -> None:
    if not _AUTH_URL:
        raise ValueError("SITE_REPO_URL / SITE_REPO_TOKEN не заданы")
    if not SITE_REPO_PATH.exists():
        SITE_REPO_PATH.parent.mkdir(parents=True, exist_ok=True)
        _run(["git", "clone", "--branch", SITE_REPO_BRANCH, "--depth", "1",
              _AUTH_URL, str(SITE_REPO_PATH)])
    else:
        _run(["git", "fetch", "origin"], cwd=SITE_REPO_PATH)
        _run(["git", "checkout", SITE_REPO_BRANCH], cwd=SITE_REPO_PATH)
        _run(["git", "reset", "--hard", f"origin/{SITE_REPO_BRANCH}"], cwd=SITE_REPO_PATH)
    log.info("Репо готово: %s", SITE_REPO_PATH)


def commit_and_push(changed_files: list[Path]) -> None:
    if not changed_files:
        log.info("Нет изменений для коммита")
        return
    paths = [str(p.relative_to(SITE_REPO_PATH)) for p in changed_files]
    _run(["git", "add", "--", *paths], cwd=SITE_REPO_PATH)
    diff = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=SITE_REPO_PATH, capture_output=True, text=True, check=True,
    )
    if not diff.stdout.strip():
        log.info("Нет staged-изменений после git add")
        return
    _run(
        ["git", "-c", "user.email=bot@example.com", "-c", "user.name=seo-fix",
         "commit", "-m",
         f"fix: укороченный title/description для M2 SEO-аудита\n\n"
         f"Исправлено {len(changed_files)} файлов: title ≤{TITLE_MAX} символов, "
         f"desc ≤{DESC_MAX} символов, добавлен JSON-LD где отсутствовал."],
        cwd=SITE_REPO_PATH,
    )
    _run(["git", "push", "origin", SITE_REPO_BRANCH], cwd=SITE_REPO_PATH)
    log.info("Запушено %d файлов", len(changed_files))


# ── HTML-фиксы ───────────────────────────────────────────────────────────────

def _get_json_ld(soup: BeautifulSoup) -> tuple[Optional[dict], Optional[object]]:
    for script in soup.find_all("script", type="application/ld+json"):
        raw = (script.string or "").strip()
        if raw:
            try:
                return json.loads(raw), script
            except json.JSONDecodeError:
                pass
    return None, None


def fix_html_file(path: Path) -> bool:
    """Возвращает True, если файл был изменён."""
    html = path.read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")
    changed = False

    # ── <title> ──────────────────────────────────────────────────────────────
    title_tag = soup.find("title")
    if title_tag:
        current_title = (title_tag.string or "").strip()
        if len(current_title) > TITLE_MAX:
            # Используем JSON-LD headline, если он короче лимита (это исходный SEO-заголовок)
            json_ld_data, _ = _get_json_ld(soup)
            headline = (json_ld_data or {}).get("headline", "")
            if headline and len(headline) <= TITLE_MAX:
                new_title = headline
            else:
                new_title = _truncate(current_title, TITLE_MAX)
            title_tag.string = new_title
            log.info("%s: title %d→%d  %r", path.name, len(current_title), len(new_title), new_title)
            changed = True

    # ── <meta name="description"> ────────────────────────────────────────────
    desc_tag = soup.find("meta", attrs={"name": "description"})
    if desc_tag and (desc_tag.get("content") or "").strip():
        current_desc = desc_tag["content"].strip()
        if len(current_desc) > DESC_MAX:
            new_desc = _truncate(current_desc, DESC_MAX)
            desc_tag["content"] = new_desc

            # og:description
            og_desc = soup.find("meta", property="og:description")
            if og_desc and og_desc.get("content", "").strip() == current_desc:
                og_desc["content"] = new_desc

            # twitter:description
            tw_desc = soup.find("meta", attrs={"name": "twitter:description"})
            if tw_desc and tw_desc.get("content", "").strip() == current_desc:
                tw_desc["content"] = new_desc

            # JSON-LD description
            json_ld_data, ld_script = _get_json_ld(soup)
            if json_ld_data and ld_script:
                if json_ld_data.get("description", "").strip() == current_desc:
                    json_ld_data["description"] = new_desc
                    ld_script.string = json.dumps(json_ld_data, ensure_ascii=False, indent=2)

            log.info("%s: desc %d→%d", path.name, len(current_desc), len(new_desc))
            changed = True

    # ── JSON-LD: добавить, если отсутствует ─────────────────────────────────
    if not soup.find("script", type="application/ld+json"):
        changed |= _add_webpage_json_ld(soup, path)

    if changed:
        path.write_text(str(soup), encoding="utf-8")

    return changed


def _add_webpage_json_ld(soup: BeautifulSoup, path: Path) -> bool:
    """Добавляет минимальный WebPage JSON-LD перед </head>."""
    head = soup.find("head")
    if not head:
        return False

    title_tag = soup.find("title")
    name = (title_tag.string or "").strip() if title_tag else ORG_NAME

    desc_tag = soup.find("meta", attrs={"name": "description"})
    desc = (desc_tag.get("content") or "").strip() if desc_tag else ""

    canonical = soup.find("link", rel="canonical")
    url = (canonical.get("href") or "").strip() if canonical else f"{SITE_URL_BASE}/"

    data = {
        "@context": "https://schema.org",
        "@type": "WebPage",
        "name": name,
        "url": url,
        "description": desc,
        "publisher": {"@type": "Organization", "name": ORG_NAME, "url": ORG_URL},
    }

    script_tag = soup.new_tag("script", type="application/ld+json")
    script_tag.string = json.dumps(data, ensure_ascii=False, indent=2)
    head.append(script_tag)
    log.info("%s: добавлен WebPage JSON-LD", path.name)
    return True


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ensure_repo()

    html_files = list(SITE_REPO_PATH.rglob("*.html"))
    log.info("Найдено HTML-файлов: %d", len(html_files))

    changed_files: list[Path] = []
    for path in sorted(html_files):
        try:
            if fix_html_file(path):
                changed_files.append(path)
        except Exception as e:
            log.warning("Ошибка в %s: %s", path, e)

    log.info("Изменено файлов: %d / %d", len(changed_files), len(html_files))
    commit_and_push(changed_files)
    return 0


if __name__ == "__main__":
    sys.exit(main())
