"""
Daily digest — короткая утренняя сводка (≈08:30 МСК).

В отличие от weekly_digest, это ежедневная сводка, но окно трафика — скользящее:
  - GSC: трафик за 28 дней (сдвиг -3 дня) vs предыдущее 28-дневное окно.
  - Позиции: последний снимок rankings vs предыдущий (движения за сутки).
  - Тех-аудит: последний аудит + что нового появилось за сутки.
  - Я.Вебмастер: ИКС (+ дельта), страниц в индексе.
  - Публикации: статьи за вчера.
  - SEO-стратег: 3–5 приоритетов на сегодня (Claude).

Одно сообщение на сайт. Домен берётся из SEO_SITE_DOMAIN или из GSC_SITE_URL.

Запуск:
    python3 orchestrator.py daily
    python3 orchestrator.py daily --dry-run
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

THIS_DIR = Path(__file__).resolve().parent
if str(THIS_DIR.parent) not in sys.path:
    sys.path.insert(0, str(THIS_DIR.parent))

log = logging.getLogger(__name__)

REPORTS_DIR = THIS_DIR.parent / "reports"
AUDITS_DIR = THIS_DIR.parent / "data" / "audits"
RANKINGS_DIR = THIS_DIR.parent / "data" / "rankings"

# Движение за сутки мельче недельного — порог «заметного» сдвига ниже.
DAILY_MOVE_THRESHOLD = 3


def site_domain() -> str:
    """Человеческий домен сайта: example.com, example.com, ..."""
    explicit = os.environ.get("SEO_SITE_DOMAIN", "").strip()
    if explicit:
        return explicit
    raw = os.environ.get("GSC_SITE_URL", "sc-domain:example.com").strip()
    raw = raw.replace("sc-domain:", "").replace("https://", "").replace("http://", "")
    return raw.strip("/")


# ───── Источники с дневной дельтой ──────────────────────────────────

# Окно трафика. Молодой сайт даёт <1 показа в день, поэтому «вчера» почти всегда
# 0 — сравнение суток бессмысленно. Берём агрегат за 28 дней со сдвигом на 3 дня
# назад (GSC отдаёт данные с задержкой 2-3 дня) и сравниваем с предыдущим окном
# такой же длины. GSC_SITE_URL в workflow — URL-prefix https://www.distill-school.ru/,
# в нём статистики больше, чем в domain-property.
GSC_LAG_DAYS = 3
GSC_WINDOW_DAYS = 28


def _gsc_window(svc, url: str, start: str, end: str) -> dict:
    """Агрегат (клики/показы/средняя позиция) + топ-запросы за период [start, end]."""
    from modules.gsc_client import query_search_analytics
    # dimensions=[] → одна агрегированная строка с суммарными метриками за период.
    totals = query_search_analytics(svc, url, start, end, [], row_limit=1)
    agg = totals[0] if totals else {}
    queries = query_search_analytics(svc, url, start, end, ["query"], row_limit=10)
    return {
        "clicks": int(agg.get("clicks", 0)),
        "impressions": int(agg.get("impressions", 0)),
        "position": float(agg.get("position", 0.0)),
        "queries": queries,
    }


def gsc_traffic_daily() -> dict:
    """Трафик за последние 28 дней (сдвиг -3 дня) vs предыдущее 28-дневное окно.

    Возвращает суммарные клики/показы, среднюю позицию, топ-запросы и дельту
    к прошлому окну. Пустой dict — если данных нет вовсе.
    """
    try:
        from modules.gsc_client import gsc_service
        svc = gsc_service()
        url = os.environ.get("GSC_SITE_URL", "sc-domain:example.com")

        end = dt.date.today() - dt.timedelta(days=GSC_LAG_DAYS)
        start = end - dt.timedelta(days=GSC_WINDOW_DAYS - 1)
        prev_end = start - dt.timedelta(days=1)
        prev_start = prev_end - dt.timedelta(days=GSC_WINDOW_DAYS - 1)

        cur = _gsc_window(svc, url, start.isoformat(), end.isoformat())
        prev = _gsc_window(svc, url, prev_start.isoformat(), prev_end.isoformat())

        if not (cur["clicks"] or cur["impressions"] or cur["queries"]):
            return {}

        has_prev = bool(prev["clicks"] or prev["impressions"])
        top = [
            {
                "query": r["keys"][0],
                "clicks": int(r.get("clicks", 0)),
                "impressions": int(r.get("impressions", 0)),
                "position": float(r.get("position", 0.0)),
            }
            for r in cur["queries"][:5]
        ]
        return {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "days": GSC_WINDOW_DAYS,
            "clicks": cur["clicks"],
            "impressions": cur["impressions"],
            "position": cur["position"],
            "delta_clicks": cur["clicks"] - prev["clicks"],
            "delta_impressions": cur["impressions"] - prev["impressions"],
            # Позиция: меньше = лучше, поэтому в дельте отрицательное — это рост.
            "delta_position": round(cur["position"] - prev["position"], 1) if has_prev else 0.0,
            "top_queries": top,
            "has_prev": has_prev,
        }
    except Exception as e:
        log.warning("GSC daily traffic: %s", e)
        return {}


def _load_rankings(path: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out[row["keyword"]] = row
    return out


def rankings_daily() -> dict:
    """Сравнить последний снимок позиций с предыдущим (сутки)."""
    if not RANKINGS_DIR.exists():
        return {}
    snapshots = sorted(RANKINGS_DIR.glob("*.csv"), reverse=True)
    if not snapshots:
        return {}
    latest = snapshots[0]
    curr = _load_rankings(latest)

    result = {
        "date": latest.stem,
        "total": len(curr),
        "google_top10": sum(1 for r in curr.values()
                            if r.get("google_pos") and float(r["google_pos"]) <= 10),
        "google_top30": sum(1 for r in curr.values()
                            if r.get("google_pos") and float(r["google_pos"]) <= 30),
        "yandex_top10": sum(1 for r in curr.values()
                            if r.get("yandex_pos") and float(r["yandex_pos"]) <= 10),
        "risers": [], "fallers": [], "new_top10": [],
    }

    prev_path = snapshots[1] if len(snapshots) >= 2 else None
    if not prev_path:
        return result
    prev = _load_rankings(prev_path)

    for kw, row in curr.items():
        if kw not in prev:
            continue
        for engine in ("google", "yandex"):
            try:
                new = float(row[f"{engine}_pos"]) if row.get(f"{engine}_pos") else None
                old = float(prev[kw][f"{engine}_pos"]) if prev[kw].get(f"{engine}_pos") else None
            except (KeyError, ValueError):
                continue
            if new is None or old is None:
                continue
            diff = new - old
            if diff <= -DAILY_MOVE_THRESHOLD:
                result["risers"].append((kw, old, new, engine))
            elif diff >= DAILY_MOVE_THRESHOLD:
                result["fallers"].append((kw, old, new, engine))
            if old > 10 and new <= 10:
                result["new_top10"].append((kw, old, new, engine))

    result["risers"].sort(key=lambda x: x[1] - x[2], reverse=True)
    result["fallers"].sort(key=lambda x: x[2] - x[1], reverse=True)
    return result


def audit_delta_daily(current: dict, max_gap_days: int = 2) -> tuple[int, int]:
    """(новых, исправлено) относительно предыдущего снимка аудита.

    Суточную дельту показываем, только если предыдущий снимок свежий
    (≤ max_gap_days). Иначе сравнение было бы не «за сутки», а за неделю —
    и цифры вводили бы в заблуждение. Когда M2 крутится ежедневно, снимки
    идут день в день и дельта честная.
    """
    if not current or not AUDITS_DIR.exists():
        return (0, 0)
    days = sorted([d for d in AUDITS_DIR.iterdir() if d.is_dir()], reverse=True)
    curr_date = current.get("date")
    try:
        curr_dt = dt.date.fromisoformat(curr_date) if curr_date else dt.date.today()
    except ValueError:
        curr_dt = dt.date.today()
    for d in days:
        if d.name == curr_date:
            continue
        j = d / "audit.json"
        if not j.exists():
            continue
        try:
            if (curr_dt - dt.date.fromisoformat(d.name)).days > max_gap_days:
                return (0, 0)
        except ValueError:
            pass
        prev = json.loads(j.read_text(encoding="utf-8"))
        prev_keys = {(i["url"], i["check"], i["detail"]) for i in prev.get("issues", [])}
        curr_keys = {(i["url"], i["check"], i["detail"]) for i in current.get("issues", [])}
        return (len(curr_keys - prev_keys), len(prev_keys - curr_keys))
    return (0, 0)


def yandex_webmaster_daily() -> dict:
    """ИКС + дельта ИКС за сутки + страниц в индексе. Мягко падает при ошибках."""
    try:
        from modules.yandex_webmaster import (
            yw_resolve_host_id, yw_host_info, yw_sqi_history,
        )
        user_id, host_id = yw_resolve_host_id(host_url=site_domain())
        info = yw_host_info(user_id, host_id)
        out = {"sqi": info.get("sqi"), "verified": info.get("verified")}

        # Дельта ИКС из истории (последние 2 точки).
        try:
            date_to = (dt.date.today()).isoformat()
            date_from = (dt.date.today() - dt.timedelta(days=21)).isoformat()
            points = yw_sqi_history(user_id, host_id, date_from, date_to)
            vals = [p.get("value") for p in points if p.get("value") is not None]
            if len(vals) >= 2 and isinstance(vals[-1], int) and isinstance(vals[-2], int):
                out["sqi_delta"] = vals[-1] - vals[-2]
        except Exception as e:
            log.debug("sqi-history: %s", e)
        return out
    except Exception as e:
        log.warning("Я.Вебмастер daily: %s", e)
        return {}


def yandex_traffic_daily() -> dict:
    """Трафик из Яндекса за 28 дней (сдвиг -3) vs предыдущее 28-дневное окно.

    Возвращает показы/клики/среднюю позицию по всему сайту, топ-5 запросов и
    дельту к прошлому окну. Окно и лаг — те же, что у GSC (см. GSC_LAG_DAYS).
    При ошибке API возвращает {"error": "<короткая причина>"} — дайджест не роняем.
    """
    try:
        from modules.yandex_webmaster import (
            yw_resolve_host_id, yw_queries_total, yw_top_queries,
        )
        user_id, host_id = yw_resolve_host_id(host_url=site_domain())

        end = dt.date.today() - dt.timedelta(days=GSC_LAG_DAYS)
        start = end - dt.timedelta(days=GSC_WINDOW_DAYS - 1)
        prev_end = start - dt.timedelta(days=1)
        prev_start = prev_end - dt.timedelta(days=GSC_WINDOW_DAYS - 1)

        cur = yw_queries_total(user_id, host_id, start.isoformat(), end.isoformat())
        prev = yw_queries_total(user_id, host_id, prev_start.isoformat(), prev_end.isoformat())
        top = yw_top_queries(user_id, host_id, start.isoformat(), end.isoformat(), limit=5)

        if not (cur["shows"] or cur["clicks"] or top):
            return {}

        has_prev = bool(prev["shows"] or prev["clicks"])
        return {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "days": GSC_WINDOW_DAYS,
            "shows": cur["shows"],
            "clicks": cur["clicks"],
            "position": cur["position"],
            "click_position": cur.get("click_position") or 0.0,
            "delta_shows": cur["shows"] - prev["shows"],
            "delta_clicks": cur["clicks"] - prev["clicks"],
            "top_queries": top,
            "has_prev": has_prev,
        }
    except Exception as e:
        # Короткая причина для человека, без трейсбэка. HTTPError → код+текст.
        reason = getattr(getattr(e, "response", None), "text", "") or str(e)
        reason = reason.strip().replace("\n", " ")[:140]
        log.warning("Яндекс trafic daily: %s", e)
        return {"error": reason or "нет данных"}


def content_published_yesterday() -> list[dict]:
    """Статьи, опубликованные вчера (из weekly_digest, но cutoff=вчера)."""
    try:
        from modules.weekly_digest import content_factory_published
        yesterday = dt.date.today() - dt.timedelta(days=1)
        rows = content_factory_published(yesterday)
        return [r for r in rows if (r.get("date") or "") == yesterday.isoformat()]
    except Exception as e:
        log.debug("content_published_yesterday: %s", e)
        return []


def latest_audit() -> Optional[dict]:
    from modules.weekly_digest import latest_audit as _la
    return _la()


# ───── Сборка payload ───────────────────────────────────────────────

def build_payload() -> dict:
    payload = {
        "traffic": gsc_traffic_daily(),
        "yandex_traffic": yandex_traffic_daily(),
        "rankings": rankings_daily(),
        "audit": latest_audit(),
        "yandex_webmaster": yandex_webmaster_daily(),
        "content_published": content_published_yesterday(),
    }
    if payload.get("audit"):
        payload["audit_delta"] = audit_delta_daily(payload["audit"])
    return payload


# ───── Рендер ───────────────────────────────────────────────────────

def _eng_ru(eng: str) -> str:
    return "Яндекс" if eng == "yandex" else "Google"


def render_telegram(today: dt.date, site: str, payload: dict, advice: str) -> str:
    from notifiers.humanize import plural

    lines = [f"☀️ {site} · утренняя сводка · {today.strftime('%d.%m')}"]

    t = payload.get("traffic") or {}
    if t:
        dc, di = t["delta_clicks"], t["delta_impressions"]
        days = t.get("days", GSC_WINDOW_DAYS)
        trend = ("больше прошлого периода" if dc > 0
                 else "меньше прошлого периода" if dc < 0 else "как в прошлом периоде")
        delta_note = f"{dc:+d} — {trend}" if t.get("has_prev") else "первое окно"
        lines.append(
            f"\n👥 Трафик из Google за {days} дней: {t['clicks']} "
            f"{plural(t['clicks'], 'переход', 'перехода', 'переходов')} "
            f"({delta_note}). Показов {t['impressions']} ({di:+d})."
        )
        pos = t.get("position") or 0.0
        if pos:
            dp = t.get("delta_position") or 0.0
            # dp<0 = позиция улучшилась (стала ближе к 1).
            pos_trend = "" if not (t.get("has_prev") and dp) else (
                f", {'↑' if dp < 0 else '↓'} {abs(dp):.1f}")
            lines.append(f"   📍 Средняя позиция: {pos:.1f}{pos_trend}.")
        tq = t.get("top_queries") or []
        if tq:
            lines.append("   🔑 Топ-запросы:")
            for q in tq:
                lines.append(
                    f"      • «{q['query']}» — {q['clicks']} "
                    f"{plural(q['clicks'], 'клик', 'клика', 'кликов')} / "
                    f"{q['impressions']} показ. (поз. {q['position']:.1f})"
                )
    else:
        lines.append("\n👥 Трафик из Google: данных пока нет "
                     "(новый домен или Search Console ещё копит статистику).")

    y = payload.get("yandex_traffic") or {}
    if y.get("error"):
        lines.append(f"\n🔎 Яндекс: данные недоступны ({y['error']}).")
    elif y:
        dc, ds = y["delta_clicks"], y["delta_shows"]
        days = y.get("days", GSC_WINDOW_DAYS)
        trend = ("больше прошлого периода" if ds > 0
                 else "меньше прошлого периода" if ds < 0 else "как в прошлом периоде")
        delta_note = f"{ds:+d} — {trend}" if y.get("has_prev") else "первое окно"
        lines.append(
            f"\n🔎 Трафик из Яндекса за {days} дней: {y['clicks']} "
            f"{plural(y['clicks'], 'переход', 'перехода', 'переходов')} "
            f"({dc:+d}). Показов {y['shows']} ({delta_note})."
        )
        pos = y.get("position") or 0.0
        cpos = y.get("click_position") or 0.0
        if pos:
            # Позиция показа сопоставима с Google; в скобках — позиция клика
            # (её Вебмастер выносит в заголовок «средняя позиция»).
            cpos_str = f" (по кликам {cpos:.1f})" if cpos else ""
            lines.append(f"   📍 Средняя позиция показа: {pos:.1f}{cpos_str}.")
        yq = y.get("top_queries") or []
        if yq:
            lines.append("   🔑 Топ-запросы:")
            for q in yq:
                pos_str = f"поз. {q['position']:.1f}" if q.get("position") is not None else "поз. —"
                lines.append(
                    f"      • «{q['query']}» — {q['clicks']} "
                    f"{plural(q['clicks'], 'клик', 'клика', 'кликов')} / "
                    f"{q['shows']} показ. ({pos_str})"
                )

    r = payload.get("rankings") or {}
    if r and r.get("total"):
        lines.append(f"\n📊 В ТОП-10 Google: {r.get('google_top10', 0)} из "
                     f"{r.get('total', 0)} запросов, в ТОП-30: {r.get('google_top30', 0)}.")
        if r.get("yandex_top10"):
            lines.append(f"   В ТОП-10 Яндекса: {r['yandex_top10']}.")
        if r.get("new_top10"):
            lines.append("   🟢 Вышли на 1-ю страницу за сутки:")
            for kw, old, new, eng in r["new_top10"][:3]:
                lines.append(f"      • «{kw}» ({old:.0f}→{new:.0f}, {_eng_ru(eng)})")
        if r.get("risers"):
            lines.append(f"   ⬆️ Подросли: {len(r['risers'])}")
            for kw, old, new, eng in r["risers"][:2]:
                lines.append(f"      • «{kw}» ({old:.0f}→{new:.0f}, {_eng_ru(eng)})")
        if r.get("fallers"):
            lines.append(f"   🔻 Просели: {len(r['fallers'])}")
            for kw, old, new, eng in r["fallers"][:2]:
                lines.append(f"      • «{kw}» ({old:.0f}→{new:.0f}, {_eng_ru(eng)})")

    a = payload.get("audit")
    if a:
        by_sev: dict[str, int] = {}
        for i in a.get("issues", []):
            by_sev[i["severity"]] = by_sev.get(i["severity"], 0) + 1
        urgent = by_sev.get("critical", 0) + by_sev.get("high", 0)
        minor = by_sev.get("medium", 0) + by_sev.get("low", 0)
        new_count, fixed_count = payload.get("audit_delta", (0, 0))
        delta_str = ""
        if new_count or fixed_count:
            delta_str = f" За сутки: +{new_count} новых, −{fixed_count} исправлено."
        lines.append(f"\n🔍 Тех-замечания: важных — {urgent}, помельче — {minor}.{delta_str}")
        stats = a.get("crawl_stats") or {}
        broken = (stats.get("client_err", 0) or 0) + (stats.get("server_err", 0) or 0)
        if broken:
            lines.append(f"   ⚠️ Недоступных страниц: {broken} из {stats.get('total')}.")

    yw = payload.get("yandex_webmaster") or {}
    if yw.get("sqi") is not None:
        d = yw.get("sqi_delta")
        d_str = f" ({d:+d})" if isinstance(d, int) and d else ""
        lines.append(f"\n🌐 Яндекс: ИКС {yw['sqi']}{d_str}.")

    cf = payload.get("content_published") or []
    if cf:
        n = len(cf)
        lines.append(f"\n✍️ Опубликовано вчера: {n} {plural(n, 'статья', 'статьи', 'статей')}.")

    if advice:
        lines.append("\n🧭 SEO-стратег:")
        lines.append(advice)

    return "\n".join(lines)


def render_markdown(today: dt.date, site: str, payload: dict, advice: str) -> str:
    """Полный текст для архива в reports/daily-*.md (тот же текст, что в Telegram)."""
    header = f"# Утренняя SEO-сводка · {site} · {today.isoformat()}\n"
    return header + "\n" + render_telegram(today, site, payload, advice)


# ───── Main ─────────────────────────────────────────────────────────

def run_daily(dry_run: bool = False) -> Path:
    today = dt.date.today()
    site = site_domain()
    log.info("=== Daily digest · %s · %s ===", site, today)

    payload = build_payload()

    period = "за последние 28 дней (трафик) и за сутки (позиции, аудит)"
    try:
        from modules.strategist import strategist_advice
        advice = strategist_advice(site, period, payload)
    except Exception as e:
        log.warning("Стратег недоступен: %s", e)
        advice = ""

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS_DIR / f"daily-{today.isoformat()}.md"
    report_path.write_text(render_markdown(today, site, payload, advice), encoding="utf-8")
    log.info("Markdown: %s", report_path)

    if not dry_run and os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
        from notifiers.telegram import send_telegram
        send_telegram(render_telegram(today, site, payload, advice))
        log.info("Daily digest отправлен в Telegram")
    else:
        log.info("dry-run или нет Telegram-кредов — в Telegram не отправляю")

    return report_path


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Daily SEO digest")
    parser.add_argument("--dry-run", action="store_true", help="Не слать в Telegram")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_daily(dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
