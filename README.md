# Утро

Личный новостной дайджест: GitHub Actions собирает новости и вызывает выбранного провайдера, Git хранит историю, GitHub Pages показывает архив. Своего сервера, платной базы и сторонних Python-зависимостей нет.

## Что уже есть

- Пять групп: AI-агенты (подробно), глобальные технологии, иммиграция в UK, главное о России, переговоры об урегулировании войны.
- 17 источников: официальные релизы Codex, Claude Code, Gemini CLI, Agents SDK и LangGraph; OpenAI, Anthropic, Cursor, Google AI, Simon Willison, HN, The Verge, Ars Technica, GOV.UK, BBC Russian, Meduza и Guardian Ukraine.
- История выпусков, поиск, фильтры, отдельные страницы, Markdown и JSON Feed. Закладки и прочитанное — только в текущем браузере.
- Отправка короткого сообщения в Telegram со ссылкой на полный выпуск.
- Один JSON-контракт для всех моделей. Ссылки подставляет программа из источников; неизвестные или повторные ссылки блокируют публикацию.

**Архив и репозиторий публичны.** Не добавляйте приватные источники, личные документы или секреты в конфигурацию. Ключи находятся только в Actions Secrets. На сайте нет входа и редактирования серверных настроек.

## Подключить модель

В `config/settings.json` измените `provider` и при необходимости `model`. Альтернатива — Repository Variables `DIGEST_PROVIDER` и `DIGEST_MODEL`: они переопределяют файл без изменения кода. Названия моделей выбираются независимо от адаптера.

| provider | Доступ | GitHub Secret | model |
|---|---|---|---|
| `links` | Без модели: выдержки и ссылки | Не нужен | Пусто |
| `gemini` | Google Gemini API | `GEMINI_API_KEY` | По умолчанию `gemini-3.8-flash` |
| `claude-cli` | Claude Code, включая подписку | `CLAUDE_CODE_OAUTH_TOKEN` **или** `ANTHROPIC_API_KEY` | По умолчанию `haiku` |
| `codex` | Официальный Codex Action в GitHub | `OPENAI_API_KEY` | Пусто = выбор Codex; можно указать явно |
| `anthropic` | Claude Messages API | `ANTHROPIC_API_KEY` | Укажите доступную API-модель |
| `openai` | OpenAI Responses API | `OPENAI_API_KEY` | Укажите доступную API-модель |

Для Claude с подпиской создайте токен командой `claude setup-token` и сохраните результат в GitHub Secret `CLAUDE_CODE_OAUTH_TOKEN`. Не отправляйте токен в чат и не коммитьте его. Для `claude-cli` с подпиской не задавайте одновременно API-ключ, чтобы случайно не выбрать API-биллинг.

Codex в GitHub подключён через `openai/codex-action`, с API-прокси, read-only sandbox и отключёнными инструментами выполнения команд. Перенос `auth.json` личной подписки в публичный репозиторий не поддерживается этим проектом. Локальный адаптер `codex exec` может использовать уже существующую авторизацию CLI.

`links` — явный режим проверки сбора, без перевода и тематического AI-отбора. Перехода на другую или платную модель при ошибке **нет**. Бесплатность Gemini зависит от доступности выбранной модели и лимитов вашего аккаунта; этот код не включает платный биллинг.

После настройки запустите **Actions → Digest and Pages → Run workflow**. Чтобы получить новый выпуск в тот же день после смены провайдера, отметьте **Create an additional edition today**.

## Подключить Telegram

1. Создайте бота у [@BotFather](https://t.me/BotFather) и отправьте ему `/start` из своего Telegram.
2. Добавьте Secrets `TELEGRAM_BOT_TOKEN` и `TELEGRAM_CHAT_ID` в [настройках репозитория](https://github.com/mike0sv/news-digest/settings/secrets/actions). Chat ID можно получить из `getUpdates` вашего бота; не публикуйте URL с токеном.
3. `telegram.enabled` уже включён. При отсутствии секретов workflow оставляет notice и ничего не отправляет. Выпуски в режиме `links` по умолчанию в Telegram не уходят.
4. Запустите workflow. Последний настоящий AI-выпуск за текущий день будет доставлен после публикации сайта.

История сохраняется **до** отправки. После подтверждения Telegram записывается receipt, поэтому обычный повтор workflow не дублирует сообщение. При аварии между доставкой и записью receipt повтор всё же возможен; Telegram Bot API не предоставляет ключ идемпотентности. Не включайте параллельную ручную отправку.

## Источники, отбор и расписание

Источники, группы, приоритеты и ограничения — в одном `config/settings.json`. Сменить модель можно, не трогая сбор, хранение или интерфейс. Добавить адаптер — реализовать `Result(data, usage)` в `digest/providers.py` и зарегистрировать имя.

Основной вход — RSS/Atom. Anthropic News и Cursor читаются с их публичных страниц; их разметка может измениться. Для UK дополнительно читается GOV.UK Content API с недавними изменениями документа. Общие страницы разделов исключены. Недоступный источник отмечается; если недоступны все, выпуск не создаётся.

По умолчанию рассматриваются последние 48 часов. Тексты из RSS могут быть неполными. В зависимости от объёма источников новые публикации могут не попасть в ограниченный набор кандидатов. Удаляются повторы URL и точных заголовков; смысловые повторы объединяет модель. Предыдущие сюжеты за 14 дней передаются ей для сравнения. Это мониторинг выбранных источников, а не гарантия охвата всего интернета.

Сбор запускается после 08:17 `Europe/London`. Два cron-запуска учитывают летнее время; лишний и повторный запуск ничего не генерирует. При смене часа/таймзоны исправьте и cron в workflow. GitHub может задерживать или пропускать cron — точное время не гарантировано.

В Git коммитятся только метаданные статей, готовые выпуски и квитанции доставки. Исходный текст и prompt хранятся в игнорируемом `.runtime/` только во время выполнения. Pages публикует только собранную папку `site/`.

## Локальная проверка

Нужен Python 3.11+; на GitHub используется 3.12. Для проверки JavaScript — Node; для CLI-провайдеров — соответствующий CLI.

```sh
python -m unittest discover -s tests -v
python -m digest check
python -m digest run
python -m digest build
python -m http.server --directory site 8000
```

Команды также доступны отдельно: `prepare`, `generate`, `build`, `deliver`. Обычный `run` повторно не создаёт выпуск того же дня; `run --force` создаёт дополнительный. Telegram отправляется только отдельной командой `deliver`.

## Официальные контракты

- [Gemini structured output](https://ai.google.dev/gemini-api/docs/generate-content/structured-output)
- [Claude CLI structured output](https://code.claude.com/docs/en/headless), [авторизация подпиской](https://code.claude.com/docs/en/authentication#generate-a-long-lived-token)
- [Codex GitHub Action](https://learn.chatgpt.com/docs/github-action), [ограничения авторизации в CI](https://learn.chatgpt.com/docs/non-interactive-mode)
- [OpenAI structured output](https://developers.openai.com/api/docs/guides/structured-outputs)
- [GitHub Pages](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages)
