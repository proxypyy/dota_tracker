"""Цели мониторинга и настройки трекера обновлений сборок Dota 2."""

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
RETRY_BACKOFF_SECONDS = 2.0            # backoff: 2 с, 4 с (общий)
STEAM_RETRY_SLEEP_CAP_SECONDS = 30.0
STEAM_POLITE_DELAY_SECONDS = 0.5

# Сеть: Discord webhook.
DISCORD_TIMEOUT_SECONDS = 30
DISCORD_MAX_ATTEMPTS = 3
DISCORD_SEND_DELAY_SECONDS = 1.5
DISCORD_RETRY_SLEEP_CAP_SECONDS = 60.0

# Сеть: Telegram Bot API.
TELEGRAM_API_URL_TEMPLATE = "https://api.telegram.org/bot{token}/{method}"
TELEGRAM_TIMEOUT_SECONDS = 30
TELEGRAM_MAX_ATTEMPTS = 3
TELEGRAM_SEND_DELAY_SECONDS = 1.5      # пауза между постами в канал
TELEGRAM_RETRY_SLEEP_CAP_SECONDS = 60.0

# Прочее.
MSK_UTC_OFFSET_HOURS = 3
USER_AGENT = "Dota2-Build-Tracker/1.0 (Python requests; +https://github.com)"