import ctypes
import inspect
import json
import math
import os
import subprocess
from pathlib import Path

import pyautogui
import time

import requests
from flask import Flask, request, jsonify
from openai import OpenAI

# ==========================================
# 1. КОНСТАНТЫ
# ==========================================
API_BASE = "http://127.0.0.1:1234/v1"  # Адрес вашего LM Studio
API_KEY = "lm-studio"                 # LM Studio не проверяет ключ, но библиотека требует его наличия
MODEL_NAME = "qwen3.5-9b"    # Укажите точное имя вашей модели из LM Studio
MODEL_TYPE = "chat"                   # Тип модели (в данном случае мы используем чат-эндпоинт)

# --- Учёт контекста и защита от переполнения ---
MAX_CONTEXT_TOKENS = 8192      # фолбэк, если LM Studio не отдаёт точное значение
MAX_COMPLETION_TOKENS = 1024   # резерв под ответ модели
CONTEXT_SAFETY_MARGIN = 128    # запас на служебные токены
MAX_TOOL_CALLS = 5             # предохранитель от «лавины» инструментов в одном ответе
MAX_ITERATIONS = 4             # максимум раундов «модель → инструменты»
MAX_TOTAL_TOOL_CALLS = 12      # суммарно вызовов инструментов за запрос
MAX_TOOL_RESULT_CHARS = 4000   # обрезка результата инструмента в контексте
NATIVE_BASE = API_BASE.rsplit("/v1", 1)[0]

pyautogui.PAUSE = 0.1 # Небольшая пауза после каждого действия, чтобы система успевала реагировать

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


_CONTEXT_LIMIT_CACHE = None


def get_context_limit() -> int:
    """Точный размер контекста загруженной модели из LM Studio.

    Пробует нативное API /api/v0/models (loaded_context_length /
    max_context_length). Если недоступно — возвращает MAX_CONTEXT_TOKENS.
    """
    global _CONTEXT_LIMIT_CACHE
    if _CONTEXT_LIMIT_CACHE is not None:
        return _CONTEXT_LIMIT_CACHE
    try:
        r = requests.get(f"{NATIVE_BASE}/api/v0/models", timeout=2)
        r.raise_for_status()
        for m in r.json().get("data", []):
            if m.get("id") == MODEL_NAME:
                for key in ("loaded_context_length", "max_context_length"):
                    val = m.get(key)
                    if val:
                        _CONTEXT_LIMIT_CACHE = int(val)
                        return _CONTEXT_LIMIT_CACHE
    except Exception:
        pass
    return MAX_CONTEXT_TOKENS


def estimate_tokens(text: str) -> int:
    """Консервативная оценка токенов (≈3 символа на токен)."""
    return max(1, len(text) // 3)


# ==========================================
# 2. СИСТЕМА ИНСТРУМЕНТОВ (Декоратор @tool)
# ==========================================
TOOLS_SCHEMA = []
REGISTERED_TOOLS = {}
VERBATIM_TOOLS = set()   # инструменты, чей результат — финальный ответ как есть


def tool(description: str, params: dict, verbatim: bool = False):
    """Декоратор для регистрации инструментов и автоматической генерации JSON-схемы.

    verbatim=True — результат инструмента сразу становится финальным ответом
    (без повторного обращения к модели и перефразирования).
    """
    def decorator(func):
        REGISTERED_TOOLS[func.__name__] = func
        if verbatim:
            VERBATIM_TOOLS.add(func.__name__)

        # required = только параметры без значения по умолчанию.
        # Пустой required не добавляем — он сбивает часть моделей.
        sig = inspect.signature(func)
        required = [
            name for name, p in sig.parameters.items()
            if name in params and p.default is inspect.Parameter.empty
        ]
        parameters = {"type": "object", "properties": params}
        if required:
            parameters["required"] = required

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

# ================ Регистрируем инструменты =====================================
@tool(
    description="Получает текущую погоду в указанном городе",
    params={
        "location": {"type": "string", "description": "Название города, например, Москва"}
    },
    verbatim=True,
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
    params={},
    verbatim=True,
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
    verbatim=True,
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


@tool(
    description="Возвращает текущие координаты курсора мыши на экране",
    params={}
)
def get_mouse_position():
    """Возвращает текущие координаты курсора мыши."""
    x, y = pyautogui.position()
    return {"x": x, "y": y}


@tool(
    description="Кликает мышью. Если указаны координаты — сначала перемещает курсор туда, затем кликает. Если координаты не указаны — кликает в текущей позиции курсора.",
    params={
        "x": {
            "type": "integer",
            "description": "Координата X на экране. Необязательно."
        },
        "y": {
            "type": "integer",
            "description": "Координата Y на экране. Необязательно."
        },
        "button": {
            "type": "string",
            "enum": ["left", "right", "middle"],
            "description": "Кнопка мыши: left (левая), right (правая), middle (средняя). По умолчанию left."
        },
        "clicks": {
            "type": "integer",
            "description": "Количество кликов. По умолчанию 1."
        },
        "duration": {
            "type": "number",
            "description": "Длительность перемещения курсора в секундах (для плавности). По умолчанию 0.0 — мгновенно."
        }
    }
)
def click_mouse(
    x: int = None,
    y: int = None,
    button: str = "left",
    clicks: int = 1,
    duration: float = 0.0
):
    """Кликает мышью в указанных координатах или в текущей позиции."""
    try:
        # Если координаты переданы — сначала перемещаем курсор
        if x is not None and y is not None:
            pyautogui.moveTo(x, y, duration=duration)

        pyautogui.click(button=button, clicks=clicks)

        clicked_at = {"x": x, "y": y} if x is not None else "current_position"
        return {"status": "ok", "clicked_at": clicked_at}

    except pyautogui.FailSafeException:
        return {
            "status": "error",
            "message": "Сработала аварийная защита: курсор был перемещён в левый верхний угол, клик отменён."
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


@tool(
    description=(
        "Перемещает курсор мыши в указанные координаты экрана без клика. "
        "Используй, когда просят навести или переместить мышь в точку."
    ),
    params={
        "x": {"type": "integer", "description": "Координата X на экране"},
        "y": {"type": "integer", "description": "Координата Y на экране"},
        "duration": {
            "type": "number",
            "description": "Длительность перемещения в секундах (0 — мгновенно)",
        },
    },
)
def move_mouse(x: int, y: int, duration: float = 0.0):
    """Перемещает курсор мыши в (x, y)."""
    try:
        pyautogui.moveTo(x, y, duration=duration)
        return {"status": "ok", "moved_to": {"x": x, "y": y}}
    except pyautogui.FailSafeException:
        return {
            "status": "error",
            "message": "Сработала аварийная защита (FailSafe), курсор не перемещён.",
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


@tool(
    description=(
        "Возвращает размеры экрана в пикселях (ширину и высоту). "
        "Используй, чтобы знать границы экрана перед кликом или перемещением."
    ),
    params={}
)
def get_screen_size():
    """Возвращает ширину и высоту экрана."""
    w, h = pyautogui.size()
    return {"width": w, "height": h}


@tool(
    description=(
        "Перемещает курсор мыши в центр экрана. Используй, когда просят "
        "передвинуть или навести мышь в центр экрана — не нужно предварительно "
        "узнавать размер экрана."
    ),
    params={}
)
def move_mouse_to_center():
    """Перемещает курсор в центр экрана."""
    try:
        w, h = pyautogui.size()
        cx, cy = w // 2, h // 2
        pyautogui.moveTo(cx, cy)
        return {"status": "ok", "moved_to": {"x": cx, "y": cy}}
    except pyautogui.FailSafeException:
        return {
            "status": "error",
            "message": "Сработала аварийная защита (FailSafe), курсор не перемещён.",
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


@tool(
    description=(
        "Перемещает курсор мыши по кругу (окружности) на экране. Используй, "
        "когда просят 'провести круг мышью', 'нарисовать окружность курсором' "
        "или 'подвигать мышь по кругу'. Центр по умолчанию — центр экрана."
    ),
    params={
        "radius": {
            "type": "integer",
            "description": "Радиус круга в пикселях (по умолчанию 200)",
        },
        "duration": {
            "type": "number",
            "description": "Длительность полного круга в секундах (по умолчанию 1.5)",
        },
        "steps": {
            "type": "integer",
            "description": "Количество шагов/точек круга (по умолчанию 72)",
        },
        "center_x": {
            "type": "integer",
            "description": "Координата X центра (по умолчанию центр экрана)",
        },
        "center_y": {
            "type": "integer",
            "description": "Координата Y центра (по умолчанию центр экрана)",
        },
        "hold": {
            "type": "boolean",
            "description": (
                "Удерживать левую кнопку мыши при движении (рисование). "
                "По умолчанию false."
            ),
        },
    },
)
def move_mouse_circle(
    radius: int = 200,
    duration: float = 1.5,
    steps: int = 72,
    center_x: int = None,
    center_y: int = None,
    hold: bool = False,
):
    """Двигает курсор по окружности; hold=True — с зажатой кнопкой (рисование)."""
    try:
        sw, sh = pyautogui.size()
        cx = center_x if center_x is not None else sw // 2
        cy = center_y if center_y is not None else sh // 2
        r = max(1, int(radius))
        n = max(8, min(int(steps), 720))
        delay = (duration / n) if duration and duration > 0 else 0

        if hold:
            pyautogui.mouseDown()

        for i in range(n + 1):
            ang = 2 * math.pi * i / n
            x = int(cx + r * math.cos(ang))
            y = int(cy + r * math.sin(ang))
            pyautogui.moveTo(x, y, _pause=False)
            if delay:
                time.sleep(delay)

        if hold:
            pyautogui.mouseUp()

        return {
            "status": "ok",
            "center": {"x": cx, "y": cy},
            "radius": r,
            "steps": n,
            "hold": hold,
        }
    except pyautogui.FailSafeException:
        try:
            if hold:
                pyautogui.mouseUp()
        except Exception:
            pass
        return {
            "status": "error",
            "message": "Сработала аварийная защита (FailSafe), движение прервано.",
        }
    except Exception as e:
        try:
            if hold:
                pyautogui.mouseUp()
        except Exception:
            pass
        return {"status": "error", "message": str(e)}


@tool(
    description=(
        "Рисует/ведёт курсор мыши по произвольной траектории из точек. Это "
        "УНИВЕРСАЛЬНЫЙ инструмент для любых фигур и рисунков: круг, квадрат, "
        "треугольник, звезда, восьмёрка, линия, «каляка-маляка» и т.п. Ты сам "
        "вычисляешь точки (в пикселях) и передаёшь их по порядку. При hold=true "
        "удерживается левая кнопка — рисунок появится в активном окне (например, "
        "Paint). Если нужны центр/границы экрана, сначала вызови get_screen_size. "
        "Формулы: круг x=cx+R*cos(2*pi*i/N), y=cy+R*sin(2*pi*i/N); восьмёрка "
        "x=cx+A*sin(t), y=cy+B*sin(t)*cos(t); звезда — чередование внешнего и "
        "внутреннего радиусов."
    ),
    params={
        "points": {
            "type": "array",
            "description": "Список точек [x, y] в пикселях, по порядку соединения",
            "items": {"type": "array", "items": {"type": "number"}},
        },
        "hold": {
            "type": "boolean",
            "description": (
                "Удерживать левую кнопку мыши во время движения (рисование). "
                "По умолчанию true."
            ),
        },
        "duration": {
            "type": "number",
            "description": (
                "Длительность всей траектории в секундах (0 — быстро). "
                "По умолчанию 1.0."
            ),
        },
        "close": {
            "type": "boolean",
            "description": "Замкнуть фигуру, добавив первую точку в конец. По умолчанию false.",
        },
    },
)
def draw_path(points, hold: bool = True, duration: float = 1.0, close: bool = False):
    """Ведёт курсор по списку точек; hold=True — с зажатой кнопкой (рисование)."""
    try:
        pts = []
        for p in points or []:
            if isinstance(p, dict):
                x, y = p.get("x"), p.get("y")
            else:
                x, y = p[0], p[1]
            if x is None or y is None:
                continue
            pts.append((int(x), int(y)))

        if not pts:
            return {"status": "error", "message": "Пустой список точек (points)."}

        if close and len(pts) >= 2:
            pts.append(pts[0])

        n = len(pts)
        delay = (duration / n) if duration and duration > 0 else 0

        pyautogui.moveTo(pts[0][0], pts[0][1], _pause=False)
        if hold:
            pyautogui.mouseDown()

        for (x, y) in pts[1:]:
            pyautogui.moveTo(x, y, _pause=False)
            if delay:
                time.sleep(delay)

        if hold:
            pyautogui.mouseUp()

        return {
            "status": "ok",
            "points": n,
            "hold": bool(hold),
            "closed": bool(close),
        }
    except pyautogui.FailSafeException:
        try:
            if hold:
                pyautogui.mouseUp()
        except Exception:
            pass
        return {
            "status": "error",
            "message": "Сработала аварийная защита (FailSafe), рисование прервано.",
        }
    except Exception as e:
        try:
            if hold:
                pyautogui.mouseUp()
        except Exception:
            pass
        return {"status": "error", "message": str(e)}


def _find_paint_windows():
    """Возвращает [(hwnd, title)] главных окон Paint (класс окна MSPaintApp)."""
    user32 = ctypes.windll.user32
    found = []
    EnumProc = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)

    def _cb(hwnd, lparam):
        cbuf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cbuf, 256)
        if cbuf.value == "MSPaintApp":
            tlen = user32.GetWindowTextLengthW(hwnd)
            title = ""
            if tlen:
                tbuf = ctypes.create_unicode_buffer(tlen + 1)
                user32.GetWindowTextW(hwnd, tbuf, tlen + 1)
                title = tbuf.value
            found.append((hwnd, title))
        return 1

    user32.EnumWindows(EnumProc(_cb), 0)
    return found


@tool(
    description=(
        "Открывает или выводит на передний план стандартный Paint Windows, "
        "разворачивает окно и возвращает прямоугольник холста. ОБЯЗАТЕЛЬНО "
        "вызывай перед рисованием (draw_path с hold=true), чтобы рисунок попал "
        "в Paint, а не в другое окно. В ответе есть rect {left, top, right, "
        "bottom} — вычисляй точки внутри него, оставляя ~120px сверху под панель."
    ),
    params={
        "maximize": {
            "type": "boolean",
            "description": "Развернуть окно Paint на весь экран (по умолчанию true)",
        },
    },
)
def open_paint(maximize: bool = True):
    """Открывает/фокусирует Paint и возвращает прямоугольник его окна."""
    if os.name != "nt":
        return {"status": "error", "message": "Инструмент доступен только в Windows."}
    try:
        user32 = ctypes.windll.user32
        wins = _find_paint_windows()
        launched = False
        if not wins:
            exe = os.path.join(
                os.environ.get("WINDIR", r"C:\Windows"), "System32", "mspaint.exe"
            )
            subprocess.Popen([exe])
            launched = True
            deadline = time.time() + 10
            while time.time() < deadline and not wins:
                time.sleep(0.3)
                wins = _find_paint_windows()

        if not wins:
            return {"status": "error", "message": "Не удалось найти окно Paint."}

        hwnd, title = wins[0]
        # Небольшая хитрость, чтобы Windows разрешила смену фокуса.
        user32.keybd_event(0x12, 0, 0, 0)
        user32.keybd_event(0x12, 0, 2, 0)
        user32.ShowWindow(hwnd, 3 if maximize else 9)
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
        time.sleep(0.4)

        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long),
                ("top", ctypes.c_long),
                ("right", ctypes.c_long),
                ("bottom", ctypes.c_long),
            ]

        rect = RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        return {
            "status": "ok",
            "title": title,
            "launched": launched,
            "rect": {
                "left": rect.left,
                "top": rect.top,
                "right": rect.right,
                "bottom": rect.bottom,
            },
            "hint": (
                "Рисуй внутри rect; оставь ~120px сверху под панель инструментов "
                "и ~20px по краям."
            ),
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


@tool(
    description=(
        "Рисует фигуру в Paint: САМ открывает/фокусирует и разворачивает Paint, "
        "затем ведёт курсор по точкам с зажатой кнопкой. УНИВЕРСАЛЬНЫЙ инструмент "
        "для рисования в Paint (звезда, круг, квадрат, восьмёрка, каляки). Точки "
        "задавай в долях 0..1 от области рисования: (0,0) — левый верхний угол "
        "холста, (1,1) — правый нижний. Открывать Paint отдельно и узнавать размер "
        "экрана НЕ нужно."
    ),
    params={
        "points": {
            "type": "array",
            "description": "Точки [u, v], каждая в диапазоне 0..1",
            "items": {"type": "array", "items": {"type": "number"}},
        },
        "hold": {
            "type": "boolean",
            "description": "Рисовать (зажимать ЛКМ). По умолчанию true.",
        },
        "duration": {
            "type": "number",
            "description": "Длительность рисования в секундах (по умолчанию 1.5).",
        },
        "close": {
            "type": "boolean",
            "description": "Замкнуть фигуру. По умолчанию false.",
        },
        "offset_x": {
            "type": "integer",
            "description": "Отступ области от левого края окна Paint (по умолчанию 60).",
        },
        "offset_y": {
            "type": "integer",
            "description": "Отступ сверху под панель инструментов (по умолчанию 150).",
        },
        "area_width": {
            "type": "integer",
            "description": "Ширина области рисования (по умолчанию 500).",
        },
        "area_height": {
            "type": "integer",
            "description": "Высота области рисования (по умолчанию 400).",
        },
    },
)
def draw_in_paint(
    points,
    hold: bool = True,
    duration: float = 1.5,
    close: bool = False,
    offset_x: int = 60,
    offset_y: int = 150,
    area_width: int = 500,
    area_height: int = 400,
):
    """Открывает Paint и рисует по нормализованным (0..1) или абсолютным точкам."""
    info = open_paint(maximize=True)
    if not isinstance(info, dict) or info.get("status") != "ok":
        return {
            "status": "error",
            "message": f"Paint не открылся: {info.get('message') if isinstance(info, dict) else info}",
        }

    rect = info.get("rect") or {}
    left = int(rect.get("left", 0))
    top = int(rect.get("top", 0))
    right = int(rect.get("right", left + 800))
    bottom = int(rect.get("bottom", top + 600))

    raw = []
    for p in points or []:
        if isinstance(p, dict):
            u, v = p.get("x"), p.get("y")
        else:
            u, v = p[0], p[1]
        if u is None or v is None:
            continue
        raw.append((float(u), float(v)))

    if not raw:
        return {"status": "error", "message": "Пустой список точек (points)."}

    # Если координаты выглядят как абсолютные (>1.5) — используем как есть.
    max_val = max(max(abs(u), abs(v)) for u, v in raw)
    normalized = max_val <= 1.5

    if normalized:
        ox = left + int(offset_x)
        oy = top + int(offset_y)
        aw = max(50, min(int(area_width), max(50, (right - left) - int(offset_x) - 40)))
        ah = max(50, min(int(area_height), max(50, (bottom - top) - int(offset_y) - 40)))
        abs_points = [[int(ox + u * aw), int(oy + v * ah)] for u, v in raw]
        area = {"x": ox, "y": oy, "w": aw, "h": ah}
    else:
        abs_points = [[int(u), int(v)] for u, v in raw]
        area = None

    time.sleep(0.4)
    res = draw_path(abs_points, hold=hold, duration=duration, close=close)
    if isinstance(res, dict):
        res["paint"] = info.get("title")
        res["normalized"] = normalized
        if area:
            res["area"] = area
    return res
    
# ==========================================


# ==========================================
# 3. FLASK СЕРВЕР И ЦИКЛ АГЕНТА
# ==========================================
@app.route('/chat', methods=['POST'])
def chat():
    data = request.json
    user_message = data.get("message", "")

    if not user_message:
        return jsonify({"error": "Поле 'message' обязательно"}), 400

    tools_used = []

    messages = [
        {
            "role": "system",
            "content": (
                "Ты агент с инструментами. Строго соблюдай правила маршрутизации:\n"
                "- Ты можешь вызывать инструменты последовательно: после получения "
                "результата при необходимости вызови следующий инструмент, пока "
                "задача не будет выполнена. Когда готов — ответь кратким текстом.\n"
                "- Если пользователь здоровается (привет, здравствуй, здорово, хай, "
                "приветствую, добрый день) — ОБЯЗАТЕЛЬНО вызови инструмент hello_user. "
                "Никогда не отвечай на приветствие собственным текстом.\n"
                "- Если спрашивают о погоде — вызови инструмент get_weather.\n"
                "- Если просят создать/записать файл — вызови write_text_file(path, content).\n"
                "- Если просят прочитать файл — вызови read_text_file(path).\n"
                "- Если спрашивают, где сейчас курсор мыши, или просят узнать координаты "
                "мыши — вызови get_mouse_position.\n"
                "- Если просят кликнуть мышью в конкретных координатах (например, "
                "'кликни в (500, 300)') — вызови click_mouse(x=500, y=300).\n"
                "- Если просят кликнуть мышью, но НЕ указывают координаты — сначала вызови "
                "get_mouse_position, чтобы узнать текущее положение, затем, если намерение "
                "пользователя понятно (например, 'кликни здесь'), вызови click_mouse "
                "без координат — он кликнет в текущей позиции курсора.\n"
                "- Если просят кликнуть правой/средней кнопкой или несколько раз — "
                "передай в click_mouse параметры button и clicks соответственно.\n"
                "- НИКОГДА не вызывай click_mouse без явных координат от пользователя, "
                "если не уверен, куда именно нужно кликнуть. В такой ситуации лучше "
                "задать пользователю уточняющий вопрос текстом.\n"
                "- Если просят переместить или навести мышь в координаты — вызови "
                "move_mouse(x, y).\n"
                "- Если просят узнать размер экрана или разрешение — вызови "
                "get_screen_size.\n"
                "- Если просят переместить мышь в центр экрана — вызови "
                "move_mouse_to_center (размер экрана узнавать не нужно).\n"
                "- Если просят нарисовать в Paint фигуру или рисунок (звезда, круг, "
                "квадрат, восьмёрка, каляка-маляка и т.п.) — вызови "
                "draw_in_paint(points, hold=true, close=...). Он САМ откроет и "
                "сфокусирует Paint. Точки задавай в долях 0..1 от области рисования: "
                "(0,0) — левый верхний угол холста, (1,1) — правый нижний. Открывать "
                "Paint и узнавать размер экрана НЕ нужно.\n"
                "- Если просят провести мышью без рисования (просто движение) — "
                "вычисли точки в пикселях и вызови draw_path(..., hold=false). Для "
                "ровного круга можно move_mouse_circle(radius, duration, hold).\n"
                "- Если просят просто открыть Paint — вызови open_paint().\n"
                "- В остальных случаях отвечай текстом.\n"
                "Пример: 'Привет' -> вызвать hello_user.\n"
                "Пример: 'погода в Москве' -> вызвать get_weather.\n"
                "Пример: 'Создай файл text.txt и напиши в нём Привет' "
                "-> вызвать write_text_file(path='text.txt', content='Привет').\n"
                "Пример: 'Где сейчас мышка?' -> вызвать get_mouse_position.\n"
                "Пример: 'Кликни в точку (500, 300)' -> вызвать click_mouse(x=500, y=300).\n"
                "Пример: 'Сделай правый клик в (100, 200)' "
                "-> вызвать click_mouse(x=100, y=200, button='right').\n"
                "Пример: 'Кликни дважды в (400, 400)' "
                "-> вызвать click_mouse(x=400, y=400, clicks=2).\n"
                "Пример: 'Кликни там, где сейчас курсор' -> сначала вызвать "
                "get_mouse_position (чтобы показать пользователю, где это), затем "
                "вызвать click_mouse без координат.\n"
                "Пример: 'Перемести мышь в (500, 300)' "
                "-> вызвать move_mouse(x=500, y=300).\n"
                "Пример: 'Какой размер экрана?' -> вызвать get_screen_size.\n"
                "Пример: 'Передвинь мышь в центр экрана' "
                "-> вызвать move_mouse_to_center.\n"
                "Пример: 'Проведи круг мышью' "
                "-> вызвать move_mouse_circle(radius=200, duration=1.5).\n"
                "Пример: 'Нарисуй звезду в Paint' -> вызвать draw_in_paint(points="
                "[[0.5,0.05],[0.61,0.35],[0.93,0.36],[0.67,0.56],[0.76,0.86],"
                "[0.5,0.68],[0.24,0.86],[0.33,0.56],[0.07,0.36],[0.39,0.35]], "
                "hold=true, close=true).\n"
                "Пример: 'Открой Paint' -> вызвать open_paint().\n"
                "Пример: 'Проведи восьмёркой' -> вычислить точки лемнискаты "
                "x=cx+A*sin(t), y=cy+B*sin(t)*cos(t) и вызвать "
                "draw_path(points=[[x0,y0],...], hold=false).\n"
                "Пример: 'Нарисуй каляку-маляку' -> придумать произвольные точки "
                "0..1 и вызвать draw_in_paint(points=[[u,v],...], hold=true).\n"
            ),
        },
        {"role": "user", "content": user_message}
    ]

    context_limit = get_context_limit()

    final_answer = ""
    hit_limit = False
    iterations = 0
    consumed_total = 0
    last_prompt = 0
    last_completion = 0
    last_total = 0
    total_tool_calls = 0

    try:
        # Итеративный цикл агента: модель -> инструменты -> модель ...
        for iteration in range(1, MAX_ITERATIONS + 1):
            iterations = iteration

            # Защита от переполнения: оцениваем размер текущего контекста.
            prompt_estimate = sum(
                estimate_tokens(m.get("content") or "")
                for m in messages
                if isinstance(m.get("content"), str)
            )
            available = context_limit - prompt_estimate - CONTEXT_SAFETY_MARGIN
            if available < 64:
                hit_limit = True
                if not final_answer:
                    final_answer = (
                        "Контекст почти исчерпан — останавливаюсь, чтобы не "
                        "переполнить модель. Сформулируйте запрос короче."
                    )
                break
            this_max_tokens = min(MAX_COMPLETION_TOKENS, available)

            response = client.chat.completions.create(
                model=MODEL_NAME,
                messages=messages,
                tools=TOOLS_SCHEMA,
                tool_choice="auto",
                temperature=0,
                max_tokens=this_max_tokens,
            )
            response_message = response.choices[0].message

            _u = getattr(response, "usage", None)
            pt = getattr(_u, "prompt_tokens", None) if _u else None
            ct = getattr(_u, "completion_tokens", None) if _u else None
            tt = getattr(_u, "total_tokens", None) if _u else None
            last_prompt = pt if pt is not None else prompt_estimate
            last_completion = ct if ct is not None else 0
            last_total = tt if tt is not None else (last_prompt + last_completion)
            consumed_total += last_total

            messages.append(response_message.model_dump(exclude_none=True))

            if not response_message.tool_calls:
                final_answer = response_message.content or ""
                break

            executed_this_round = 0
            verbatim_outputs = []
            for tool_call in response_message.tool_calls[:MAX_TOOL_CALLS]:
                if total_tool_calls >= MAX_TOTAL_TOOL_CALLS:
                    break
                function_name = tool_call.function.name
                raw_args = tool_call.function.arguments
                try:
                    function_args = json.loads(raw_args) if raw_args else {}
                except (json.JSONDecodeError, TypeError):
                    function_args = {}
                if not isinstance(function_args, dict):
                    function_args = {}

                print(f"[Агент] Вызывает инструмент: {function_name} с аргументами {function_args}")

                if function_name in REGISTERED_TOOLS:
                    function_to_call = REGISTERED_TOOLS[function_name]
                    function_response = function_to_call(**function_args)
                    result_str = str(function_response)
                    tools_used.append({
                        "name": function_name,
                        "arguments": function_args,
                        "result": result_str,
                    })
                    if function_name in VERBATIM_TOOLS:
                        verbatim_outputs.append(result_str)

                    # Обрезаем результат для контекста, чтобы не съесть лимит.
                    ctx_result = result_str
                    if len(ctx_result) > MAX_TOOL_RESULT_CHARS:
                        cut = len(ctx_result) - MAX_TOOL_RESULT_CHARS
                        ctx_result = (
                            ctx_result[:MAX_TOOL_RESULT_CHARS]
                            + f"\n... [обрезано {cut} символов]"
                        )

                    messages.append({
                        "tool_call_id": tool_call.id,
                        "role": "tool",
                        "name": function_name,
                        "content": ctx_result,
                    })
                    total_tool_calls += 1
                    executed_this_round += 1

            if verbatim_outputs:
                # Инструмент отдал готовый пользовательский ответ — не даём
                # модели перефразировать его на следующей итерации.
                final_answer = "\n".join(verbatim_outputs)
                break

            if executed_this_round == 0:
                final_answer = response_message.content or ""
                break
        else:
            # Цикл закончился без break — исчерпан лимит итераций.
            hit_limit = True
            if not final_answer:
                final_answer = (
                    "Достигнут лимит итераций агента — задача не завершена. "
                    "Уточните или упростите запрос."
                )

        if not final_answer and tools_used:
            final_answer = "\n".join(
                t["result"] for t in tools_used[-MAX_TOOL_CALLS:]
            )

        usage_info = {
            "prompt_tokens": last_prompt,
            "completion_tokens": last_completion,
            "total_tokens": last_total,
            "tokens_consumed": consumed_total,
            "context_limit": context_limit,
            "context_remaining": max(0, context_limit - last_total),
            "max_output_tokens": MAX_COMPLETION_TOKENS,
            "model_calls": iterations,
            "tool_calls": total_tool_calls,
            "hit_limit": hit_limit,
        }

        return jsonify({
            "response": final_answer,
            "tools": tools_used,
            "usage": usage_info,
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    # Запуск сервера на порту 5000
    app.run(port=5000, debug=True)