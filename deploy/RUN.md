# Расписание: как это крутится само

## Почему на ноутбуке, а не в облаке

Твиттер-слой держится на куках обычного аккаунта, а X режет запросы с
диапазонов дата-центров жёстче всего — это относится и к Cloudflare
Workers, и к GitHub Actions (их раннеры живут на адресах Azure).
Домашний адрес ноутбука для X выглядит обычным пользователем, адрес
облака — нет.

Три остальных источника (HN, YC, GitHub) из облака работают прекрасно.
Если твиттер-слой когда-нибудь окончательно закроется, весь скаут
переносится в Cloudflare Worker без переписывания: источники изолированы
друг от друга специально.

Замечание про сеть: на этой машине исходящий адрес казахстанский
(проверено 2026-09-20 — `85.117.111.175`, Astana), и x.com открывается
напрямую. Если сбор вдруг встанет на всех запросах разом, первое, что
надо проверить, — не сменился ли выход в сеть.

## Постановка на расписание (Windows)

Прогон раз в 25 минут. Чаще не нужно: замеры всё равно делаются с
интервалом, а лишние обращения к X только приближают аккаунт к блокировке.

**Путь к python брать из `sys.executable`, а не из `Get-Command`.** В PATH
лежит `WindowsApps\python.exe` — это не интерпретатор, а execution alias
Microsoft Store: в консоли он работает, а из Планировщика процесс убивается
сразу, с кодом `0xC000013A` и без единой строки в логах. Замерено
2026-09-20: задача числилась выполненной, а прогон не доходил до базы.

```powershell
$py  = & python -c "import sys; print(sys.executable)"
$dir = "C:\Users\LENOVO\Desktop\IBIX Studio\products\launch-scout"
$act = New-ScheduledTaskAction -Execute $py -Argument "scout.py" -WorkingDirectory $dir
$trg = New-ScheduledTaskTrigger -Once -At (Get-Date) `
        -RepetitionInterval (New-TimeSpan -Minutes 25)
$set = New-ScheduledTaskSettingsSet -StartWhenAvailable `
        -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 20)
Register-ScheduledTask -TaskName "launch-scout" -Action $act -Trigger $trg `
        -Settings $set -Description "Поиск свежих запусков -> Telegram"
```

`-StartWhenAvailable` обязателен: без него пропущенные из-за выключенного
ноутбука прогоны просто теряются, а с ним задача догоняет при включении.

Сводка дважды в день — отдельной задачей:

```powershell
$act2 = New-ScheduledTaskAction -Execute $py -Argument "scout.py --digest" -WorkingDirectory $dir
$trg2 = New-ScheduledTaskTrigger -Daily -At 10:00
Register-ScheduledTask -TaskName "launch-scout-digest" -Action $act2 -Trigger $trg2 -Settings $set
```

## Бот с кнопками

`bot.py` — отдельный постоянно висящий процесс: он ЖДЁТ нажатия, а задача
планировщика по определению отрабатывает и умирает. Оба пишут в одну базу,
это безопасно — в `db.py` включён WAL.

Автозапуск при входе в систему сделан файлом
`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\launch-scout-bot.bat`
(триггер `-AtLogOn` в Планировщике требует прав администратора, которых на
этой машине нет). Убрать автозапуск = удалить этот файл.

Запустить вручную: `python bot.py`. Кнопки: топ-10, обновить сейчас,
за сутки, статус. Отвечает ТОЛЬКО владельцу — `TG_CHAT_ID` из `.env`.

## Приёмка

Проверить, что задача жива:

```powershell
Get-ScheduledTaskInfo -TaskName "launch-scout" |
    Select-Object LastRunTime, LastTaskResult, NextRunTime
```

`LastTaskResult` = 0 — прогон отработал. Любое другое число — смотреть
`python scout.py --status`: там видно, какой источник молчит и почему.

**Одного нуля мало.** «Задача зарегистрирована» и «задача работает» на
Windows — разные события, и между ними помещается целый класс ошибок.
Приёмка — прогнать `Start-ScheduledTask` и убедиться, что в данных
появился след:

```powershell
Start-ScheduledTask -TaskName "launch-scout"; Start-Sleep 60
Get-ScheduledTaskInfo -TaskName "launch-scout" | Select-Object LastTaskResult
```

```bash
python scout.py --status   # у источников должно стоять «0 мин назад»
```

## Остановить

```powershell
Disable-ScheduledTask -TaskName "launch-scout"        # пауза
Unregister-ScheduledTask -TaskName "launch-scout"     # снять совсем
```

## Что ломается и как чинить

| Признак | Причина | Что делать |
|---|---|---|
| в `--status` у `x` стоит `401/403 — куки не приняты` | протухли куки: сменили пароль, вышли из аккаунта в браузере, или аккаунт ограничен | заново скопировать `auth_token` и `ct0` в `.env` |
| у `x` — `нет queryId` или `404 на SearchTimeline` | X обновил фронтенд | парсер обновляет `queryId` сам при первой же ошибке; если не помогло — удалить `data/x_queries.json` и прогнать снова |
| у `x` — `429` | упёрлись в лимит | увеличить `X_DELAY` в `.env` до 2.5–3 с |
| у `gh` — `rate limit exceeded` | нет токена, 60 запросов в час на адрес | завести `GITHUB_TOKEN` (нужны только права на чтение публичного) |
| уведомлений нет вообще сутки | либо всё ниже порога, либо источники молчат | `python scout.py --status`, затем `python scout.py --dry` |
| уведомлений слишком много | пороги низковаты для вашего потока | поднять `HOT_MIN` в `score.py`, затем `python test_score.py` |

Аккаунт X при блокировке меняется на другой без потери данных: база,
история замеров и нормы авторов привязаны к постам, а не к аккаунту сбора.
