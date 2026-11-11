"""test_send.py — интерактивная проверка трекера сборок Dota 2.

Запуск:
    python test_send.py            # интерактивное меню
    echo 5 | python test_send.py   # все проверки без вопросов (код выхода 0/1)

Проверки:
    1 — Steam API: текущие версии всех целей (боевой код из tracker.py).
    2 — Discord: текстовое сообщение в webhook (валидность вебхука).
    3 — Discord: тестовый embed + JSON-файл — формат боевого уведомления,
        помеченный [ТЕСТ]. Историю не трогает, git-операций не выполняет.
    4 — Git: репозиторий, user.name/email, remote, право на push
        (git push --dry-run — реально ничего не отправляется).
    5 — все проверки подряд + сводка.

Побочные эффекты: отсутствуют, кроме того, что проверка 3 может создать
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
    """input() с обработкой EOF: позволяет запускать `echo 5 | python test_send.py`."""
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


def require_webhook() -> str | None:
    url = webhook_url()
    if not url:
        print("FAIL: DISCORD_WEBHOOK_URL не задан (.env или переменная окружения).")
        print("      Discord: Server Settings -> Integrations -> Webhooks -> Copy URL.")
        return None
    if "/api/webhooks/" not in url:
        print("WARNING: URL не похож на Discord webhook, пробую всё равно...")
    return url


def explain_discord_error(response: requests.Response) -> None:
    code = response.status_code
    if code in (401, 404):
        print(f"FAIL: HTTP {code} — вебхук удалён или URL неверный. Создайте новый и обновите .env/secret.")
    elif code == 400:
        print(f"FAIL: HTTP 400 — Discord отклонил сообщение: {response.text[:300]!r}")
    else:
        print(f"FAIL: HTTP {code}: {response.text[:300]!r}")


def post_with_retry(send) -> bool:
    """Вызывает send() -> Response; 3 попытки, обработка 429/5xx/таймаутов."""
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

    if appid is None:
        raw = ask("appid (Enter = 570): ", "570")
        if not raw.isdigit():
            print("FAIL: appid должен быть числом.")
            return False
        appid = int(raw)
    if appid not in config.TRACKED_APPS:
        print(f"FAIL: appid {appid} нет в config.TRACKED_APPS.")
        print("Доступные: " + ", ".join(str(a) for a in config.TRACKED_APPS))
        return False

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
            return False
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


def check_git() -> bool:
    print(SEP)
    print("ПРОВЕРКА 4: Git (ничего не отправляется: push --dry-run)")
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
    results = {
        "Steam API": check_steam(session),
        "Discord текст": check_discord_text(session),
        "Discord embed + файл": check_discord_embed(session, appid=570),
        "Git": check_git(),
    }
    print("\n" + SEP)
    print("СВОДКА")
    print(SEP)
    for label, ok in results.items():
        print(f"  {'OK  ' if ok else 'FAIL'} — {label}")
    return all(results.values())


def main() -> int:
    load_dotenv(BASE_DIR / ".env")
    print(SEP)
    print("Dota 2 Build Tracker — проверка настроек и тестовые запросы")
    print(SEP)
    print(f"DISCORD_WEBHOOK_URL: {'задан' if webhook_url() else 'НЕ задан'}")
    print(f"GITHUB_REPO_URL:     {repo_url() or 'НЕ задан'}")
    print("DRY_RUN на этот скрипт не влияет: тесты отправляются реально.")
    with requests.Session() as session:
        session.headers.update({"User-Agent": config.USER_AGENT})
        while True:
            print("\n" + SEP)
            print("1 — Steam API: текущие версии всех целей")
            print("2 — Discord: тестовое текстовое сообщение")
            print("3 — Discord: тестовый embed + JSON-файл (формат уведомления)")
            print("4 — Git: репозиторий, identity, remote, право на push")
            print("5 — все проверки сразу")
            print("0 — выход")
            choice = ask("Пункт [0-5]: ", "0")
            if choice == "0":
                return 0
            elif choice == "1":
                check_steam(session)
            elif choice == "2":
                check_discord_text(session)
            elif choice == "3":
                check_discord_embed(session)
            elif choice == "4":
                check_git()
            elif choice == "5":
                return 0 if run_all(session) else 1
            else:
                print("Неизвестный пункт.")


if __name__ == "__main__":
    raise SystemExit(main())