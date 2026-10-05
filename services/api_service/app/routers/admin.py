# services/api_service/app/routers/admin.py
"""Админка сбора данных: история запусков обхода, детали запуска по шардам, состояние планировщика
и запуск обхода по кнопке; проблемы сбора, резервные копии БД и проверка алертов в Telegram.

Вход — HTTP Basic (ADMIN_USER, ADMIN_PASSWORD); без ADMIN_PASSWORD админка выключена. Состояние
и запуск — через управление планировщиком (SCHEDULER_URL, car_scrapers/scheduler.py).
"""
import asyncio
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from html import escape
from typing import Any, Optional
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from eadh_common.models import Anomaly, AnomalyStatus, CrawlRun, CrawlShard, Listing

from app.core.config import settings
from app.db.database import get_session

basic = HTTPBasic(auto_error=False)

CATEGORY_NAMES = {"car": "легковые", "motorcycle": "мотоциклы"}
RUNS_ON_PAGE = 50
# Проблемы сбора: находки «здоровья сбора» и качества по запуску/площадке (не по объявлениям)
PROBLEM_KINDS = ("crawl_health", "data_quality")
PROBLEM_ENTITIES = ("run", "source")
PROBLEM_DAYS = 14
TRIGGER_NAMES = {"manual": "по кнопке", "schedule": "по расписанию", "catch_up": "догоняющий",
                 "start": "при старте"}


def require_admin(credentials: Optional[HTTPBasicCredentials] = Depends(basic)) -> str:
    if not settings.ADMIN_PASSWORD:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Админка выключена: задайте ADMIN_PASSWORD в .env")
    if credentials is None or not (
            secrets.compare_digest(credentials.username.encode(), settings.ADMIN_USER.encode())
            and secrets.compare_digest(credentials.password.encode(), settings.ADMIN_PASSWORD.encode())):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Нужен вход в админку",
                            headers={"WWW-Authenticate": 'Basic realm="EuroAutoDataHub admin"'})
    return credentials.username


router = APIRouter(dependencies=[Depends(require_admin)])


# --- Планировщик ---

def _scheduler_call(method: str, path: str) -> tuple[int, dict]:
    request = urllib.request.Request(settings.SCHEDULER_URL.rstrip("/") + path, method=method)
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.load(error)
        except ValueError:
            return error.code, {}


async def scheduler_status() -> Optional[dict]:
    """Состояние планировщика; None — недоступен."""
    try:
        code, body = await asyncio.to_thread(_scheduler_call, "GET", "/status")
    except (OSError, ValueError):
        return None
    return body if code == 200 else None


async def scheduler_run() -> tuple[bool, str]:
    try:
        code, body = await asyncio.to_thread(_scheduler_call, "POST", "/run")
    except (OSError, ValueError):
        return False, "Планировщик недоступен"
    if code == 202:
        return True, "Обход запущен"
    return False, body.get("error") or f"Планировщик ответил {code}"


def backup_status() -> Optional[dict]:
    """Итог последней резервной копии (ops/backup/backup.sh); None — копий ещё не было."""
    path = Path(settings.BACKUP_DIR) / "last_backup.json"
    try:
        status_ = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    status_["files"] = len(list(Path(settings.BACKUP_DIR).glob("eadh_*.dump")))
    return status_


def _telegram_send(text: str) -> None:
    data = urllib.parse.urlencode({"chat_id": settings.TELEGRAM_CHAT_ID, "text": text}).encode()
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage", data=data, method="POST")
    with urllib.request.urlopen(request, timeout=10) as response:
        if not json.load(response).get("ok"):
            raise ValueError("Telegram ответил ok=false")


async def telegram_test() -> tuple[bool, str]:
    if not (settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID):
        return False, "Telegram не настроен: задайте TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в .env"
    try:
        await asyncio.to_thread(_telegram_send, "✅ EuroAutoDataHub: тестовое сообщение из админки. "
                                                "Сюда будут приходить отчёты о сборе и алерты.")
    except urllib.error.HTTPError as error:
        return False, f"Telegram ответил {error.code}: проверьте токен бота и chat id (и что вы написали боту /start)"
    except (OSError, ValueError) as error:
        return False, f"Не удалось отправить: {error}"
    return True, "Тестовое сообщение отправлено в Telegram"


# --- Форматирование ---

def _tz() -> ZoneInfo:
    return ZoneInfo(settings.CRAWL_TZ)


def _when(value: Optional[Any]) -> str:
    if value is None:
        return "—"
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.astimezone(_tz()).strftime("%d.%m.%Y %H:%M")


def _duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "—"
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} ч {minutes} мин"
    return f"{minutes} мин {secs} с" if minutes else f"{secs} с"


def _num(value: Optional[Any]) -> str:
    return "—" if value is None else f"{value:,}".replace(",", " ")


def _size(value: Optional[int]) -> str:
    if not value:
        return "—"
    return f"{value / 1024 / 1024:.1f} МБ" if value >= 1024 * 1024 else f"{value / 1024:.0f} КБ"


def _pct(value: Optional[float]) -> str:
    return "—" if value is None else f"{value:.0%}"


def _categories(shards: list[CrawlShard]) -> str:
    found = sorted({(s.filters or {}).get("category") or "car" for s in shards})
    return ", ".join(CATEGORY_NAMES.get(c, c) for c in found) or "—"


def _run_state(run: CrawlRun) -> tuple[str, str]:
    """(текст, класс бейджа)."""
    if run.status != "finished":
        return "идёт", "info"
    report = run.report or {}
    if run.finish_reason != "finished":
        return f"прерван ({run.finish_reason})", "bad"
    if report.get("warnings") or report.get("failed_shards"):
        return "с предупреждениями", "warn"
    return "успешно", "ok"


def _run_summary(run: CrawlRun, shards: list[CrawlShard]) -> dict:
    report = run.report or {}
    if run.status != "finished":  # отчёта ещё нет: считаем по пришедшим шардам
        collected = sum(s.collected_count for s in shards)
        expected = sum(s.expected_count for s in shards)
        complete = sum(1 for s in shards if s.complete)
        duration = (datetime.now(run.started_at.tzinfo) - run.started_at).total_seconds()
        # всего шардов заранее не известно: большие шарды дробятся по ходу обхода
        return {"collected": collected, "expected": expected, "completeness": None,
                "shards": f"{complete}, идёт", "new": None, "delisted": None, "warnings": [],
                "duration": duration}
    events = report.get("events") or {}
    duration = report.get("duration_s")
    if duration is None and run.finished_at:
        duration = (run.finished_at - run.started_at).total_seconds()
    return {
        "collected": report.get("collected"), "expected": report.get("expected"),
        "completeness": report.get("completeness"),
        "shards": f"{report.get('shards_complete', sum(1 for s in shards if s.complete))} / "
                  f"{report.get('shards_planned') or run.shards_planned or len(shards)}",
        "new": events.get("new", 0), "delisted": events.get("delisted", 0),
        "warnings": report.get("warnings") or [], "duration": duration,
    }


STYLE = """
:root{--bg:#f6f7f9;--card:#fff;--text:#1d2330;--muted:#667085;--line:#e4e7ec;--accent:#2f6fde;
--ok:#1a7f4b;--ok-bg:#e6f4ec;--warn:#9a6700;--warn-bg:#fff4d6;--bad:#b42318;--bad-bg:#fde8e7;
--info:#175cd3;--info-bg:#e5efff;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#111318;--card:#1a1d24;--text:#e6e8ec;--muted:#98a2b3;
--line:#2b303b;--accent:#6a9cf5;--ok:#5ac58b;--ok-bg:#16301f;--warn:#e5b546;--warn-bg:#33290f;
--bad:#f2766b;--bad-bg:#3a1714;--info:#84adff;--info-bg:#15233d;color-scheme:dark}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,-apple-system,"Segoe UI",
Roboto,sans-serif}
main{max-width:1200px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 10px}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.muted{color:var(--muted)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-top:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.card .label{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.04em}
.card .value{font-size:20px;font-weight:600;margin-top:4px;font-variant-numeric:tabular-nums}
.card .sub{color:var(--muted);font-size:12px;margin-top:2px}
.bar{display:flex;flex-wrap:wrap;align-items:center;gap:12px;justify-content:space-between}
button{font:inherit;font-weight:600;padding:9px 16px;border-radius:8px;border:0;background:var(--accent);
color:#fff;cursor:pointer}button:disabled{opacity:.45;cursor:not-allowed}
.table{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%;min-width:760px}
th,td{padding:9px 12px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap}
th{font-size:12px;color:var(--muted);font-weight:600;background:var(--card)}
tr:last-child td{border-bottom:0}td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;font-weight:600}
.ok{color:var(--ok);background:var(--ok-bg)}.warn{color:var(--warn);background:var(--warn-bg)}
.bad{color:var(--bad);background:var(--bad-bg)}.info{color:var(--info);background:var(--info-bg)}
.notice{padding:10px 14px;border-radius:8px;margin-top:16px}
ul.warnings{margin:6px 0 0;padding-left:18px}
code{font-size:12px}
form.inline{display:inline;margin:0}
button.link{background:none;color:var(--accent);padding:0;font-weight:600}
td.wrap{white-space:normal;min-width:320px}
table.problems{min-width:600px}
details.resolved{margin-top:4px}details.resolved summary{cursor:pointer;color:var(--muted);padding:6px 0}
"""


def page(title: str, body: str, refresh: bool = False) -> HTMLResponse:
    meta = '<meta http-equiv="refresh" content="20">' if refresh else ""
    return HTMLResponse(f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">{meta}
<title>{escape(title)}</title><style>{STYLE}</style></head><body><main>{body}</main></body></html>""")


def card(label: str, value: str, sub: str = "") -> str:
    return (f'<div class="card"><div class="label">{escape(label)}</div><div class="value">{value}</div>'
            f'<div class="sub">{sub}</div></div>')


# --- Страницы ---

@router.get("", response_class=HTMLResponse)
async def admin_home(msg: Optional[str] = None, ok: bool = True, session: AsyncSession = Depends(get_session)):
    runs = (await session.execute(
        select(CrawlRun).order_by(CrawlRun.started_at.desc()).limit(RUNS_ON_PAGE))).scalars().all()
    shards_by_run: dict[str, list[CrawlShard]] = {run.id: [] for run in runs}
    if runs:
        for shard in (await session.execute(
                select(CrawlShard).where(CrawlShard.run_id.in_(list(shards_by_run))))).scalars().all():
            shards_by_run[shard.run_id].append(shard)
    active = (await session.execute(
        select(Listing.category, func.count()).where(Listing.status == "active").group_by(Listing.category)
    )).all()
    sched = await scheduler_status()
    since = (datetime.now(timezone.utc) - timedelta(days=PROBLEM_DAYS)).date()
    open_problems = (await session.execute(
        select(Anomaly).where(Anomaly.kind.in_(PROBLEM_KINDS), Anomaly.entity_type.in_(PROBLEM_ENTITIES),
                              Anomaly.status == AnomalyStatus.NEW.value, Anomaly.detected_on >= since)
        .order_by(Anomaly.detected_on.desc(), Anomaly.id.desc()).limit(30))).scalars().all()
    # решённые за те же дни: «Решено» можно отменить
    resolved_problems = (await session.execute(
        select(Anomaly).where(Anomaly.kind.in_(PROBLEM_KINDS), Anomaly.entity_type.in_(PROBLEM_ENTITIES),
                              Anomaly.status == AnomalyStatus.RESOLVED.value, Anomaly.detected_on >= since)
        .order_by(Anomaly.status_changed_at.desc(), Anomaly.id.desc()).limit(30))).scalars().all()
    backup = backup_status()

    # Планировщик
    if sched is None:
        state, can_run = '<span class="badge bad">планировщик недоступен</span>', False
    elif sched.get("running"):
        state = (f'<span class="badge info">идёт обход</span> с {_when(sched["running"]["started_at"])}'
                 f' ({TRIGGER_NAMES.get(sched["running"]["trigger"], sched["running"]["trigger"])})')
        can_run = False
    elif sched.get("requested"):
        state, can_run = '<span class="badge info">обход запрошен</span>', False
    else:
        state, can_run = '<span class="badge ok">ожидание</span>', True
    last_ok = next((r for r in runs if _run_state(r)[1] in ("ok", "warn")), None)

    # Резервная копия
    if backup is None:
        backup_card = card("Резервная копия", '<span class="badge warn">ещё не было</span>',
                           "делается раз в сутки ночью; сейчас — make backup")
    elif not backup.get("ok"):
        backup_card = card("Резервная копия", '<span class="badge bad">ошибка</span>',
                           f'{_when(backup.get("finished_at"))}: {escape(str(backup.get("error") or ""))[:200]}')
    else:
        stale = datetime.now(timezone.utc) - datetime.fromisoformat(backup["finished_at"]) > \
            timedelta(hours=settings.BACKUP_STALE_H)
        backup_card = card("Резервная копия", (f'<span class="badge warn">устарела</span> ' if stale else "")
                           + _when(backup["finished_at"]),
                           f'{_size(backup.get("size_bytes"))}, восстановление проверено '
                           f'({_num(backup.get("listings"))} объявл.); копий: {backup.get("files", 0)}')
    telegram_on = bool(settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID)
    telegram_card = card(
        "Алерты в Telegram",
        '<span class="badge ok">настроены</span>' if telegram_on else '<span class="badge warn">не настроены</span>',
        '<form method="post" action="/admin/telegram-test" class="inline"><button class="link" type="submit">'
        'Отправить тестовое</button></form>' if telegram_on
        else "отчёты и алерты пишутся только в лог; настройка — docs/LOCAL_RUN.md")

    # Проблемы: находки здоровья сбора и состояние планировщика
    problems = []
    if sched is None:
        problems.append(("critical", None, "Планировщик не отвечает: ежедневные обходы не запустятся. "
                                           "Проверьте make status и make logs-scheduler.", None))
    else:
        last = sched.get("last") or {}
        failed = [code for code in last.get("exit_codes") or [] if code != 0]
        if failed:
            problems.append(("critical", last.get("finished_at"),
                             f"Обход {_when(last.get('started_at'))} завершился с ошибкой (код {failed[0]}): "
                             f"см. make logs-scheduler", None))
        interrupted = sched.get("interrupted")
        if interrupted and not sched.get("running"):
            problems.append(("warning", interrupted.get("started_at"),
                             f"Обход {_when(interrupted.get('started_at'))} прервался (компьютер выключили или "
                             f"остановили Docker) — он будет запущен заново при старте планировщика", None))
    if backup is not None and not backup.get("ok"):
        problems.append(("critical", backup.get("finished_at"), "Резервная копия не сделана: "
                         + str(backup.get("error") or ""), None))
    for anomaly in open_problems:
        problems.append((anomaly.severity, anomaly.detected_on, anomaly.message, anomaly.id))

    cards = [
        card("Планировщик", state, escape(", ".join(sched["spiders"])) if sched else ""),
        card("Следующий запуск", _when(sched.get("next_run_at")) if sched else "—",
             f"ежедневно в {escape(sched['schedule'])}" if sched else ""),
        card("Последний успешный", _when(last_ok.started_at) if last_ok else "—",
             f"собрано {_num((last_ok.report or {}).get('collected'))}" if last_ok else "запусков ещё не было"),
        card("Активных объявлений", _num(sum(count for _, count in active)),
             escape(", ".join(f"{CATEGORY_NAMES.get(c, c)}: {_num(n)}" for c, n in active)) or "нет"),
        backup_card,
        telegram_card,
    ]
    severity_badge = {"critical": ("bad", "критично"), "warning": ("warn", "внимание"), "info": ("info", "инфо")}
    problem_rows = []
    for severity, when, message, anomaly_id in problems:
        cls, label = severity_badge.get(severity, ("warn", severity))
        when_text = when.strftime("%d.%m.%Y") if hasattr(when, "strftime") and not isinstance(when, datetime) \
            else _when(when)
        action = (f'<form method="post" action="/admin/problems/{anomaly_id}/resolve" class="inline">'
                  f'<button class="link" type="submit">Решено</button></form>') if anomaly_id else ""
        problem_rows.append(f'<tr><td><span class="badge {cls}">{label}</span></td><td>{when_text}</td>'
                            f'<td class="wrap">{escape(str(message))}</td><td>{action}</td></tr>')
    problems_block = (
        '<h2>Проблемы сбора</h2><div class="table"><table class="problems"><tbody>' + "".join(problem_rows)
        + '</tbody></table></div><p class="muted">«Решено» скрывает находку; если условие повторится, '
          'она появится снова.</p>') if problem_rows else \
        '<div class="notice ok">Проблем со сбором нет.</div>'
    if resolved_problems:
        resolved_rows = "".join(
            f'<tr><td><span class="badge {severity_badge.get(a.severity, ("warn", a.severity))[0]}">'
            f'{severity_badge.get(a.severity, ("warn", a.severity))[1]}</span></td>'
            f'<td>{a.detected_on.strftime("%d.%m.%Y")}</td><td class="wrap">{escape(a.message)}</td>'
            f'<td>решено {_when(a.status_changed_at)}</td>'
            f'<td><form method="post" action="/admin/problems/{a.id}/reopen" class="inline">'
            f'<button class="link" type="submit">Вернуть</button></form></td></tr>'
            for a in resolved_problems)
        problems_block += (f'<details class="resolved"><summary>Решённые за {PROBLEM_DAYS} дней '
                           f'({len(resolved_problems)})</summary><div class="table"><table class="problems"><tbody>'
                           f'{resolved_rows}</tbody></table></div></details>')

    notice = ""
    if msg:
        notice = f'<div class="notice {"ok" if ok else "bad"}">{escape(msg)}</div>'

    rows = []
    for run in runs:
        shards = shards_by_run[run.id]
        summary = _run_summary(run, shards)
        text, cls = _run_state(run)
        warnings = len(summary["warnings"])
        rows.append(
            f'<tr><td><a href="/admin/runs/{escape(run.id)}">{_when(run.started_at)}</a></td>'
            f'<td>{escape(run.source)}</td><td>{escape(_categories(shards))}</td>'
            f'<td><span class="badge {cls}">{escape(text)}</span></td>'
            f'<td class="num">{_duration(summary["duration"])}</td>'
            f'<td class="num">{escape(summary["shards"])}</td>'
            f'<td class="num">{_num(summary["collected"])} / {_num(summary["expected"])}</td>'
            f'<td class="num">{_pct(summary["completeness"])}</td>'
            f'<td class="num">{_num(summary["new"])}</td><td class="num">{_num(summary["delisted"])}</td>'
            f'<td class="num">{warnings or "—"}</td></tr>')
    table = ('<div class="table"><table><thead><tr><th>Начало</th><th>Площадка</th><th>Раздел</th><th>Итог</th>'
             '<th class="num">Длительность</th><th class="num">Полных шардов</th>'
             '<th class="num">Собрано / ожидалось</th><th class="num">Полнота</th><th class="num">Новых</th>'
             '<th class="num">Снято</th><th class="num">Предупр.</th></tr></thead><tbody>'
             + ("".join(rows) or '<tr><td colspan="11" class="muted">Запусков ещё не было</td></tr>')
             + "</tbody></table></div>")

    disabled = "" if can_run else " disabled"
    body = f"""
<div class="bar"><div><h1>Сбор данных</h1>
<div class="muted">Время — {escape(settings.CRAWL_TZ)}. Страница обновляется сама, пока идёт обход.</div></div>
<form method="post" action="/admin/run"><button type="submit"{disabled}>Запустить сейчас</button></form></div>
{notice}<div class="cards">{"".join(cards)}</div>
{problems_block}
<h2>Запуски</h2>{table}
<p class="muted">Последние {RUNS_ON_PAGE} запусков. Подробности — по ссылке на время начала.</p>"""
    running = bool(sched and (sched.get("running") or sched.get("requested"))) or \
        any(run.status != "finished" for run in runs)
    return page("Сбор данных — EuroAutoDataHub", body, refresh=running)


@router.get("/runs/{run_id}", response_class=HTMLResponse)
async def admin_run(run_id: str, session: AsyncSession = Depends(get_session)):
    run = await session.get(CrawlRun, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Запуск не найден")
    shards = (await session.execute(
        select(CrawlShard).where(CrawlShard.run_id == run_id).order_by(CrawlShard.started_at))).scalars().all()
    summary = _run_summary(run, shards)
    report = run.report or {}
    text, cls = _run_state(run)

    cards = "".join([
        card("Итог", f'<span class="badge {cls}">{escape(text)}</span>',
             f"{_when(run.started_at)} — {_when(run.finished_at)}"),
        card("Длительность", _duration(summary["duration"])),
        card("Собрано / ожидалось", f'{_num(summary["collected"])} / {_num(summary["expected"])}',
             f'полнота {_pct(summary["completeness"])}'),
        card("Полных шардов", escape(summary["shards"])),
        card("Новых / снято", f'{_num(summary["new"])} / {_num(summary["delisted"])}'),
    ])
    blocks = []
    if summary["warnings"]:
        blocks.append('<h2>Предупреждения</h2><div class="card"><ul class="warnings">'
                      + "".join(f"<li>{escape(str(w))}</li>" for w in summary["warnings"]) + "</ul></div>")
    if report.get("anomalies"):
        blocks.append('<h2>Аномалии сбора</h2><div class="card"><ul class="warnings">'
                      + "".join(f"<li>{escape(str(a))}</li>" for a in report["anomalies"]) + "</ul></div>")
    fill = report.get("fill_rates") or {}
    stats = report.get("spider_stats") or {}
    if fill or stats:
        errors = {key: stats.get(key) for key in ("forbidden_403", "pauses", "http_errors", "graphql_errors",
                                                  "json_decode_errors", "missing_data_errors", "filter_mismatch")
                  if key in stats}
        blocks.append(
            '<h2>Качество</h2><div class="cards">'
            + card("Заполненность полей", _pct(min(fill.values())) if fill else "—",
                   "минимум; " + ", ".join(f"{escape(k)} {_pct(v)}" for k, v in fill.items()))
            + card("Ошибки паука", _num(sum(v or 0 for v in errors.values())),
                   ", ".join(f"{escape(k)}={v}" for k, v in errors.items()))
            + "</div>")

    rows = "".join(
        f'<tr><td><code>{escape(s.shard_key)}</code></td>'
        f'<td><span class="badge {"ok" if s.complete else "bad"}">{"полный" if s.complete else "неполный"}</span></td>'
        f'<td class="num">{_num(s.collected_count)} / {_num(s.expected_count)}</td>'
        f'<td class="num">{s.pages_total}{f" ({s.pages_failed} неудачн.)" if s.pages_failed else ""}</td>'
        f'<td class="num">{_duration((s.finished_at - s.started_at).total_seconds())}</td>'
        f'<td>{escape(s.lifecycle_status)}</td><td class="num">{_num(s.missed_count)}</td></tr>'
        for s in shards)
    shards_table = ('<h2>Шарды</h2><div class="table"><table><thead><tr><th>Шард</th><th>Статус</th>'
                    '<th class="num">Собрано / ожидалось</th><th class="num">Страниц</th>'
                    '<th class="num">Время</th><th>Снятия</th><th class="num">Снято</th></tr></thead><tbody>'
                    + (rows or '<tr><td colspan="7" class="muted">Шардов ещё нет</td></tr>')
                    + "</tbody></table></div>")
    body = f"""<p><a href="/admin">← Все запуски</a></p>
<h1>Запуск {escape(run.source)} · {escape(_categories(shards))}</h1>
<div class="muted"><code>{escape(run.id)}</code></div>
<div class="cards">{cards}</div>{"".join(blocks)}{shards_table}"""
    return page(f"Запуск {_when(run.started_at)} — EuroAutoDataHub", body, refresh=run.status != "finished")


def _same_origin(request: Request) -> bool:
    """Защита кнопки от отправки формы с чужого сайта: Origin или Referer — этот же хост."""
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source:
        return True
    return urlparse(source).netloc == request.headers.get("host")


@router.post("/run")
async def admin_start_run(request: Request):
    if not _same_origin(request):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Запрос не со страницы админки")
    ok, message = await scheduler_run()
    return RedirectResponse(f"/admin?msg={quote(message)}&ok={str(ok).lower()}", status.HTTP_303_SEE_OTHER)


@router.post("/problems/{anomaly_id}/resolve")
async def admin_resolve_problem(anomaly_id: int, request: Request, session: AsyncSession = Depends(get_session)):
    if not _same_origin(request):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Запрос не со страницы админки")
    anomaly = await session.get(Anomaly, anomaly_id)
    if anomaly is None or anomaly.kind not in PROBLEM_KINDS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Находка не найдена")
    anomaly.status = AnomalyStatus.RESOLVED.value
    anomaly.status_changed_at = datetime.now(timezone.utc)
    await session.commit()
    message = "Находка отмечена как решённая. Вернуть — в списке «Решённые» под проблемами"
    return RedirectResponse(f"/admin?msg={quote(message)}&ok=true", status.HTTP_303_SEE_OTHER)


@router.post("/problems/{anomaly_id}/reopen")
async def admin_reopen_problem(anomaly_id: int, request: Request, session: AsyncSession = Depends(get_session)):
    """Отменить «Решено»: находка снова в «Проблемах сбора»."""
    if not _same_origin(request):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Запрос не со страницы админки")
    anomaly = await session.get(Anomaly, anomaly_id)
    if anomaly is None or anomaly.kind not in PROBLEM_KINDS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Находка не найдена")
    anomaly.status = AnomalyStatus.NEW.value
    anomaly.status_changed_at = datetime.now(timezone.utc)
    await session.commit()
    return RedirectResponse(f"/admin?msg={quote('Находка возвращена в проблемы сбора')}&ok=true",
                            status.HTTP_303_SEE_OTHER)


@router.post("/telegram-test")
async def admin_telegram_test(request: Request):
    if not _same_origin(request):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Запрос не со страницы админки")
    ok, message = await telegram_test()
    return RedirectResponse(f"/admin?msg={quote(message)}&ok={str(ok).lower()}", status.HTTP_303_SEE_OTHER)
