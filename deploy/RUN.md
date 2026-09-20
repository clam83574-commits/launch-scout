# Расписание: как это крутится само

## Где что живёт (после переезда 2026-09-20)

| Часть | Где | Нужен ли ноутбук |
|---|---|---|
| Сбор источников | GitHub Actions, cron `*/10` | нет |
| База сбора | кэш Actions, переносится по `restore-keys` | нет |
| Уведомления | шлёт сам прогон в Actions | нет |
| Кнопки в Telegram | Cloudflare Worker `launch-scout-bot` + D1 | нет |
| Срез для кнопок | `export_d1.py` → `wrangler d1 execute` в конце прогона | нет |

Репозиторий: github.com/clam83574-commits/launch-scout (ПУБЛИЧНЫЙ — на
публичных Actions не лимитированы, это и позволяет шаг в 10 минут).
Worker: https://launch-scout-bot.clam83574.workers.dev

Локальные `bot.py` и задача Планировщика больше не нужны и отключены.
`bot.py` остаётся в репозитории как запасной вариант: он работает на
long polling, а это НЕСОВМЕСТИМО с вебхуком — Telegram отдаёт апдейты
только одному каналу. Прежде чем запускать его снова, снять вебхук:
`curl "https://api.telegram.org/bot<токен>/deleteWebhook"`.

### Что нужно, чтобы кнопки обновлялись сами

В секреты репозитория GitHub добавить `CLOUDFLARE_API_TOKEN` и
`CLOUDFLARE_ACCOUNT_ID`. Без них сбор идёт, уведомления приходят, а бот
показывает находки, застывшие на момент последней ручной заливки — в логе
прогона на этот случай стоит явное предупреждение.

Токен делается на dash.cloudflare.com → My Profile → API Tokens → Create
Token, права: `D1 → Edit` и `Workers Scripts → Edit` для своего аккаунта.

```bash
gh secret set CLOUDFLARE_API_TOKEN --repo clam83574-commits/launch-scout
gh secret set CLOUDFLARE_ACCOUNT_ID --repo clam83574-commits/launch-scout
```

### Кто может пользоваться ботом

Владелец — по id в `LS_BOT_ALLOW` (`wrangler.toml`). Остальные — по КОДУ
ДОСТУПА: человек пишет боту код одним сообщением, бот запоминает его id в
таблице `access` и больше кода не спрашивает.

Код, а не список id: чтобы добавить человека, не надо заранее выяснять его
Telegram id — достаточно переслать ему ссылку на бота и код.

Открытым «для всех» бот не делается: найти его поиском в Telegram может кто
угодно, а внутри — находки, ради которых всё считается. В tm-scout это место
осталось нараспашку (`TM_BOT_ALLOW` пуст) и числится долгом.

Перебор кода заперт: пять промахов закрывают чат на час (таблица
`access_tries`). Проверено 2026-09-20 — шестая попытка получает отказ.

```bash
# сменить код
cd worker && npx wrangler@4 secret put LS_ACCESS_CODE

# посмотреть, кому выдан доступ
npx wrangler@4 d1 execute launch-scout --remote -y   --command "SELECT chat_id, who, granted_at FROM access"

# отобрать доступ
npx wrangler@4 d1 execute launch-scout --remote -y   --command "DELETE FROM access WHERE chat_id = '<id>'"
```

Смена кода НЕ отбирает доступ у тех, кто уже вошёл: их id лежат в `access`.
Это и нужно — код меняют, когда он утёк, а не чтобы выгнать своих.

`/whoami` отвечает кому угодно: человеку без доступа надо чем-то
представиться владельцу.

### Деплой Worker вручную

```bash
cd worker
npx wrangler@4 deploy
```

Аккаунт прописан в `wrangler.toml` ЯВНО. Без этого wrangler на этой машине
уходит в чужой account id и отвечает «Authentication error [code: 10000]»
при полностью рабочих правах — замерено 2026-09-20.

## Запасной вариант: запуск на ноутбуке

Изначально всё это жило на ноутбуке из опасения, что X режет адреса
дата-центров. ЗАМЕР ЭТО НЕ ПОДТВЕРДИЛ: с раннера GitHub `guest/activate`
и `syndication` отдают 200 (шаг «Проверить, пускает ли X с раннера» в
workflow гоняет эту проверку каждый прогон и пишет вердикт в лог).

Что проверка НЕ доказывает: она ходит неавторизованными запросами. С
куками аккаунта риск выше — за автоматический сбор с адресов дата-центров
аккаунт ограничивают охотнее, чем с домашнего. Если твиттер-слой в облаке
начнёт получать 401/403 при живых куках, вот тогда сбор по X имеет смысл
вернуть на ноутбук — остальные три источника оставив в Actions.

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
