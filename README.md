# Agent Chat — локальный AI-агент с инструментами

Локальный чат-агент на базе LLM из LM Studio с вызовом инструментов (tool-calling)
и настольным интерфейсом на PyQt6.

## Возможности
- Вызов инструментов моделью через OpenAI-совместимый API LM Studio (`tool_calls`).
- Готовые инструменты: `hello_user`, `get_weather`, `write_text_file`, `read_text_file`.
- Настольный GUI: лента чата и панель «Лог инструментов» (время, аргументы, результат, длительность ответа).
- Автозапуск стека: LM Studio → Flask-сервер → GUI.

## Требования
- Python 3.10+
- [LM Studio](https://lmstudio.ai/) с загруженной моделью, поддерживающей tool-calling
- (опционально) `lms` CLI в PATH — для автозапуска LM Studio

## Установка
```bat
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

## Запуск
```bat
start.bat            :: сервер + GUI (двойной клик тоже работает)
agent.vbs            :: тихий запуск без окна консоли
start.bat check      :: проверка зависимостей и LM Studio
start.bat server     :: только Flask-сервер
```

Либо напрямую:
```bat
.venv\Scripts\python.exe start.py
```

## Конфигурация
В [`program.py`](program.py):
- `API_BASE` — адрес LM Studio (по умолчанию `http://127.0.0.1:1234/v1`);
- `MODEL_NAME` — имя загруженной модели (по умолчанию `qwen3.5-9b`);
- `API_KEY` — заглушка для локального сервера (`lm-studio`).

## Инструменты
Инструменты регистрируются декоратором `@tool` в [`program.py`](program.py).
Схема для модели и реестр вызовов собираются автоматически.

## Структура
- [`program.py`](program.py) — Flask-сервер, цикл агента и инструменты;
- [`gui.py`](gui.py) — настольный клиент;
- [`start.py`](start.py) — оркестратор запуска;
- [`start.bat`](start.bat), [`agent.vbs`](agent.vbs) — лаунчеры для Windows.

## Безопасность
- Модель и сервер работают локально, данные наружу не отправляются.
- Инструменты `write_text_file` / `read_text_file` дают доступ к файловой системе
  в пределах прав пользователя — используйте с осторожностью.
- Секретов в репозитории нет; `API_KEY` — фиктивная заглушка для LM Studio.

## Лицензия
MIT — см. [`LICENSE`](LICENSE).
