"""Трекер обновлений сборок Dota 2 (аналог DOTA_DM_SPAM / DOTA2_WEB).

Один проход:
    1. Опрос Steam Web API (IGCVersion_*) для каждой цели из config.TRACKED_APPS.
    2. Сравнение result.active_version с сохранённым в steam_api/versions/.
    3. Первый запуск: только baseline-файлы, без истории и уведомлений.
    4. Обновление: перезапись JSON + запись в _HISTORY_by_date.txt / _HISTORY_by_name.txt.
    5. При любых изменениях: git add -> commit -> push, затем уведомления:
       Discord (embed + файл) и/или Telegram (документ + HTML-подпись).
       Оба канала опциональны и независимы; пауза 1.5 с между отправками.

DRY_RUN=true выполняет шаги 1-4 локально, пропуская git и уведомления.
Коды выхода: 0 — успех / нет изменений; 1 — ошибка git или уведомлений.
"""

import json
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

import config

BASE_DIR = Path(__file__).resolve().parent
STEAM_API_DIR = BASE_DIR / "steam_api"
VERSIONS_DIR = STEAM_API_DIR / "versions"
HISTORY_BY_DATE_FILE = STEAM_API_DIR / "_HISTORY_by_date.txt"
HISTORY_BY_NAME_FILE = STEAM_API_DIR / "_HISTORY_by_name.txt"

MSK_TZ = timezone(timedelta(hours=config.MSK_UTC_OFFSET_HOURS))
RETRYABLE_HTTP_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
TRUTHY_VALUES = frozenset({"1", "true", "yes", "on"})


def msk_timestamp() -> str:
    """Текущее московское время (UTC+3) в формате YYYY-MM-DD HH:MM:SS."""
    return datetime.now(MSK_TZ).strftime("%Y-%m-%d %H:%M:%S")


def log(message: str) -> None:
    """Лог с московским временем; flush=True — для логов GitHub Actions."""
    print(f"[{msk_timestamp()}] {message}", flush=True)


def is_dry_run() -> bool:
    return os.getenv("DRY_RUN", "").strip().lower() in TRUTHY_VALUES


def ensure_storage() -> None:
    """Создаёт steam_api/versions/ и оба файла истории, если их ещё нет."""
    VERSIONS_DIR.mkdir(parents=True, exist_ok=True)
    for history_file in (HISTORY_BY_DATE_FILE, HISTORY_BY_NAME_FILE):
        if not history_file.exists():
            history_file.touch()


def version_file_path(appid: int, name: str) -> Path:
    return VERSIONS_DIR / f"{appid} - {name}.json"


def atomic_write_text(path: Path, text: str) -> None:
    """Запись через временный файл + os.replace: файл не останется полузаписанным."""
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp_path, path)


def retry_after_seconds(response: requests.Response, default: float, cap: float) -> float:
    """Извлекает Retry-After (в секундах), ограничивая ожидание сверху."""
    raw = response.headers.get("Retry-After", "").strip()
    if raw:
        try:
            return min(max(float(raw), 0.0), cap)
        except ValueError:
            return default
    return default


def fetch_steam_payload(session: requests.Session, appid: int, app_cfg: dict) -> dict | None:
    """GET к Steam API: 3 попытки, таймаут 10 с, backoff на 429/5xx и сетевых ошибках."""
    url = config.STEAM_API_URL_TEMPLATE.format(
        gc_appid=app_cfg["gc_appid"], method=app_cfg["method"], appid=appid
    )
    attempts = config.STEAM_MAX_ATTEMPTS
    for attempt in range(1, attempts + 1):
        try:
            response = session.get(url, timeout=config.STEAM_TIMEOUT_SECONDS)
        except (requests.Timeout, requests.ConnectionError) as exc:
            log(f"[{appid}] сетевая ошибка {type(exc).__name__} (попытка {attempt}/{attempts})")
            if attempt < attempts:
                time.sleep(config.RETRY_BACKOFF_SECONDS * attempt)
            continue

        if response.status_code in RETRYABLE_HTTP_STATUSES:
            delay = retry_after_seconds(
                response,
                config.RETRY_BACKOFF_SECONDS * attempt,
                config.STEAM_RETRY_SLEEP_CAP_SECONDS,
            )
            log(f"[{appid}] HTTP {response.status_code} (попытка {attempt}/{attempts}), пауза {delay:.1f} с")
            if attempt < attempts:
                time.sleep(delay)
            continue

        if response.status_code != 200:
            log(f"[{appid}] HTTP {response.status_code}: {response.text[:200]!r} — цель пропущена")
            return None

        try:
            payload = response.json()
        except ValueError:
            log(f"[{appid}] ответ не является JSON (попытка {attempt}/{attempts})")
            if attempt < attempts:
                time.sleep(config.RETRY_BACKOFF_SECONDS * attempt)
            continue

        result = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(result, dict):
            log(f"[{appid}] в ответе нет объекта 'result' — цель пропущена")
            return None
        if result.get("success") is not True:
            log(f"[{appid}] result.success != true — цель пропущена")
            return None
        if "active_version" not in result:
            log(f"[{appid}] в result отсутствует 'active_version' — цель пропущена")
            return None
        return payload

    log(f"[{appid}] все {attempts} попытки исчерпаны — цель пропущена")
    return None


def read_saved_payload(path: Path) -> dict | None:
    """Читает сохранённый JSON; битый файл трактуем как отсутствие baseline."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log(f"Файл {path.name} повреждён ({exc}); будет перезаписан как baseline")
        return None
    return payload if isinstance(payload, dict) else None


def extract_active_version(payload: dict | None) -> int | str | None:
    """Достаёт result.active_version из сохранённого/свежего ответа."""
    if not isinstance(payload, dict):
        return None
    result = payload.get("result")
    if not isinstance(result, dict):
        return None
    return result.get("active_version")


def save_payload(path: Path, payload: dict) -> None:
    atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def append_history_by_date(line: str) -> None:
    """Дописывает строку в конец _HISTORY_by_date.txt (UTF-8, LF)."""
    with HISTORY_BY_DATE_FILE.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(line + "\n")


def insert_history_by_name(line: str, prefix: str) -> None:
    """Вставляет строку после последней строки с prefix; иначе — в конец."""
    lines = HISTORY_BY_NAME_FILE.read_text(encoding="utf-8").splitlines()
    insert_at = len(lines)
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].startswith(prefix):
            insert_at = index + 1
            break
    lines.insert(insert_at, line)
    atomic_write_text(HISTORY_BY_NAME_FILE, "\n".join(lines) + "\n")


def run_git(*args: str) -> str:
    """subprocess.run(..., check=True); GIT_TERMINAL_PROMPT=0 — не зависать на логине."""
    completed = subprocess.run(
        ["git", *args],
        cwd=BASE_DIR,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    return completed.stdout.strip()


def git_commit_and_push(changed_files: int) -> str:
    """add -> commit -> push; возвращает хеш коммита (git rev-parse HEAD)."""
    run_git("add", "steam_api/")
    run_git("commit", "-m", f"steam_api | {changed_files} files")
    run_git("push")
    return run_git("rev-parse", "HEAD")


def build_embed(update: dict, commit_url: str) -> dict:
    """Embed для Discord (формат боевого уведомления)."""
    appid = update["appid"]
    color = config.EMBED_COLOR_MAIN if appid in config.MAIN_APP_IDS else config.EMBED_COLOR_TEST
    description = (
        f"Версия: **{update['old_version']}** ➔ **{update['new_version']}**\n"
        f"Время: `{update['timestamp']} (UTC+3)`\n"
        f"[Открыть diff на GitHub]({commit_url})"
    )
    return {
        "title": f"Обновление: {update['name']} ({appid})",
        "description": description,
        "color": color,
    }


def send_discord_update(
    session: requests.Session, webhook_url: str, update: dict, commit_url: str
) -> bool:
    """POST multipart/form-data: payload_json (embed) + file (JSON версии)."""
    appid = update["appid"]
    json_path = version_file_path(appid, update["name"])
    payload_json = json.dumps(
        {"embeds": [build_embed(update, commit_url)]}, ensure_ascii=False
    ).encode("utf-8")
    file_bytes = json_path.read_bytes()
    attempts = config.DISCORD_MAX_ATTEMPTS

    for attempt in range(1, attempts + 1):
        files = {
            "payload_json": (None, payload_json, "application/json"),
            "file": (json_path.name, file_bytes, "application/json"),
        }
        try:
            response = session.post(webhook_url, files=files, timeout=config.DISCORD_TIMEOUT_SECONDS)
        except (requests.Timeout, requests.ConnectionError) as exc:
            log(f"[{appid}] Discord: сетевая ошибка {type(exc).__name__} (попытка {attempt}/{attempts})")
            if attempt < attempts:
                time.sleep(config.RETRY_BACKOFF_SECONDS * attempt)
            continue

        if response.status_code in (200, 204):
            log(f"[{appid}] Discord: уведомление отправлено")
            return True

        if response.status_code == 429:
            delay = config.DISCORD_SEND_DELAY_SECONDS
            try:
                delay = float(response.json().get("retry_after", delay))
            except (ValueError, TypeError, AttributeError):
                delay = retry_after_seconds(response, delay, config.DISCORD_RETRY_SLEEP_CAP_SECONDS)
            delay = min(max(delay, 0.0) + 0.5, config.DISCORD_RETRY_SLEEP_CAP_SECONDS)
            log(f"[{appid}] Discord 429: пауза {delay:.1f} с (попытка {attempt}/{attempts})")
            if attempt < attempts:
                time.sleep(delay)
            continue

        if response.status_code in RETRYABLE_HTTP_STATUSES:
            log(f"[{appid}] Discord HTTP {response.status_code} (попытка {attempt}/{attempts})")
            if attempt < attempts:
                time.sleep(config.RETRY_BACKOFF_SECONDS * attempt)
            continue

        log(f"[{appid}] Discord HTTP {response.status_code}: {response.text[:300]!r}")
        return False

    log(f"[{appid}] Discord: все {attempts} попытки исчерпаны")
    return False


def tg_escape(value) -> str:
    """Минимальный HTML-escape для parse_mode=HTML (без сторонних модулей)."""
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_telegram_caption(update: dict, commit_url: str) -> str:
    """HTML-подпись к документу в Telegram (аналог embed в Discord)."""
    return (
        f"<b>Обновление: {tg_escape(update['name'])} ({update['appid']})</b>\n"
        f"Версия: <b>{tg_escape(update['old_version'])}</b> ➔ "
        f"<b>{tg_escape(update['new_version'])}</b>\n"
        f"Время: <code>{tg_escape(update['timestamp'])} (UTC+3)</code>\n"
        f'<a href="{tg_escape(commit_url)}">Открыть diff на GitHub</a>'
    )


def send_telegram_update(
    session: requests.Session, bot_token: str, chat_id: str, update: dict, commit_url: str
) -> bool:
    """sendDocument: JSON-файл версии + HTML-подпись. 3 попытки, учёт retry_after."""
    appid = update["appid"]
    json_path = version_file_path(appid, update["name"])
    file_bytes = json_path.read_bytes()
    caption = build_telegram_caption(update, commit_url)
    url = config.TELEGRAM_API_URL_TEMPLATE.format(token=bot_token, method="sendDocument")
    attempts = config.TELEGRAM_MAX_ATTEMPTS

    for attempt in range(1, attempts + 1):
        try:
            response = session.post(
                url,
                data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
                files={"document": (json_path.name, file_bytes, "application/json")},
                timeout=config.TELEGRAM_TIMEOUT_SECONDS,
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            log(f"[{appid}] Telegram: сетевая ошибка {type(exc).__name__} (попытка {attempt}/{attempts})")
            if attempt < attempts:
                time.sleep(config.RETRY_BACKOFF_SECONDS * attempt)
            continue

        try:
            body = response.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}

        if response.status_code == 200 and body.get("ok") is True:
            log(f"[{appid}] Telegram: уведомление отправлено")
            return True

        error_code = body.get("error_code")
        description = body.get("description") or response.text[:200]

        if response.status_code == 429 or error_code == 429:
            delay = config.TELEGRAM_SEND_DELAY_SECONDS
            parameters = body.get("parameters")
            if isinstance(parameters, dict):
                try:
                    delay = float(parameters.get("retry_after", delay))
                except (TypeError, ValueError):
                    pass
            delay = min(max(delay, 0.0) + 0.5, config.TELEGRAM_RETRY_SLEEP_CAP_SECONDS)
            log(f"[{appid}] Telegram 429: пауза {delay:.1f} с (попытка {attempt}/{attempts})")
            if attempt < attempts:
                time.sleep(delay)
            continue

        if response.status_code in RETRYABLE_HTTP_STATUSES or (
            isinstance(error_code, int) and error_code >= 500
        ):
            log(f"[{appid}] Telegram HTTP {response.status_code} (попытка {attempt}/{attempts})")
            if attempt < attempts:
                time.sleep(config.RETRY_BACKOFF_SECONDS * attempt)
            continue

        if response.status_code == 401:
            log(f"[{appid}] Telegram 401: неверный TELEGRAM_BOT_TOKEN ({description})")
        elif response.status_code == 403:
            log(f"[{appid}] Telegram 403: бот не админ канала или нет права Post messages ({description})")
        elif response.status_code == 400:
            log(f"[{appid}] Telegram 400: проверьте TELEGRAM_CHAT_ID ({description})")
        else:
            log(f"[{appid}] Telegram HTTP {response.status_code}: {description}")
        return False

    log(f"[{appid}] Telegram: все {attempts} попытки исчерпаны")
    return False


def collect_updates(session: requests.Session) -> tuple[list[dict], int]:
    """Опрашивает цели, пишет файлы и историю.

    Возвращает (список обновлений, число изменённых файлов):
    baseline = 1 файл, обновление = 3 файла (JSON + две истории).
    """
    updates: list[dict] = []
    changed_files = 0
    app_items = list(config.TRACKED_APPS.items())

    for index, (appid, app_cfg) in enumerate(app_items):
        if index > 0:
            time.sleep(config.STEAM_POLITE_DELAY_SECONDS)
        name = app_cfg["name"]
        payload = fetch_steam_payload(session, appid, app_cfg)
        if payload is None:
            continue

        new_version = extract_active_version(payload)
        path = version_file_path(appid, name)

        if not path.exists():
            save_payload(path, payload)
            changed_files += 1
            log(f"[{appid}] {name}: первый запуск — baseline v{new_version} сохранён, без уведомлений")
            continue

        old_version = extract_active_version(read_saved_payload(path))
        if old_version is None:
            save_payload(path, payload)
            changed_files += 1
            log(f"[{appid}] {name}: baseline v{new_version} восстановлен, без уведомлений")
            continue

        if new_version == old_version:
            log(f"[{appid}] {name}: без изменений (v{new_version})")
            continue

        timestamp = msk_timestamp()
        save_payload(path, payload)
        line = f"{appid} - {name} | [v] {old_version} => {new_version} | {timestamp} (UTC+3)"
        append_history_by_date(line)
        insert_history_by_name(line, f"{appid} - {name} |")
        changed_files += 3
        updates.append(
            {
                "appid": appid,
                "name": name,
                "old_version": old_version,
                "new_version": new_version,
                "timestamp": timestamp,
            }
        )
        log(f"[{appid}] {name}: v{old_version} => v{new_version}")

    return updates, changed_files


def main() -> int:
    load_dotenv(BASE_DIR / ".env")
    dry_run = is_dry_run()
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    repo_url = os.getenv("GITHUB_REPO_URL", "").strip()
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    telegram_ready = bool(telegram_token and telegram_chat_id)

    log(f"Трекер запущен. DRY_RUN={'true' if dry_run else 'false'}")
    if not dry_run and not webhook_url and not telegram_ready:
        log("ВНИМАНИЕ: не настроен ни Discord, ни Telegram — уведомления будут пропущены")
    if not dry_run and bool(telegram_token) != bool(telegram_chat_id):
        log("ВНИМАНИЕ: для Telegram нужны ОБА параметра: TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID")
    if not dry_run and not repo_url:
        log("ВНИМАНИЕ: GITHUB_REPO_URL не задан — ссылка на коммит будет некорректной")

    ensure_storage()

    with requests.Session() as session:
        session.headers.update({"User-Agent": config.USER_AGENT})
        updates, changed_files = collect_updates(session)

        if changed_files == 0:
            log("Изменений нет — выходим")
            return 0

        log(f"Изменено файлов: {changed_files}; обновлений сборок: {len(updates)}")

        if dry_run:
            log("[DRY_RUN] git add/commit/push пропущены")
            for update in updates:
                log(
                    f"[DRY_RUN] уведомления пропущены: {update['appid']} - {update['name']} "
                    f"v{update['old_version']} => v{update['new_version']}"
                )
            return 0

        try:
            commit_hash = git_commit_and_push(changed_files)
        except subprocess.CalledProcessError as exc:
            log(f"Git: команда {' '.join(exc.cmd)} завершилась с кодом {exc.returncode}")
            if exc.stderr:
                log(f"Git stderr: {exc.stderr.strip()}")
            return 1
        log(f"Git: коммит {commit_hash} отправлен в remote")
        commit_url = f"{repo_url.rstrip('/')}/commit/{commit_hash}"

        if not updates:
            log("Сохранены только baseline-файлы — уведомления не требуются")
            return 0
        if not webhook_url and not telegram_ready:
            log("ОШИБКА: есть обновления, но не настроен ни Discord, ни Telegram")
            return 1

        all_sent = True

        if webhook_url:
            for index, update in enumerate(updates):
                if index > 0:
                    time.sleep(config.DISCORD_SEND_DELAY_SECONDS)
                if not send_discord_update(session, webhook_url, update, commit_url):
                    all_sent = False
        else:
            log("Discord не настроен — уведомления пропущены")

        if telegram_ready:
            for index, update in enumerate(updates):
                if index > 0:
                    time.sleep(config.TELEGRAM_SEND_DELAY_SECONDS)
                if not send_telegram_update(
                    session, telegram_token, telegram_chat_id, update, commit_url
                ):
                    all_sent = False
        else:
            log("Telegram не настроен — уведомления пропущены")

        return 0 if all_sent else 1


if __name__ == "__main__":
    raise SystemExit(main())