# Dota 2 Build Tracker

Трекер обновлений сборок Dota 2 (аналог DOTA_DM_SPAM / DOTA2_WEB): каждые 5 минут
опрашивает публичный Steam Web API, фиксирует смену `active_version` в git-репозитории
и отправляет уведомление в Discord с приложением JSON-файла версии и ссылкой на diff.

## Цели мониторинга

| appid   | Название                    | Источник (Steam Web API)              |
|---------|-----------------------------|---------------------------------------|
| 570     | Dota 2                      | `IGCVersion_570/GetClientVersion/v1`  |
| 373310  | Dota 2 Dedicated Server     | `IGCVersion_570/GetServerVersion/v1`  |
| 2305270 | Dota 2 Staging              | `IGCVersion_2305270/GetClientVersion/v1` |
| 2305290 | Dota 2 Staging Server       | `IGCVersion_2305270/GetServerVersion/v1` |
| 247040  | Dota 2 Experimental         | `IGCVersion_247040/GetClientVersion/v1` |
| 247060  | Dota 2 Experimental Server  | `IGCVersion_247040/GetServerVersion/v1` |

> Почему так: у серверных appid нет собственного интерфейса `IGCVersion_<appid>`
> (Steam отвечает `Interface not found`), их версии отдаёт метод `GetServerVersion`
> родительского приложения. Ответ API: `{"result": {"success": true, ..., "active_version": N}}`.
> Ключевое поле сравнения — `result.active_version`.

## Структура
