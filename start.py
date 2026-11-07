#!/usr/bin/env python3
"""
Один скрипт для запуска всего стека агента.

Запуск из консоли:
    python start.py
Двойным кликом (без консоли):
    start.bat          — с видимым логом
    agent.vbs          — полностью скрыто, только окно чата

Режимы:
    python start.py              — LM Studio + сервер + GUI (по умолчанию)
    python start.py server       — LM Studio + только сервер
    python start.py gui|all      — то же, что по умолчанию
    python start.py --check      — проверить зависимости и состояние lms
    python start.py --no-lms     — не трогать LM Studio (запущен вручную)

Стек поднимается автоматически:
    1. ставит недостающие pip-пакеты;
    2. запускает сервер модели через CLI `lms server start`;
    3. загружает модель через `lms load` (имя и порт читаются из program.py);
    4. запускает Flask на порту 5000;
    5. запускает GUI, а при его закрытии останавливает Flask.
"""

import sys
import subprocess
import os
import platform
import time
import ast
import json
import shutil
import importlib.util
import argparse
import signal

# ==========================================
# 1. ПУТИ И НАСТРОЙКИ
# ==========================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROGRAM_PY = os.path.join(SCRIPT_DIR, "program.py")
GUI_PY = os.path.join(SCRIPT_DIR, "gui.py")
AGENT_URL = "http://127.0.0.1:5000/chat"

# Настройки по умолчанию, если их не удалось прочитать из program.py
DEFAULT_MODEL_NAME = "qwen3.5-9b"
DEFAULT_MODEL_PORT = 1234

# Параметры загрузки модели через lms
LMS_GPU = "max"            # полное снятие на GPU
LMS_CONTEXT = "16384"      # длина контекста
LMS_LOAD_TIMEOUT = 600     # загрузка большой модели может идти долго

# ==========================================
# 2. УТИЛИТЫ
# ==========================================
def color(text: str, code: int) -> str:
    """Возвращает ANSI-строку с цветом, если терминал поддерживает."""
    if platform.system() == "Windows":
        os.system("")  # включаем VT-обработку
    return f"\033[{code}m{text}\033[0m"

def ok(text: str):
    print(color(f"  [OK] {text}", 32))

def warn(text: str):
    print(color(f"  [WARN] {text}", 33))

def err(text: str):
    print(color(f"  [ERR] {text}", 31))

def info(text: str):
    print(color(f"  [INFO] {text}", 36))

def check_dependency(module_name: str, pip_name: str = None):
    """Проверяет, установлен ли модуль. pip_name — как называть в pip install."""
    if pip_name is None:
        pip_name = module_name
    if importlib.util.find_spec(module_name) is not None:
        return True
    return False

def install_pip(pkg: str):
    """Устанавливает пакет через pip текущего интерпретатора."""
    info(f"Устанавливаю {pkg}...")
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "-q", pkg],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    ok(f"{pkg} установлен.")

def check_ports(ports: list[int]):
    """Проверяет, слушаются ли порты (возвращает set открытых портов)."""
    import socket
    opened = set()
    for port in ports:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.5)
        result = s.connect_ex(("127.0.0.1", port))
        s.close()
        if result == 0:
            opened.add(port)
    return opened

def read_agent_config():
    """Извлекает MODEL_NAME и порт сервера модели из program.py.
    
    Возвращает (model_name, port). Если не удалось — (DEFAULT_MODEL_NAME, DEFAULT_MODEL_PORT).
    """
    model_name = DEFAULT_MODEL_NAME
    port = DEFAULT_MODEL_PORT
    try:
        with open(PROGRAM_PY, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        if t.id == "MODEL_NAME":
                            v = node.value
                            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                                model_name = v.value
                        elif t.id == "API_BASE":
                            v = node.value
                            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                                # http://127.0.0.1:1234/v1 → 1234
                                try:
                                    port = int(v.value.split(":")[2].split("/")[0])
                                except (IndexError, ValueError):
                                    pass
    except Exception:
        pass
    return model_name, port


def run_lms(args: list, timeout: int = 60, capture: bool = True):
    """Выполняет команду `lms ...` и возвращает (returncode, stdout, stderr)."""
    try:
        kw = {"capture_output": capture, "text": True, "encoding": "utf-8", "errors": "replace"}
        res = subprocess.run(
            ["lms"] + args,
            timeout=timeout,
            **kw,
        )
        return res.returncode, (res.stdout or "").strip(), (res.stderr or "").strip()
    except FileNotFoundError:
        return -1, "", "lms не найден в PATH"
    except subprocess.TimeoutExpired:
        return 124, "", f"Команда lms {' '.join(args)} не завершилась за {timeout}с"


def is_model_loaded(model_name: str, port: int) -> bool:
    """Проверяет, загружена ли модель в памяти через `lms ps --json`."""
    rc, out, _ = run_lms(["ps", "--json"], timeout=10)
    if rc != 0:
        return False
    try:
        loaded = json.loads(out or "[]")
        for m in loaded:
            if m.get("identifier") == model_name or m.get("modelKey") == model_name:
                return True
    except json.JSONDecodeError:
        pass
    return False


def ensure_lm_studio(model_name: str, port: int):
    """Запускает LM Studio CLI: сервер + загружает модель, если нужно.
    
    Возвращает True если сервер доступен, False — нет.
    """
    lms = shutil.which("lms")
    if lms is None:
        warn("CLI 'lms' не найден в PATH. "
             "Установите LM Studio и запустите 'lms bootstrap', либо "
             "запустите LM Studio вручную → Developer → Start Server.")
        return wait_for_server(f"http://127.0.0.1:{port}/v1/models", port, timeout=8, name="LM Studio")

    # --- 1. Запуск сервера (если не запущен) ---
    ports = check_ports([port])
    if port not in ports:
        info(f"Запускаю LM Studio server на порту {port}...")
        rc, _out, _err = run_lms(["server", "start", "-p", str(port)], timeout=30)
        if rc != 0:
            warn(f"lms server start вернул код {rc}. Возможно, сервер уже запущен.")
        if not wait_for_server(f"http://127.0.0.1:{port}/v1/models", port, timeout=15, name="LM Studio server"):
            warn("LM Studio сервер не стал доступен.")
            return False
        ok(f"LM Studio server на порту {port} запущен.")
    else:
        ok(f"LM Studio server на порту {port} уже работает.")

    # --- 2. Загрузка модели (если не в памяти) ---
    if is_model_loaded(model_name, port):
        ok(f"Модель '{model_name}' уже загружена в память.")
    else:
        info(f"Загружаю модель '{model_name}' в память (GPU {LMS_GPU}, контекст {LMS_CONTEXT})...")
        rc, _out, _err = run_lms(
            ["load", model_name, "-y", f"--gpu={LMS_GPU}", f"-c", LMS_CONTEXT],
            timeout=LMS_LOAD_TIMEOUT,
            capture=False,   # прогресс-бар в консоль
        )
        if rc != 0:
            warn(f"lms load вернул код {rc}. Модель может не работать.")
            return True  # сервер работает, но без модели
        ok(f"Модель '{model_name}' загружена.")
        # Проверим ещё раз
        if not is_model_loaded(model_name, port):
            warn("Модель не появилась в 'lms ps'. Проверьте вывод выше.")

    return True

def terminate_tree(proc):
    """Останавливает процесс и всех его потомков.
    
    Flask с debug=True запускает reloader — порождает дочерний python.
    Обычный terminate() не трогает потомков, и порт остаётся занятым.
    """
    if proc is None or proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            import os as _os
            _os.killpg(os.getpgid(proc.pid), 9)
    except Exception:
        pass
    try:
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass

# ==========================================
# 3. ПРОВЕРКА ЗАВИСИМОСТЕЙ
# ==========================================
REQUIRED = {
    "flask": "flask",
    "requests": "requests",
}

GUI_REQUIRED = {
    "PyQt6": "PyQt6",
}

def check_dependencies():
    """Устанавливает недостающие зависимости."""
    all_ok = True
    for module, pip in REQUIRED.items():
        if not check_dependency(module, pip):
            all_ok = False
    if not all_ok:
        warn("Некоторые зависимости отсутствуют. Устанавливаю...")
        for module, pip in REQUIRED.items():
            if not check_dependency(module, pip):
                install_pip(pip)
        ok("Все базовые зависимости установлены.")

def check_gui_dependencies():
    """Устанавливает зависимости для GUI."""
    all_ok = True
    for module, pip in GUI_REQUIRED.items():
        if not check_dependency(module, pip):
            all_ok = False
    if not all_ok:
        warn("Зависимости GUI отсутствуют. Устанавливаю...")
        for module, pip in GUI_REQUIRED.items():
            if not check_dependency(module, pip):
                install_pip(pip)
        ok("Все GUI-зависимости установлены.")

# ==========================================
# 4. ОЖИДАНИЕ СЕРВЕРОВ
# ==========================================
def wait_for_port(port: int, timeout: int = 15, name: str = "Сервер"):
    """Ждёт, пока порт начнёт слушаться. Без HTTP-запросов.
    
    Используется для Flask: эндпоинт /chat принимает только POST, и GET
    вернул бы 405, что ошибочно считалось бы "сервер не работает".
    """
    info(f"Жду {name} на порту {port} (таймаут {timeout}с)...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port in check_ports([port]):
            ok(f"{name} на порту {port} доступен.")
            return True
        time.sleep(0.4)
    warn(f"{name} на порту {port} не слушается за {timeout}с.")
    return False

def wait_for_server(url: str, port: int, timeout: int = 10, name: str = "Сервер"):
    """Ждёт HTTP-ответ от сервера. Любой HTTP-статус = сервер живой."""
    import urllib.error
    info(f"Жду {name} на порту {port} (таймаут {timeout}с)...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            import urllib.request
            urllib.request.urlopen(url, timeout=2)
            ok(f"{name} на порту {port} доступен.")
            return True
        except urllib.error.HTTPError:
            # Сервер ответил (404/405 и т.п.) — процесс живой
            ok(f"{name} на порту {port} доступен.")
            return True
        except Exception:
            time.sleep(0.5)
    warn(f"{name} на порту {port} не доступен за {timeout}с.")
    return False

# ==========================================
# 5. ЗАПУСК КОМАНД
# ==========================================
def run_server():
    """Запускает Flask-сервер."""
    if not os.path.isfile(PROGRAM_PY):
        err(f"Файл {PROGRAM_PY} не найден!")
        sys.exit(1)
    info(f"Запускаю сервер на http://127.0.0.1:5000")
    # Запускаем в отдельном процессе; Ctrl+C завершит и его
    proc = subprocess.Popen(
        [sys.executable, PROGRAM_PY],
        cwd=SCRIPT_DIR,
    )
    # Регистрируем обработчик Ctrl+C
    def signal_handler(sig, frame):
        info("Прерывание... Завершаю сервер.")
        terminate_tree(proc)
        sys.exit(0)
    signal.signal(signal.SIGINT, signal_handler)

    # Ждём запуска сервера
    if wait_for_port(5000, timeout=15, name="Flask-сервер"):
        print()
        print(color("=" * 50, 36))
        print(color("  Сервер запущен. Press Ctrl+C для выхода.", 36))
        print(color("  Быстрая проверка из другой консоли:", 36))
        print(
            color(
                "  python -c \"import requests;print(requests.post('%s',json={'message':'Привет'}).json())\""
                % AGENT_URL,
                36,
            )
        )
        print(color("=" * 50, 36))
        print()
        proc.wait()
    else:
        err("Сервер не запустился.")
        terminate_tree(proc)
        sys.exit(1)

def run_all():
    """Запускает сервер + GUI."""
    if not os.path.isfile(GUI_PY):
        err(f"Файл {GUI_PY} не найден!")
        sys.exit(1)

    # Сначала сервер
    ports = check_ports([5000])
    if 5000 not in ports:
        warn("Сервер не запущен. Запускаю...")
        proc = subprocess.Popen(
            [sys.executable, PROGRAM_PY],
            cwd=SCRIPT_DIR,
        )
        if not wait_for_port(5000, timeout=15, name="Flask-сервер"):
            err("Сервер не запустился.")
            terminate_tree(proc)
            sys.exit(1)
    else:
        proc = None

    # Запускаем GUI
    info("Запускаю GUI-клиент...")
    gui_proc = subprocess.Popen(
        [sys.executable, GUI_PY],
        cwd=SCRIPT_DIR,
    )

    # Ждём GUI; при завершении закрываем сервер
    gui_proc.wait()

    if proc:
        info("Завершаю сервер...")
        terminate_tree(proc)

# ==========================================
# 6. ГЛАВНЫЙ ENTRY POINT
# ==========================================
def main():
    parser = argparse.ArgumentParser(
        description="Запуск агента: сервер и GUI.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Примеры:\n"
            "  python start.py              — сервер + GUI (то же, что all)\n"
            "  python start.py server       — только сервер\n"
            "  python start.py gui          — сервер + GUI\n"
            "  python start.py all          — сервер + GUI\n"
            "  python start.py --check      — проверить зависимости и lms\n"
            "  python start.py --no-lms     — не трогать LM Studio (он сам запустится)\n"
        ),
    )
    parser.add_argument(
        "mode",
        nargs="?",
        default="all",
        choices=["server", "gui", "all"],
        help="Режим запуска (по умолчанию: all)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Только проверить зависимости и lms",
    )
    parser.add_argument(
        "--no-lms",
        action="store_true",
        help="Не запускать LM Studio через lms (если уже запущен вручную)",
    )

    args = parser.parse_args()

    # Читаем настройки из program.py
    model_name, model_port = read_agent_config()

    # Режим проверки
    if args.check:
        print(color("\nПроверка...", 36))
        check_dependencies()
        if args.mode in ("gui", "all"):
            check_gui_dependencies()
        ensure_lm_studio(model_name, model_port)
        print(color("Готово.\n", 32))
        return

    # Проверка зависимостей
    print(color("Проверка зависимостей...", 36))
    check_dependencies()
    if args.mode in ("gui", "all"):
        check_gui_dependencies()

    # LM Studio через CLI
    if args.no_lms:
        info("--no-lms: пропускаю запуск LM Studio через lms")
    else:
        ensure_lm_studio(model_name, model_port)

    # Запуск
    print()
    if args.mode == "server":
        run_server()
    elif args.mode in ("gui", "all"):
        run_all()


if __name__ == "__main__":
    main()
