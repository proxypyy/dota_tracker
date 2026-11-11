"""test_send.py — интерактивная проверка трекера сборок Dota 2.

Запуск:
    python test_send.py            # интерактивное меню
    echo 8 | python test_send.py   # все проверки без вопросов (код выхода 0/1)

Проверки:
    1 — Steam API: текущие версии всех целей (боевой код из tracker.py).
    2 — Discord: текстовое сообщение в webhook.
    3 — Discord: тестовый embed + JSON-файл (формат уведомления, [ТЕСТ]).
    4 — Telegram: текстовое сообщение в канал.
    5 — Telegram: документ + HTML-подпись (формат уведомления, [ТЕСТ]).
    6 — Telegram: определить chat_id канала через getUpdates.
    7 — Git: репозиторий, user.name/email, remote, право на push (push --dry-run).
    8 — все проверки подряд + сводка (ненастроенные каналы — SKIP).

Побочные эффекты: отсутствуют, кроме того, что проверки 3/5 могут создать
baseline-файл цели, если его ещё нет (точно как первый запуск трекера).
DRY_RUN на этот скрипт не влияет: его задача — реально отправлять тесты.
"""

import json
import os
import subprocess
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

import config
import tracker

BASE_DIR = Path(__file__).resolve().parent
SEP = "-" * 62


def ask(prompt: str, default: str = "") -> str:
    """input() с обработкой EOF: позволяет запускать `echo 8 | python test_send.py`."""
    try:
        value = input(prompt).strip()
    except EOFError:
        print()
        return default
    return value or default


def webhook_url() -> str:
    return os.getenv("DISCORD_WEBHOOK_URL", "").strip()


def repo_url() -> str:
    return os.getenv("GITHUB_REPO_URL", "").strip()


def telegram_token() -> str:
    return os.getenv("TELEGRAM_BOT_TOKEN", "").strip()


def telegram_chat_id() -> str:
    return os.getenv("TELEGRAM_CHAT_ID", "").strip()


def require_webhook() -> str | None:
    url = webhook_url()
    if not url:
        print("FAIL: DISCORD_WEBHOOK_URL не задан (.env или переменная окружения).")
        print("      Discord: Server Settings -> Integrations -> Webhooks -> Copy URL.")
        return None
    if "/api/webhooks/" not in url:
        print("WARNING: URL не похож на Discord webhook, пробую всё равно...")
    return url


def require_telegram() -> tuple[str, str] | None:
    token = telegram_token()
    chat_id = telegram_chat_id()
    if not token or not chat_id:
        print("FAIL: Telegram не настроен: нужны TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID (.env).")
        print("      Бот: @BotFather -> /newbot. Канал: добавьте бота админом (право Post messages).")
        print("      chat_id: публичный канал — @username; приватный — пункт 6 этого скрипта.")
        return None
    return token, chat_id


def explain_discord_error(response: requests.Response) -> None:
    code = response.status_code
    if code in (401, 404):
        print(f"FAIL: HTTP {code} — вебхук удалён или URL неверный. Создайте новый и обновите .env/secret.")
    elif code == 400:
        print(f"FAIL: HTTP 400 — Discord отклонил сообщение: {response.text[:300]!r}")
    else:
        print(f"FAIL: HTTP {code}: {response.text[:300]!r}")


def post_with_retry(send) -> bool:
    """Вызывает send() -> Response; 3 попытки, обработка 429/5xx/таймаутов (Discord)."""
    attempts = config.DISCORD_MAX_ATTEMPTS
    for attempt in range(1, attempts + 1):
        try:
            response = send()
        except (requests.Timeout, requests.ConnectionError) as exc:
            print(f"Сетевая ошибка {type(exc).__name__} (попытка {attempt}/{attempts})")
            if attempt < attempts:
                time.sleep(config.RETRY_BACKOFF_SECONDS * attempt)
            continue

        if response.status_code in (200, 204):
            return True

        if response.status_code == 429:
            delay = config.DISCORD_SEND_DELAY_SECONDS
            try:
                delay = float(response.json().get("retry_after", delay))
            except (ValueError, TypeError, AttributeError):
                delay = tracker.retry_after_seconds(response, delay, config.DISCORD_RETRY_SLEEP_CAP_SECONDS)
            delay = min(max(delay, 0.0) + 0.5, config.DISCORD_RETRY_SLEEP_CAP_SECONDS)
            print(f"HTTP 429: пауза {delay:.1f} с (попытка {attempt}/{attempts})")
            if attempt < attempts:
                time.sleep(delay)
            continue

        if response.status_code in tracker.RETRYABLE_HTTP_STATUSES:
            print(f"HTTP {response.status_code} (попытка {attempt}/{attempts})")
            if attempt < attempts:
                time.sleep(config.RETRY_BACKOFF_SECONDS * attempt)
            continue

        explain_discord_error(response)
        return False

    print(f"FAIL: все {attempts} попытки исчерпаны.")
    return False


def tg_post_with_retry(send) -> bool:
    """Вызывает send() -> Response; 3 попытки, обработка 429/5xx/таймаутов (Telegram)."""
    attempts = config.TELEGRAM_MAX_ATTEMPTS
    for attempt in range(1, attempts + 1):
        try:
            response = send()
        except (requests.Timeout, requests.ConnectionError) as exc:
            print(f"Сетевая ошибка {type(exc).__name__} (попытка {attempt}/{attempts})")
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
            print(f"HTTP 429: пауза {delay:.1f} с (попытка {attempt}/{attempts})")
            if attempt < attempts:
                time.sleep(delay)
            continue

        if response.status_code in tracker.RETRYABLE_HTTP_STATUSES or (
            isinstance(error_code, int) and error_code >= 500
        ):
            print(f"HTTP {response.status_code} (попытка {attempt}/{attempts})")
            if attempt < attempts:
                time.sleep(config.RETRY_BACKOFF_SECONDS * attempt)
            continue

        if response.status_code == 401:
            print("FAIL: 401 — неверный TELEGRAM_BOT_TOKEN (BotFather -> /token).")
        elif response.status_code == 403:
            print(f"FAIL: 403 — бот не админ канала или нет права Post messages ({description}).")
        elif response.status_code == 400:
            print(f"FAIL: 400 — {description}. Проверьте TELEGRAM_CHAT_ID.")
        else:
            print(f"FAIL: HTTP {response.status_code}: {description}")
        return False

    print(f"FAIL: все {attempts} попытки исчерпаны.")
    return False


def git_run(*args: str) -> tuple[bool, str]:
    """git с таймаутом 30 с; возвращает (успех, stdout+stderr)."""
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=BASE_DIR,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except subprocess.CalledProcessError as exc:
        return False, ((exc.stderr or "") + (exc.stdout or "")).strip()
    except subprocess.TimeoutExpired:
        return False, "таймаут 30 с"
    except OSError as exc:
        return False, f"git не найден: {exc}"
    return True, completed.stdout.strip()


def resolve_appid(appid: int | None) -> int | None:
    """Спрашивает/проверяет appid из config.TRACKED_APPS."""
    if appid is None:
        raw = ask("appid (Enter = 570): ", "570")
        if not raw.isdigit():
            print("FAIL: appid должен быть числом.")
            return None
        appid = int(raw)
    if appid not in config.TRACKED_APPS:
        print(f"FAIL: appid {appid} нет в config.TRACKED_APPS.")
        print("Доступные: " + ", ".join(str(a) for a in config.TRACKED_APPS))
        return None
    return appid


def prepare_test_update(session: requests.Session, appid: int) -> tuple[dict, Path, str] | None:
    """Готовит тестовое 'обновление' (old = current - 1) и файл для вложения.

    Историю не трогает. Если baseline-файла нет — создаёт его из Steam API.
    Возвращает (update, путь к JSON, commit_url) либо None.
    """
    name = config.TRACKED_APPS[appid]["name"]
    tracker.ensure_storage()
    path = tracker.version_file_path(appid, name)

    payload = tracker.read_saved_payload(path) if path.exists() else None
    version = tracker.extract_active_version(payload)
    if version is None:
        print("Baseline-файла нет (или он повреждён) — запрашиваю версию у Steam API...")
        payload = tracker.fetch_steam_payload(session, appid, config.TRACKED_APPS[appid])
        version = tracker.extract_active_version(payload)
        if version is None:
            print("FAIL: Steam API недоступен, а файла для вложения нет.")
            return None
        tracker.save_payload(path, payload)
        print(f"Создан baseline-файл: {path.name}")

    if isinstance(version, int):
        old_version, new_version = version - 1, version
    else:
        old_version, new_version = "0", version

    git_ok, git_out = git_run("rev-parse", "HEAD")
    commit_hash = git_out if git_ok and git_out else "test"
    base_repo = repo_url()
    if base_repo:
        commit_url = f"{base_repo.rstrip('/')}/commit/{commit_hash}"
    else:
        commit_url = "https://github.com"
        print("WARNING: GITHUB_REPO_URL не задан — ссылка на diff будет заглушкой.")

    update = {
        "appid": appid,
        "name": name,
        "old_version": old_version,
        "new_version": new_version,
        "timestamp": tracker.msk_timestamp(),
    }
    return update, path, commit_url


def check_steam(session: requests.Session) -> bool:
    print(SEP)
    print("ПРОВЕРКА 1: Steam API (IGCVersion_*)")
    print(SEP)
    ok_count = 0
    items = list(config.TRACKED_APPS.items())
    for index, (appid, app_cfg) in enumerate(items):
        if index > 0:
            time.sleep(config.STEAM_POLITE_DELAY_SECONDS)
        url = config.STEAM_API_URL_TEMPLATE.format(
            gc_appid=app_cfg["gc_appid"], method=app_cfg["method"], appid=appid
        )
        print(f"\n{appid} - {app_cfg['name']}")
        print(f"  URL: {url}")
        payload = tracker.fetch_steam_payload(session, appid, app_cfg)
        version = tracker.extract_active_version(payload)
        if version is None:
            print("  FAIL: не удалось получить active_version (см. лог выше).")
        else:
            ok_count += 1
            print(f"  OK: active_version = {version}")
    print(f"\nИтог: {ok_count}/{len(items)} целей отвечают.")
    return ok_count == len(items)


def check_discord_text(session: requests.Session) -> bool:
    print(SEP)
    print("ПРОВЕРКА 2: Discord — текстовое сообщение")
    print(SEP)
    url = require_webhook()
    if url is None:
        return False
    content = (
        "Тест трекера Dota 2: вебхук работает. "
        f"Время: {tracker.msk_timestamp()} (UTC+3)"
    )

    def send():
        return session.post(url, json={"content": content}, timeout=config.DISCORD_TIMEOUT_SECONDS)

    ok = post_with_retry(send)
    print("OK: сообщение отправлено, проверьте канал Discord." if ok else "Сообщение не отправлено.")
    return ok


def check_discord_embed(session: requests.Session, appid: int | None = None) -> bool:
    print(SEP)
    print("ПРОВЕРКА 3: Discord — тестовый embed + JSON-файл")
    print(SEP)
    url = require_webhook()
    if url is None:
        return False
    appid = resolve_appid(appid)
    if appid is None:
        return False
    prepared = prepare_test_update(session, appid)
    if prepared is None:
        return False
    update, path, commit_url = prepared

    embed = tracker.build_embed(update, commit_url)
    embed["title"] = "[ТЕСТ] " + embed["title"]
    embed["description"] = (
        "Проверочное сообщение трекера — это НЕ реальное обновление.\n" + embed["description"]
    )
    payload_json = json.dumps({"embeds": [embed]}, ensure_ascii=False).encode("utf-8")
    file_bytes = path.read_bytes()

    def send():
        files = {
            "payload_json": (None, payload_json, "application/json"),
            "file": (path.name, file_bytes, "application/json"),
        }
        return session.post(url, files=files, timeout=config.DISCORD_TIMEOUT_SECONDS)

    ok = post_with_retry(send)
    print("OK: тестовое уведомление с файлом отправлено." if ok else "Уведомление не отправлено.")
    return ok


def check_telegram_text(session: requests.Session) -> bool:
    print(SEP)
    print("ПРОВЕРКА 4: Telegram — текстовое сообщение")
    print(SEP)
    creds = require_telegram()
    if creds is None:
        return False
    token, chat_id = creds
    url = config.TELEGRAM_API_URL_TEMPLATE.format(token=token, method="sendMessage")
    text = (
        "Тест трекера Dota 2: Telegram работает. "
        f"Время: {tracker.msk_timestamp()} (UTC+3)"
    )

    def send():
        return session.post(
            url,
            json={
                "chat_id": chat_id,
                "text": text,
                "link_preview_options": {"is_disabled": True},
            },
            timeout=config.TELEGRAM_TIMEOUT_SECONDS,
        )

    ok = tg_post_with_retry(send)
    print("OK: сообщение отправлено, проверьте канал Telegram." if ok else "Сообщение не отправлено.")
    return ok


def check_telegram_document(session: requests.Session, appid: int | None = None) -> bool:
    print(SEP)
    print("ПРОВЕРКА 5: Telegram — документ + HTML-подпись")
    print(SEP)
    creds = require_telegram()
    if creds is None:
        return False
    token, chat_id = creds
    appid = resolve_appid(appid)
    if appid is None:
        return False
    prepared = prepare_test_update(session, appid)
    if prepared is None:
        return False
    update, path, commit_url = prepared

    caption = (
        "[ТЕСТ] Проверочное сообщение трекера — это НЕ реальное обновление.\n"
        + tracker.build_telegram_caption(update, commit_url)
    )
    file_bytes = path.read_bytes()
    url = config.TELEGRAM_API_URL_TEMPLATE.format(token=token, method="sendDocument")

    def send():
        return session.post(
            url,
            data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
            files={"document": (path.name, file_bytes, "application/json")},
            timeout=config.TELEGRAM_TIMEOUT_SECONDS,
        )

    ok = tg_post_with_retry(send)
    print("OK: тестовый документ с подписью отправлен." if ok else "Документ не отправлен.")
    return ok


def check_telegram_chat_id(session: requests.Session) -> bool:
    print(SEP)
    print("ПРОВЕРКА 6: Telegram — определить chat_id (getUpdates)")
    print(SEP)
    token = telegram_token()
    if not token:
        print("FAIL: TELEGRAM_BOT_TOKEN не задан (.env). Создайте бота у @BotFather (/newbot).")
        return False
    url = config.TELEGRAM_API_URL_TEMPLATE.format(token=token, method="getUpdates")
    try:
        response = session.get(url, timeout=config.TELEGRAM_TIMEOUT_SECONDS)
    except (requests.Timeout, requests.ConnectionError) as exc:
        print(f"FAIL: сетевая ошибка {type(exc).__name__}.")
        return False
    try:
        body = response.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict) or body.get("ok") is not True:
        description = body.get("description") if isinstance(body, dict) else response.text[:200]
        print(f"FAIL: getUpdates HTTP {response.status_code}: {description}")
        if response.status_code == 401:
            print("      Неверный TELEGRAM_BOT_TOKEN.")
        return False

    chats: dict = {}
    for upd in body.get("result", []):
        if not isinstance(upd, dict):
            continue
        for key in ("message", "channel_post", "my_chat_member", "chat_member"):
            obj = upd.get(key)
            if isinstance(obj, dict):
                chat = obj.get("chat")
                if isinstance(chat, dict) and "id" in chat:
                    chats[chat["id"]] = chat

    if not chats:
        print("Чаты не найдены. Сделайте бота АДМИНОМ канала, опубликуйте любой пост")
        print("и повторите пункт 6 (или напишите боту в личку /start).")
        return False

    print("Найденные чаты:")
    for chat in chats.values():
        title = chat.get("title") or chat.get("username") or chat.get("first_name") or "?"
        username = chat.get("username")
        suffix = f" (@{username})" if username else ""
        print(f"  id={chat['id']}  type={chat.get('type')}  {title}{suffix}")
    print("Скопируйте id нужного канала в TELEGRAM_CHAT_ID.")
    print("Для публичного канала можно использовать @username вместо числового id.")
    return True


def check_git() -> bool:
    print(SEP)
    print("ПРОВЕРКА 7: Git (ничего не отправляется: push --dry-run)")
    print(SEP)
    ok, out = git_run("rev-parse", "--is-inside-work-tree")
    if not (ok and out == "true"):
        print("FAIL: это не git-репозиторий (git init).")
        return False
    print("OK: git-репозиторий найден.")
    all_ok = True

    ok, out = git_run("config", "user.name")
    if ok and out:
        print(f"OK: user.name = {out}")
    else:
        print("FAIL: git config user.name не задан.")
        all_ok = False

    ok, out = git_run("config", "user.email")
    if ok and out:
        print(f"OK: user.email = {out}")
    else:
        print("FAIL: git config user.email не задан.")
        all_ok = False

    ok, out = git_run("remote", "-v")
    if ok and out:
        print("OK: remote:")
        for line in out.splitlines():
            print("   " + line)
    else:
        print("FAIL: remote не настроен (git remote add origin <URL>).")
        all_ok = False

    ok, out = git_run("status", "--porcelain", "--", "steam_api/")
    if ok and out:
        print("NOTE: есть незакоммиченные изменения в steam_api/ (нормально после DRY_RUN).")

    ok, out = git_run("push", "--dry-run")
    if ok:
        print("OK: git push --dry-run прошёл — право на push есть (ничего не отправлено).")
    else:
        print("FAIL: git push --dry-run:")
        print("   " + (out.replace("\n", "\n   ") or "нет вывода"))
        print("   Подсказка: проверьте remote, токен/SSH-ключ и защиту ветки.")
        all_ok = False
    return all_ok


def run_all(session: requests.Session) -> bool:
    results: list[tuple[str, bool | None]] = []
    results.append(("Steam API", check_steam(session)))

    if webhook_url():
        results.append(("Discord текст", check_discord_text(session)))
        results.append(("Discord embed + файл", check_discord_embed(session, appid=570)))
    else:
        print("\nSKIP: Discord не настроен (DISCORD_WEBHOOK_URL не задан).")
        results.append(("Discord", None))

    if telegram_token() and telegram_chat_id():
        results.append(("Telegram текст", check_telegram_text(session)))
        results.append(("Telegram документ", check_telegram_document(session, appid=570)))
    else:
        print("\nSKIP: Telegram не настроен (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID).")
        results.append(("Telegram", None))

    results.append(("Git", check_git()))

    print("\n" + SEP)
    print("СВОДКА")
    print(SEP)
    for label, ok in results:
        mark = "SKIP" if ok is None else ("OK  " if ok else "FAIL")
        print(f"  {mark} — {label}")

    notifier_configured = bool(webhook_url()) or bool(telegram_token() and telegram_chat_id())
    if not notifier_configured:
        print("FAIL: не настроен ни один канал уведомлений.")
    executed = [ok for _, ok in results if ok is not None]
    return all(executed) and notifier_configured


def main() -> int:
    load_dotenv(BASE_DIR / ".env")
    print(SEP)
    print("Dota 2 Build Tracker — проверка настроек и тестовые запросы")
    print(SEP)
    print(f"DISCORD_WEBHOOK_URL: {'задан' if webhook_url() else 'НЕ задан'}")
    print(f"GITHUB_REPO_URL:     {repo_url() or 'НЕ задан'}")
    tg_ready = bool(telegram_token() and telegram_chat_id())
    print(f"TELEGRAM:            {'задан' if tg_ready else 'НЕ задан'}")
    print("DRY_RUN на этот скрипт не влияет: тесты отправляются реально.")
    with requests.Session() as session:
        session.headers.update({"User-Agent": config.USER_AGENT})
        while True:
            print("\n" + SEP)
            print("1 — Steam API: текущие версии всех целей")
            print("2 — Discord: тестовое текстовое сообщение")
            print("3 — Discord: тестовый embed + JSON-файл")
            print("4 — Telegram: тестовое текстовое сообщение")
            print("5 — Telegram: тестовый документ + подпись")
            print("6 — Telegram: определить chat_id (getUpdates)")
            print("7 — Git: репозиторий, identity, remote, право на push")
            print("8 — все проверки сразу")
            print("0 — выход")
            choice = ask("Пункт [0-8]: ", "0")
            if choice == "0":
                return 0
            elif choice == "1":
                check_steam(session)
            elif choice == "2":
                check_discord_text(session)
            elif choice == "3":
                check_discord_embed(session)
            elif choice == "4":
                check_telegram_text(session)
            elif choice == "5":
                check_telegram_document(session)
            elif choice == "6":
                check_telegram_chat_id(session)
            elif choice == "7":
                check_git()
            elif choice == "8":
                return 0 if run_all(session) else 1
            else:
                print("Неизвестный пункт.")


if __name__ == "__main__":
    raise SystemExit(main())