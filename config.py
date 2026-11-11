"""Цели мониторинга и настройки трекера обновлений сборок Dota 2.

Источник версий — публичный интерфейс Steam Web API IGCVersion_* (его же
использует клиент Steam). Ответ имеет вид:
    {"result": {"success": true, ..., "active_version": 6973, ...}}

Нюанс: у серверных appid (373310, 2305290, 247060) нет собственного интерфейса
IGCVersion_<appid> — Steam отвечает "Interface not found". Их версии отдаёт
метод GetServerVersion родительского приложения, поэтому в URL два
идентификатора: gc_appid (владелец интерфейса) и appid (отслеживаемая цель).
"""

# {gc_appid} — приложение-владелец Game Coordinator,
# {method}   — GetClientVersion (клиент) или GetServerVersion (dedicated server),
# {appid}    — отслеживаемое приложение.
STEAM_API_URL_TEMPLATE = (
    "https://api.steampowered.com/IGCVersion_{gc_appid}/{method}/v1/"
    "?appid={appid}&version=0&format=json"
)

# appid -> параметры цели мониторинга.
TRACKED_APPS: dict[int, dict[str, str | int]] = {
    570: {"name": "Dota 2", "gc_appid": 570, "method": "GetClientVersion"},
    373310: {"name": "Dota 2 Dedicated Server", "gc_appid": 570, "method": "GetServerVersion"},
    2305270: {"name": "Dota 2 Staging", "gc_appid": 2305270, "method": "GetClientVersion"},
    2305290: {"name": "Dota 2 Staging Server", "gc_appid": 2305270, "method": "GetServerVersion"},
    247040: {"name": "Dota 2 Experimental", "gc_appid": 247040, "method": "GetClientVersion"},
    247060: {"name": "Dota 2 Experimental Server", "gc_appid": 247040, "method": "GetServerVersion"},
}

# Цвета embed'ов Discord.
EMBED_COLOR_MAIN = 3066993    # зелёный: Dota 2 и Dedicated Server
EMBED_COLOR_TEST = 15105570   # оранжевый: Staging и Experimental
MAIN_APP_IDS = frozenset({570, 373310})

# Сеть: Steam API.
STEAM_TIMEOUT_SECONDS = 10
STEAM_MAX_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 2.0            # backoff: 2 с, 4 с (и для Discord)
STEAM_RETRY_SLEEP_CAP_SECONDS = 30.0   # потолок ожидания по Retry-After
STEAM_POLITE_DELAY_SECONDS = 0.5       # вежливая пауза между опросами целей

# Сеть: Discord webhook.
DISCORD_TIMEOUT_SECONDS = 30
DISCORD_MAX_ATTEMPTS = 3
DISCORD_SEND_DELAY_SECONDS = 1.5       # пауза между отправками (rate-limit)
DISCORD_RETRY_SLEEP_CAP_SECONDS = 60.0

# Прочее.
MSK_UTC_OFFSET_HOURS = 3
USER_AGENT = "Dota2-Build-Tracker/1.0 (Python requests; +https://github.com)"