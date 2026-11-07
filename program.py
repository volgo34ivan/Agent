import json
import os
from pathlib import Path

from flask import Flask, request, jsonify
from openai import OpenAI

# ==========================================
# 1. КОНСТАНТЫ
# ==========================================
API_BASE = "http://127.0.0.1:1234/v1"  # Адрес вашего LM Studio
API_KEY = "lm-studio"                 # LM Studio не проверяет ключ, но библиотека требует его наличия
MODEL_NAME = "qwen3.5-9b"    # Укажите точное имя вашей модели из LM Studio
MODEL_TYPE = "chat"                   # Тип модели (в данном случае мы используем чат-эндпоинт)

app = Flask(__name__)
client = OpenAI(base_url=API_BASE, api_key=API_KEY)

# Папка проекта — база для относительных путей в инструментах работы с файлами.
BASE_DIR = Path(__file__).resolve().parent


def _resolve_path(path: str) -> Path:
    """Разворачивает ~ и переменные окружения (например %USERPROFILE%, %TEMP%),
    а относительные пути считает от папки проекта."""
    expanded = os.path.expandvars(os.path.expanduser(path))
    p = Path(expanded)
    if not p.is_absolute():
        p = BASE_DIR / p
    return p

# ==========================================
# 2. СИСТЕМА ИНСТРУМЕНТОВ (Декоратор @tool)
# ==========================================
TOOLS_SCHEMA = []
REGISTERED_TOOLS = {}

def tool(description: str, params: dict):
    """Декоратор для регистрации инструментов и автоматической генерации JSON-схемы"""
    def decorator(func):
        REGISTERED_TOOLS[func.__name__] = func

        # Слой 1: для инструментов без параметров не добавляем "required".
        # Пустой required=[] сбивает часть моделей и снижает вероятность вызова.
        parameters = {"type": "object", "properties": params}
        if params:
            parameters["required"] = list(params.keys())

        TOOLS_SCHEMA.append({
            "type": "function",
            "function": {
                "name": func.__name__,
                "description": description,
                "parameters": parameters,
            }
        })
        return func
    return decorator

# Регистрируем инструменты
@tool(
    description="Получает текущую погоду в указанном городе",
    params={
        "location": {"type": "string", "description": "Название города, например, Москва"}
    }
)
def get_weather(location: str):
    # В реальности здесь был бы запрос к API погоды. Для примера возвращаем заглушку.
    return f"В городе {location} сейчас +22°C, солнечно."

@tool(
    description=(
        "ОБЯЗАТЕЛЬНО вызывай этот инструмент, когда пользователь здоровается "
        "(привет, здравствуй, здорово, хай, приветствую, добрый день). "
        "Не отвечай на приветствие текстом — сначала вызови этот инструмент."
    ),
    params={}
)
def hello_user():
    return f"Привет хозяин. Чем могу служить?"


@tool(
    description=(
        "Создаёт или перезаписывает текстовый файл по указанному пути и "
        "записывает в него содержимое. Используй при просьбах создать файл, "
        "сохранить текст в файл, записать что-то на диск."
    ),
    params={
        "path": {
            "type": "string",
            "description": (
                "Путь к файлу: абсолютный, относительный (от папки проекта) "
                "или с переменными окружения, например %USERPROFILE%/text.txt"
            ),
        },
        "content": {
            "type": "string",
            "description": "Текст, который нужно записать в файл",
        },
    },
)
def write_text_file(path: str, content: str):
    p = _resolve_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"Файл сохранён: {p} ({len(content)} символов)"


@tool(
    description=(
        "Читает текстовый файл по указанному пути и возвращает его содержимое. "
        "Используй при просьбах прочитать или показать содержимое файла."
    ),
    params={
        "path": {
            "type": "string",
            "description": (
                "Путь к файлу: абсолютный, относительный (от папки проекта) "
                "или с переменными окружения, например %USERPROFILE%/text.txt"
            ),
        },
    },
)
def read_text_file(path: str):
    p = _resolve_path(path)
    return p.read_text(encoding="utf-8")


# ==========================================
# 3. FLASK СЕРВЕР И ЦИКЛ АГЕНТА
# ==========================================
@app.route('/chat', methods=['POST'])
def chat():
    data = request.json
    user_message = data.get("message", "")
    
    if not user_message:
        return jsonify({"error": "Поле 'message' обязательно"}), 400

    # Журнал вызовов инструментов — отдаём его GUI для консоли-лога.
    tools_used = []

    messages = [
        {
            "role": "system",
            "content": (
                "Ты агент с инструментами. Строго соблюдай правила маршрутизации:\n"
                "- Если пользователь здоровается (привет, здравствуй, здорово, хай, "
                "приветствую, добрый день) — ОБЯЗАТЕЛЬНО вызови инструмент hello_user. "
                "Никогда не отвечай на приветствие собственным текстом.\n"
                "- Если спрашивают о погоде — вызови инструмент get_weather.\n"
                "- Если просят создать/записать файл — вызови write_text_file(path, content).\n"
                "- Если просят прочитать файл — вызови read_text_file(path).\n"
                "- В остальных случаях отвечай текстом.\n"
                "Пример: 'Привет' -> вызвать hello_user.\n"
                "Пример: 'погода в Москве' -> вызвать get_weather.\n"
                "Пример: 'Создай файл text.txt и напиши в нём Привет' "
                "-> вызвать write_text_file(path='text.txt', content='Привет')."
            ),
        },
        {"role": "user", "content": user_message}
    ]

    try:
        # Шаг 1: Отправляем запрос модели с описанием инструментов
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
            tools=TOOLS_SCHEMA,
            tool_choice="auto",  # Модель сама решит, вызывать ли инструмент
            temperature=0,       # Слой 2: детерминированнее поведение
        )
        
        response_message = response.choices[0].message
        # Слой 4: кладём в историю обычный dict без лишних None-полей,
        # чтобы совместимые серверы не отвергали сообщение.
        messages.append(response_message.model_dump(exclude_none=True))

        # Шаг 2: Проверяем, хочет ли модель вызвать инструмент
        if response_message.tool_calls:
            tool_outputs = []
            for tool_call in response_message.tool_calls:
                function_name = tool_call.function.name
                # Слой 4: у функций без параметров модель может вернуть "" или null.
                raw_args = tool_call.function.arguments
                try:
                    function_args = json.loads(raw_args) if raw_args else {}
                except (json.JSONDecodeError, TypeError):
                    function_args = {}
                if not isinstance(function_args, dict):
                    function_args = {}
                
                print(f"[Агент] Вызывает инструмент: {function_name} с аргументами {function_args}")

                # Находим функцию в нашем реестре и выполняем её
                if function_name in REGISTERED_TOOLS:
                    function_to_call = REGISTERED_TOOLS[function_name]
                    function_response = function_to_call(**function_args)
                    tools_used.append({
                        "name": function_name,
                        "arguments": function_args,
                        "result": str(function_response),
                    })
                    tool_outputs.append(str(function_response))

                    # Добавляем результат работы инструмента в историю
                    messages.append({
                        "tool_call_id": tool_call.id,
                        "role": "tool",
                        "name": function_name,
                        "content": str(function_response),
                    })

            # Шаг 3: Ответом считаем результат инструмента как есть.
            # Повторный вызов модели не делаем, чтобы она не перефразировала
            # результат — тогда текст в чате точно совпадёт с логом.
            if tool_outputs:
                final_answer = "\n".join(tool_outputs)
            else:
                final_answer = response_message.content
        else:
            # Модель не стала использовать инструменты, а ответила сразу
            final_answer = response_message.content

        return jsonify({"response": final_answer, "tools": tools_used})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    # Запуск сервера на порту 5000
    app.run(port=5000, debug=True)