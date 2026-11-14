"""
GUI-клиент для общения с агентом.

Запуск в двух терминалах:
  1) python program.py   # Flask-сервер агента на :5000
  2) python gui.py        # это окно

Окно общается с эндпоинтом POST /chat, запросы выполняются в отдельном
потоке, поэтому интерфейс не подвисает пока модель думает.
"""

import sys
import time
from datetime import datetime

import requests
from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetrics
from PyQt6.QtWidgets import (
    QApplication,
    QGraphicsDropShadowEffect,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

# ==========================================
# 1. НАСТРОЙКИ
# ==========================================
AGENT_URL = "http://127.0.0.1:5000/chat"
REQUEST_TIMEOUT = 180  # локальные модели могут думать долго
MAX_BUBBLE_WIDTH = 560

STYLE = """
QWidget#Root {
    background-color: #eef1f8;
}
QFrame#Header {
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #4f6df5, stop:1 #7b5cf0);
}
QLabel#AppTitle {
    color: #ffffff;
    font-size: 17px;
    font-weight: 600;
}
QLabel#AppSubtitle {
    color: rgba(255, 255, 255, 0.75);
    font-size: 12px;
}
QLabel#Status {
    color: #6b7280;
    font-size: 11px;
    padding: 0px 4px;
}
QScrollArea#ChatArea {
    background-color: transparent;
    border: none;
}
QWidget#ChatViewport {
    background-color: transparent;
}
QFrame#UserBubble {
    background-color: #4f6df5;
    border-radius: 16px;
}
QLabel#UserText {
    color: #ffffff;
    font-size: 14px;
}
QFrame#BotBubble {
    background-color: #ffffff;
    border: 1px solid #e3e7f0;
    border-radius: 16px;
}
QLabel#BotText {
    color: #1f2430;
    font-size: 14px;
}
QLabel#SystemText {
    color: #b42318;
    font-size: 13px;
    padding: 4px 8px;
}
QFrame#Composer {
    background-color: #ffffff;
    border: 1px solid #dfe3ee;
    border-radius: 14px;
}
QTextEdit#Input {
    background-color: transparent;
    border: none;
    color: #1f2430;
    font-size: 14px;
    padding: 6px;
}
QTextEdit#Input:focus {
    border: none;
}
QPushButton#Send {
    background-color: #4f6df5;
    color: #ffffff;
    border: none;
    border-radius: 10px;
    font-size: 13px;
    font-weight: 600;
    padding: 8px 18px;
}
QPushButton#Send:hover {
    background-color: #3f5ae0;
}
QPushButton#Send:pressed {
    background-color: #354bc4;
}
QPushButton#Send:disabled {
    background-color: #c3cbe8;
    color: #f2f4fb;
}
QScrollBar:vertical {
    background: transparent;
    width: 8px;
    margin: 0;
}
QScrollBar::handle:vertical {
    background: #c8cfe2;
    border-radius: 4px;
    min-height: 24px;
}
QScrollBar::handle:vertical:hover {
    background: #a9b3cd;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}
QFrame#LogPanel {
    background-color: #ffffff;
    border-top: 1px solid #e3e7f0;
}
QFrame#LogHeader {
    background-color: #f5f7fc;
}
QLabel#LogTitle {
    color: #4f6df5;
    font-size: 12px;
    font-weight: 600;
}
QPushButton#LogToggle {
    background-color: transparent;
    border: none;
    color: #6b7280;
    font-size: 11px;
}
QPushButton#LogToggle:hover {
    color: #4f6df5;
}
QPlainTextEdit#LogView {
    background-color: #1f2430;
    color: #c8d3f5;
    border: none;
    font-family: Consolas, "Cascadia Mono", monospace;
    font-size: 12px;
    padding: 6px;
    selection-background-color: #4f6df5;
}
QFrame#UsagePanel {
    background-color: #ffffff;
    border-bottom: 1px solid #e3e7f0;
}
QLabel#UsageLabel {
    color: #4a5568;
    font-size: 11px;
}
QProgressBar#UsageBar {
    background-color: #eef1f8;
    border: 1px solid #dfe3ee;
    border-radius: 7px;
    height: 14px;
    text-align: center;
    font-size: 10px;
    color: #1f2430;
}
QProgressBar#UsageBar::chunk {
    background-color: #4f6df5;
    border-radius: 7px;
}
"""


# ==========================================
# 2. ФОНОВЫЙ ЗАПРОС К АГЕНТУ
# ==========================================
class ChatWorker(QThread):
    """Отправляет сообщение серверу агента, не блокируя GUI."""

    reply = pyqtSignal(str)
    tools = pyqtSignal(list)
    stats = pyqtSignal(dict)
    failure = pyqtSignal(str)

    def __init__(self, message: str, parent=None):
        super().__init__(parent)
        self._message = message

    def run(self):
        try:
            response = requests.post(
                AGENT_URL,
                json={"message": self._message},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.exceptions.ConnectionError:
            self.failure.emit("Сервер агента недоступен. Запусти program.py")
            return
        except requests.exceptions.Timeout:
            self.failure.emit("Превышено время ожидания ответа модели.")
            return
        except Exception as exc:  # noqa: BLE001 - показываем любую ошибку в окне
            self.failure.emit(f"Ошибка: {exc}")
            return

        try:
            payload = response.json()
        except ValueError:
            self.failure.emit(
                f"Некорректный ответ сервера (HTTP {response.status_code})."
            )
            return

        if "error" in payload:
            self.failure.emit(str(payload["error"]))
            return
        if not response.ok:
            self.failure.emit(f"HTTP {response.status_code}")
            return

        self.stats.emit(payload.get("usage", {}))
        self.tools.emit(payload.get("tools", []))
        self.reply.emit(str(payload.get("response", "")))


# ==========================================
# 3. ЭЛЕМЕНТЫ ИНТЕРФЕЙСА
# ==========================================
class MessageBubble(QFrame):
    """Пузырь сообщения: синий справа от пользователя, белый слева от агента."""

    H_PADDING = 14
    V_PADDING = 10

    def __init__(self, text: str, role: str, parent=None):
        super().__init__(parent)
        is_user = role == "user"
        self.setObjectName("UserBubble" if is_user else "BotBubble")
        self.setMaximumWidth(MAX_BUBBLE_WIDTH)
        self.setSizePolicy(
            QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Minimum
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(self.H_PADDING, self.V_PADDING,
                                  self.H_PADDING, self.V_PADDING)
        layout.setSpacing(0)

        self.label = QLabel(text)
        self.label.setObjectName("UserText" if is_user else "BotText")
        self.label.setWordWrap(True)
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        # Ключ к переносу: ограничиваем ширину самого текста,
        # чтобы QLabel ломал строки, а не вылезал за пузырь.
        self.label.setMaximumWidth(MAX_BUBBLE_WIDTH - 2 * self.H_PADDING)
        self.label.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum
        )
        layout.addWidget(self.label)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(18)
        shadow.setOffset(0, 2)
        shadow.setColor(QColor(31, 41, 70, 30))
        self.setGraphicsEffect(shadow)

        self._reconfigure()

    # ---------- корректный авторазмер ----------
    def _reconfigure(self):
        """Точные размеры пузыря и метки под текущий текст (без обрезки).

        Рассчитываем высоту переноса через QFontMetrics и жёстко задаём
        размеры — так пузырь не зависит от капризов layout-переговоров
        QLabel/QVBoxLayout и текст никогда не обрезается.
        """
        fm = self.label.fontMetrics()
        text = self.label.text()
        lines = text.splitlines() or [""]
        natural = max(fm.horizontalAdvance(line) for line in lines)
        max_inner = MAX_BUBBLE_WIDTH - 2 * self.H_PADDING
        inner = max(1, min(natural, max_inner))

        # Фиксируем ширину, но перед измерением снимаем ограничения по
        # высоте: иначе heightForWidth() клампится к текущей fixed-высоте
        # и пузырь «растёт» при каждом пересчёте (например, у «печатает…»).
        self.label.setFixedWidth(inner)
        self.label.setMinimumHeight(0)
        self.label.setMaximumHeight(16_777_215)
        text_h = self.label.heightForWidth(inner)
        self.label.setFixedHeight(text_h)

        self.setFixedSize(inner + 2 * self.H_PADDING,
                          text_h + 2 * self.V_PADDING)

    def set_text(self, text: str):
        """Обновить текст с пересчётом размера пузыря."""
        self.label.setText(text)
        self._reconfigure()

    def showEvent(self, event):  # noqa: N802 - имя из API Qt
        # Пересчитываем после полировки/применения стилей, когда шрифт
        # метки окончательный. Иначе размеры считаются по дефолтному шрифту.
        super().showEvent(event)
        self._reconfigure()


class ChatInput(QTextEdit):
    """Многострочное поле: Enter - отправить, Shift+Enter - перенос строки."""

    submit = pyqtSignal()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if not (event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
                event.accept()
                self.submit.emit()
                return
        super().keyPressEvent(event)


# ==========================================
# 4. ГЛАВНОЕ ОКНО
# ==========================================
class ChatWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("Root")
        self.setWindowTitle("Agent Chat")
        self.setMinimumSize(640, 560)
        self.resize(760, 680)
        self.setFont(QFont("Segoe UI", 10))
        self.setStyleSheet(STYLE)

        self._worker = None
        self._typing_bubble = None
        self._request_started = None
        self._typing_timer = QTimer(self)
        self._typing_timer.setInterval(400)
        self._typing_timer.timeout.connect(self._tick_typing)

        self._build_ui()

    # ---------- сборка интерфейса ----------
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Шапка
        header = QFrame()
        header.setObjectName("Header")
        header.setFixedHeight(64)
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(20, 10, 20, 10)
        header_layout.setSpacing(2)

        title = QLabel("Agent Chat")
        title.setObjectName("AppTitle")
        subtitle = QLabel(AGENT_URL)
        subtitle.setObjectName("AppSubtitle")
        header_layout.addWidget(title)
        header_layout.addWidget(subtitle)
        root.addWidget(header)

        # Панель расхода контекста и токенов
        self.usage_panel = QFrame()
        self.usage_panel.setObjectName("UsagePanel")
        usage_layout = QHBoxLayout(self.usage_panel)
        usage_layout.setContentsMargins(20, 6, 20, 6)
        usage_layout.setSpacing(16)

        self.usage_req = QLabel("Запрос: —")
        self.usage_req.setObjectName("UsageLabel")
        self.usage_resp = QLabel("Ответ: —")
        self.usage_resp.setObjectName("UsageLabel")
        self.usage_ctx = QLabel("Контекст: — / —")
        self.usage_ctx.setObjectName("UsageLabel")
        self.usage_iter = QLabel("Итераций: —")
        self.usage_iter.setObjectName("UsageLabel")

        self.usage_bar = QProgressBar()
        self.usage_bar.setObjectName("UsageBar")
        self.usage_bar.setRange(0, 100)
        self.usage_bar.setValue(0)
        self.usage_bar.setTextVisible(True)
        self.usage_bar.setFixedWidth(160)

        usage_layout.addWidget(self.usage_req)
        usage_layout.addWidget(self.usage_resp)
        usage_layout.addStretch(1)
        usage_layout.addWidget(self.usage_ctx)
        usage_layout.addWidget(self.usage_iter)
        usage_layout.addWidget(self.usage_bar)
        root.addWidget(self.usage_panel)

        # Лента сообщений
        self.chat_area = QScrollArea()
        self.chat_area.setObjectName("ChatArea")
        self.chat_area.setWidgetResizable(True)

        self.chat_viewport = QWidget()
        self.chat_viewport.setObjectName("ChatViewport")
        self.chat_layout = QVBoxLayout(self.chat_viewport)
        self.chat_layout.setContentsMargins(20, 16, 20, 8)
        self.chat_layout.setSpacing(10)
        self.chat_layout.addStretch(1)

        self.chat_area.setWidget(self.chat_viewport)
        root.addWidget(self.chat_area, 1)

        # Панель лога инструментов — видно, что вызывает агент
        self.log_panel = QFrame()
        self.log_panel.setObjectName("LogPanel")
        log_layout = QVBoxLayout(self.log_panel)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.setSpacing(0)

        log_header = QFrame()
        log_header.setObjectName("LogHeader")
        log_header.setFixedHeight(30)
        log_header_layout = QHBoxLayout(log_header)
        log_header_layout.setContentsMargins(20, 4, 16, 4)
        log_header_layout.setSpacing(8)

        log_title = QLabel("Лог инструментов")
        log_title.setObjectName("LogTitle")
        self.log_toggle = QPushButton("Скрыть")
        self.log_toggle.setObjectName("LogToggle")
        self.log_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.log_toggle.clicked.connect(self._toggle_log)

        log_header_layout.addWidget(log_title)
        log_header_layout.addStretch(1)
        log_header_layout.addWidget(self.log_toggle)

        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("LogView")
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(1000)
        self.log_view.setFixedHeight(130)

        log_layout.addWidget(log_header)
        log_layout.addWidget(self.log_view)
        root.addWidget(self.log_panel)

        # Панель ввода
        composer_host = QVBoxLayout()
        composer_host.setContentsMargins(20, 6, 20, 16)

        composer = QFrame()
        composer.setObjectName("Composer")
        composer_layout = QHBoxLayout(composer)
        composer_layout.setContentsMargins(10, 6, 10, 6)
        composer_layout.setSpacing(10)

        self.input = ChatInput()
        self.input.setObjectName("Input")
        self.input.setPlaceholderText(
            "Напиши сообщение... (Enter - отправить, Shift+Enter - новая строка)"
        )
        self.input.setFixedHeight(56)
        self.input.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.input.submit.connect(self.send_message)

        self.send_btn = QPushButton("Отправить")
        self.send_btn.setObjectName("Send")
        self.send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.send_btn.setFixedHeight(38)
        self.send_btn.clicked.connect(self.send_message)

        composer_layout.addWidget(self.input, 1)
        composer_layout.addWidget(self.send_btn, 0, Qt.AlignmentFlag.AlignBottom)

        status_row = QHBoxLayout()
        self.status = QLabel("Готов")
        self.status.setObjectName("Status")
        status_row.addWidget(self.status)
        status_row.addStretch(1)

        composer_host.addWidget(composer)
        composer_host.addLayout(status_row)
        root.addLayout(composer_host)

    # ---------- лента сообщений ----------
    def _insert_widget(self, widget, align_right: bool):
        """Вставляет виджет в ленту перед растяжкой, выравнивая по нужному краю."""
        if align_right:
            self.chat_layout.insertWidget(
                self.chat_layout.count() - 1, widget, 0, Qt.AlignmentFlag.AlignRight
            )
        else:
            self.chat_layout.insertWidget(
                self.chat_layout.count() - 1, widget, 0, Qt.AlignmentFlag.AlignLeft
            )

    def _add_user_message(self, text: str):
        self._insert_widget(MessageBubble(text, "user"), align_right=True)
        self._scroll_to_bottom()

    def _add_bot_message(self, text: str):
        self._insert_widget(MessageBubble(text, "bot"), align_right=False)
        self._scroll_to_bottom()

    def _add_error(self, text: str):
        label = QLabel(text)
        label.setObjectName("SystemText")
        label.setWordWrap(True)
        self._insert_widget(label, align_right=False)
        self._scroll_to_bottom()

    def _scroll_to_bottom(self):
        QTimer.singleShot(0, lambda: self.chat_area.verticalScrollBar().setValue(
            self.chat_area.verticalScrollBar().maximum()
        ))

    # ---------- индикатор "печатает" ----------
    def _show_typing(self):
        if self._typing_bubble is not None:
            return
        # Размер фиксируем по самому длинному варианту ("..."), чтобы
        # смена количества точек не меняла габариты пузыря.
        self._typing_bubble = MessageBubble("Агент печатает...", "bot")
        self._insert_widget(self._typing_bubble, align_right=False)
        self._typing_dots = 0
        self._typing_timer.start()
        self._scroll_to_bottom()

    def _tick_typing(self):
        if self._typing_bubble is None:
            self._typing_timer.stop()
            return
        self._typing_dots = (self._typing_dots + 1) % 4
        # Меняем только текст, не пересчитывая размер (он уже максимальный).
        self._typing_bubble.label.setText(
            "Агент печатает" + "." * self._typing_dots
        )

    def _hide_typing(self):
        self._typing_timer.stop()
        if self._typing_bubble is not None:
            self._typing_bubble.setParent(None)
            self._typing_bubble.deleteLater()
            self._typing_bubble = None

    # ---------- отправка ----------
    def send_message(self):
        text = self.input.toPlainText().strip()
        if not text or self._worker is not None:
            return

        self._add_user_message(text)
        self._log(f"» {text}")
        self.input.clear()
        self._set_busy(True)
        self._show_typing()

        self._worker = ChatWorker(text, parent=self)
        self._worker.stats.connect(self._on_stats)
        self._worker.tools.connect(self._on_tools)
        self._worker.reply.connect(self._on_reply)
        self._worker.failure.connect(self._on_failure)
        self._worker.finished.connect(self._on_finished)
        self._request_started = time.perf_counter()
        self._worker.start()

    def _on_reply(self, text: str):
        if text:
            self._add_bot_message(text)
        else:
            self._add_error("Модель вернула пустой ответ.")
        self.status.setText("Готов")

    def _on_failure(self, message: str):
        self._log(f"✗ {message}{self._elapsed_suffix()}")
        self._add_error(message)
        self.status.setText("Ошибка")

    # ---------- расход контекста и токенов ----------
    def _on_stats(self, usage):
        if not isinstance(usage, dict) or not usage:
            return
        prompt = usage.get("prompt_tokens")
        comp = usage.get("completion_tokens")
        total = usage.get("total_tokens")
        limit = usage.get("context_limit")

        self.usage_req.setText(
            f"Запрос: {prompt} ток." if prompt is not None else "Запрос: —"
        )
        self.usage_resp.setText(
            f"Ответ: {comp} ток." if comp is not None else "Ответ: —"
        )
        calls = usage.get("model_calls")
        self.usage_iter.setText(
            f"Итераций: {calls}" if calls is not None else "Итераций: —"
        )
        if limit:
            used = total if isinstance(total, int) else 0
            pct = min(100, max(0, int(round(used * 100 / limit))))
            self.usage_ctx.setText(f"Контекст: {used} / {limit} ({pct}%)")
            self.usage_bar.setValue(pct)
            color = "#4f6df5"
            if pct >= 90:
                color = "#d92d20"
            elif pct >= 70:
                color = "#f79009"
            self.usage_bar.setStyleSheet(
                "QProgressBar#UsageBar::chunk { background-color: %s; "
                "border-radius: 7px; }" % color
            )
            self.usage_ctx.setToolTip(
                f"Осталось: {usage.get('context_remaining', '—')} токенов; "
                f"резерв на ответ: {usage.get('max_output_tokens', '—')}; "
                f"всего израсходовано за запрос: "
                f"{usage.get('tokens_consumed', '—')} токенов; "
                f"вызовов инструментов: {usage.get('tool_calls', '—')}"
            )
        else:
            self.usage_ctx.setText("Контекст: —")
            self.usage_bar.setValue(0)

    # ---------- лог инструментов ----------
    @staticmethod
    def _fmt_args(args) -> str:
        if not args:
            return ""
        try:
            return ", ".join(f"{k}={v!r}" for k, v in args.items())
        except Exception:  # noqa: BLE001
            return str(args)

    @staticmethod
    def _timestamp() -> str:
        now = datetime.now()
        return now.strftime("%d/%m/%Y %H:%M:%S") + f":{now.microsecond // 1000:03d}"

    def _elapsed_suffix(self) -> str:
        if self._request_started is None:
            return ""
        secs = time.perf_counter() - self._request_started
        val = f"{secs:.2f}".replace(".", ",")
        return f" (На ответ ушло {val} сек.)"

    def _log(self, text: str):
        stamp = self._timestamp()
        for line in text.splitlines() or [""]:
            self.log_view.appendPlainText(f"[{stamp}] {line}")
        bar = self.log_view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _on_tools(self, tools):
        suffix = self._elapsed_suffix()
        if not tools:
            self._log(
                f"· инструменты не вызывались — текстовый ответ модели{suffix}"
            )
            return
        last = len(tools) - 1
        for idx, item in enumerate(tools):
            name = item.get("name", "?")
            args = item.get("arguments", {})
            result = item.get("result", "")
            self._log(f"→ {name}({self._fmt_args(args)})")
            tail = suffix if idx == last else ""
            self._log(f"   ← {result}{tail}")

    def _toggle_log(self):
        visible = not self.log_view.isVisible()
        self.log_view.setVisible(visible)
        self.log_toggle.setText("Скрыть" if visible else "Показать")

    def _on_finished(self):
        self._hide_typing()
        self._set_busy(False)
        self._worker = None
        self._request_started = None
        self.input.setFocus()

    def _set_busy(self, busy: bool):
        self.send_btn.setEnabled(not busy)
        self.input.setEnabled(not busy)
        if busy:
            self.status.setText("Отправлен запрос к модели...")


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Agent Chat")
    window = ChatWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
