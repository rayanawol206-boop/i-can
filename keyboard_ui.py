"""Row-column scanning keyboard.

The highlight timer uses Tk's ``after``, so it already runs on the UI
thread. ``trigger_select`` must be called from that same thread. The
camera thread only raises a flag; ``main.py`` delivers it here.
"""

from __future__ import annotations

import logging
import sys
import threading
from typing import Callable


log = logging.getLogger("ican.keyboard")

# Column time is the row time scaled so the defaults are 1.2s and 1.0s.
COLUMN_RATIO = 1.0 / 1.2

Action = Callable[[str, str], None]

PALETTE = {
    "letter": {"fg": "#222A3A", "hover": "#2E3850", "text": "#F4F7FB"},
    "suggestion": {"fg": "#2C2618", "hover": "#3C3422", "text": "#FFD98A"},
    "empty": {"fg": "#1A2030", "hover": "#1A2030", "text": "#1A2030"},
    "space": {"fg": "#243028", "hover": "#314838", "text": "#E7F6EA"},
    "backspace": {"fg": "#3A2428", "hover": "#4C3036", "text": "#FFD5D5"},
    "clear": {"fg": "#3A2428", "hover": "#4C3036", "text": "#FFD5D5"},
    "speak": {"fg": "#1C3F38", "hover": "#27594F", "text": "#C8F8E4"},
    "period": {"fg": "#222A3A", "hover": "#2E3850", "text": "#F4F7FB"},
    "phrases": {"fg": "#243044", "hover": "#324560", "text": "#D6E4FF"},
    "letters": {"fg": "#243044", "hover": "#324560", "text": "#D6E4FF"},
    "phrase": {"fg": "#24344A", "hover": "#314864", "text": "#F7F8FA"},
    "category": {"fg": "#243044", "hover": "#324560", "text": "#F7F8FA"},
    "back": {"fg": "#3A2428", "hover": "#4C3036", "text": "#FFD5D5"},
    "language": {"fg": "#3A3418", "hover": "#4E4622", "text": "#FFE7A3"},
    "assistant": {"fg": "#1C3F38", "hover": "#27594F", "text": "#C8F8E4"},
    "youtube": {"fg": "#3A2430", "hover": "#4C3040", "text": "#FFD0DC"},
    "ytcat": {"fg": "#3A2430", "hover": "#4C3040", "text": "#FFE4EC"},
    "watch": {"fg": "#24344A", "hover": "#314864", "text": "#F7F8FA"},
    "ai": {"fg": "#1C3F38", "hover": "#27594F", "text": "#E7FFF6"},
    "close": {"fg": "#5A1E24", "hover": "#7A2A32", "text": "#FFE8EA"},
}

# The language key is outside the English letter and phrase tables.
LANG_ROW = 100

LETTER_ROWS = (
    tuple(("letter", ch, ch) for ch in "ABCDEFGH"),
    tuple(("letter", ch, ch) for ch in "IJKLMNOP"),
    tuple(("letter", ch, ch) for ch in "QRSTUVWX"),
    (
        ("letter", "Y", "Y"),
        ("letter", "Z", "Z"),
        ("space", " ", "SPACE"),
        ("backspace", "", "BACKSPACE"),
        ("clear", "", "CLEAR"),
        ("speak", "", "SPEAK"),
        ("period", ".", "."),
        ("phrases", "", "PHRASES"),
    ),
)

PHRASE_ROWS = (
    (
        ("phrase", "Hello", "Hello"),
        ("phrase", "Thank you", "Thank you"),
        ("phrase", "Yes", "Yes"),
        ("phrase", "No", "No"),
    ),
    (
        ("phrase", "I need help", "I need help"),
        ("phrase", "Water please", "Water please"),
        ("phrase", "I'm hungry", "I'm hungry"),
        ("phrase", "Bathroom", "Bathroom"),
    ),
    (
        ("phrase", "I'm tired", "I'm tired"),
        ("phrase", "I'm in pain", "I'm in pain"),
        ("phrase", "Stop", "Stop"),
        ("phrase", "Please wait", "Please wait"),
    ),
    (
        ("phrase", "How are you?", "How are you?"),
        ("phrase", "I love you", "I love you"),
        ("phrase", "Goodbye", "Goodbye"),
        ("letters", "", "ABC"),
    ),
)

# English letter frequency, most common first, so the scanner reaches them sooner.
FREQUENCY_LETTERS = "ETAOINSHRDLCUMWFGYPBVKXJQZ"
FREQUENCY_ROWS = (
    tuple(("letter", ch, ch) for ch in FREQUENCY_LETTERS[:8]),
    tuple(("letter", ch, ch) for ch in FREQUENCY_LETTERS[8:16]),
    tuple(("letter", ch, ch) for ch in FREQUENCY_LETTERS[16:24]),
    (
        ("letter", FREQUENCY_LETTERS[24], FREQUENCY_LETTERS[24]),
        ("letter", FREQUENCY_LETTERS[25], FREQUENCY_LETTERS[25]),
        ("space", " ", "SPACE"),
        ("backspace", "", "BACKSPACE"),
        ("clear", "", "CLEAR"),
        ("speak", "", "SPEAK"),
        ("period", ".", "."),
        ("phrases", "", "PHRASES"),
    ),
)
LETTER_NAMES = (
    "word suggestions",
    "A to H",
    "I to P",
    "Q to X",
    "Y, Z, and actions",
)
FREQUENCY_NAMES = (
    "word suggestions",
    "E T A O I N S H",
    "R D L C U M W F",
    "G Y P B V K X J",
    "Q, Z, and actions",
)
PHRASE_NAMES = (
    "greetings and answers",
    "help and needs",
    "how you feel",
    "more phrases",
)


class ScanEngine:
    """Pure row/column scanner, separated so the timing rules can be tested."""

    def __init__(self) -> None:
        self.phase = "row"
        self.row = 0
        self.col = 0
        self.steps = 0

    def reset(self) -> None:
        self.phase = "row"
        self.row = 0
        self.col = 0
        self.steps = 0

    def lock(self) -> None:
        self.phase = "col"
        self.col = 0
        self.steps = 0

    def advance(self, lengths: list[int]) -> None:
        if not lengths:
            return
        if self.row >= len(lengths):
            self.reset()
        if self.phase == "row":
            self.row = (self.row + 1) % len(lengths)
            self.col = 0
            self.steps = 0
            return
        width = max(1, lengths[self.row])
        self.steps += 1
        if self.steps >= 2 * width:
            self.phase = "row"
            self.steps = 0
            return
        self.col = (self.col + 1) % width

    def clamp(self, lengths: list[int]) -> None:
        if not lengths:
            self.reset()
            return
        if self.row >= len(lengths):
            self.reset()
            return
        if self.phase == "col" and self.col >= lengths[self.row]:
            self.col = 0


class ScanningKeyboard:
    """Suggestion row, letter grid, quick phrases, and the scanning highlight."""

    def __init__(self, master, on_action: Action) -> None:
        import customtkinter as ctk

        self._ctk = ctk
        self.on_action = on_action
        self.frame = ctk.CTkFrame(master, fg_color="#171D29", corner_radius=16)
        self.engine = ScanEngine()
        self.phrase_mode = False
        self.local_mode = False
        self.local_rows: list = []
        self.local_names: list[str] = []
        self.local_frame = None
        self.local_buttons: list = []
        self.script_font = "Segoe UI"
        self.letter_rows = LETTER_ROWS
        self.letter_names = LETTER_NAMES
        self.suggestions = ["", "", "", "", ""]
        self.row_interval = 1.2
        self.sound_on = True
        self._paused = False
        self._suspended = False
        self._started = False
        self._token = 0
        self._after_id = None

        self.frame.grid_columnconfigure(0, weight=1)
        self.frame.grid_rowconfigure(2, weight=1)

        self.hint = ctk.CTkLabel(
            self.frame,
            text="",
            anchor="w",
            justify="left",
            font=ctk.CTkFont(family="Segoe UI", size=13),
            text_color="#9AA3B5",
        )
        self.hint.grid(row=0, column=0, sticky="ew", padx=14, pady=(10, 4))

        self.sugg_frame = ctk.CTkFrame(self.frame, fg_color="transparent")
        self.sugg_frame.grid(row=1, column=0, sticky="ew", padx=8, pady=(0, 2))
        self.sugg_buttons = []
        for column in range(5):
            self.sugg_frame.grid_columnconfigure(column, weight=1, uniform="suggest")
            button = self._button(
                self.sugg_frame,
                "",
                "empty",
                lambda c=column: self._on_suggestion(c),
                ctk.CTkFont(family="Segoe UI", size=18, weight="bold"),
            )
            button.grid(row=0, column=column, sticky="nsew", padx=3, pady=3)
            self.sugg_buttons.append(button)
        self._suggest_fonts = {
            18: ctk.CTkFont(family="Segoe UI", size=18, weight="bold"),
            15: ctk.CTkFont(family="Segoe UI", size=15, weight="bold"),
            13: ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
        }

        self.letter_frame = ctk.CTkFrame(self.frame, fg_color="transparent")
        self.letter_frame.grid(row=2, column=0, sticky="nsew", padx=8, pady=(2, 10))
        self.letter_buttons = self._build_grid(self.letter_frame, LETTER_ROWS, origin=1)

        self.phrase_frame = ctk.CTkFrame(self.frame, fg_color="transparent")
        self.phrase_frame.grid(row=2, column=0, sticky="nsew", padx=8, pady=(2, 10))
        self.phrase_buttons = self._build_grid(self.phrase_frame, PHRASE_ROWS, origin=0)
        self.phrase_frame.grid_remove()

        self.lang_frame = ctk.CTkFrame(self.frame, fg_color="transparent")
        self.lang_frame.grid(row=3, column=0, sticky="ew", padx=8, pady=(0, 8))
        self.lang_frame.grid_columnconfigure(0, weight=2)
        self.lang_frame.grid_columnconfigure(1, weight=1)
        tool_font = ctk.CTkFont(family="Segoe UI", size=14, weight="bold")
        self.lang_button = self._button(
            self.lang_frame,
            "LANGUAGE · English",
            "language",
            lambda: self._on_mouse(LANG_ROW, 0),
            ctk.CTkFont(family="Segoe UI", size=14, weight="bold"),
        )
        self.youtube_button = self._button(
            self.lang_frame,
            "YOUTUBE",
            "youtube",
            lambda: self._on_mouse(LANG_ROW, 1),
            tool_font,
        )
        self.lang_button.grid(row=0, column=0, sticky="ew", padx=3, pady=2)
        self.youtube_button.grid(row=0, column=1, sticky="ew", padx=3, pady=2)

        self._apply_highlight()

    def start(self) -> None:
        """Begin the highlight once the window is on screen, so the first row gets a full dwell."""
        if self._paused or self._started:
            return
        self._started = True
        self.engine.reset()
        self._apply_highlight()
        self._schedule()

    def grid(self, **kwargs) -> None:
        self.frame.grid(**kwargs)

    def set_frequency_layout(self, enabled: bool) -> None:
        """Switch letter keys between ABC order and a frequency order."""
        self.letter_rows = FREQUENCY_ROWS if enabled else LETTER_ROWS
        self.letter_names = FREQUENCY_NAMES if enabled else LETTER_NAMES
        for buttons, row in zip(self.letter_buttons, self.letter_rows):
            for button, (_kind, _value, label) in zip(buttons, row):
                button.configure(text=label)
        self.engine.reset()
        self._apply_highlight()
        if self._started and not self._paused:
            self._reschedule()

    def set_script_font(self, family: str) -> None:
        self.script_font = family or "Segoe UI"
        self.lang_button.configure(font=self._ctk.CTkFont(family=self.script_font, size=15, weight="bold"))

    def set_language_caption(self, caption: str) -> None:
        self.lang_button.configure(text=caption)

    def show_local_board(self, rows: list, names: list[str], font_size: int = 16) -> None:
        """Replace the letter grid with category or phrase buttons. English tables stay built."""
        import customtkinter as ctk

        self.local_mode = True
        self.phrase_mode = False
        self.local_rows = rows
        self.local_names = names
        self.sugg_frame.grid_remove()
        self.letter_frame.grid_remove()
        self.phrase_frame.grid_remove()
        self.lang_frame.grid_remove()
        if self.local_frame is not None:
            self.local_frame.destroy()
        self.local_frame = ctk.CTkFrame(self.frame, fg_color="transparent")
        self.local_frame.grid(row=2, column=0, sticky="nsew", padx=8, pady=(2, 10))
        self.local_buttons = self._build_grid(
            self.local_frame, rows, origin=0, font_family=self.script_font, font_size=font_size
        )
        self.hint.configure(font=ctk.CTkFont(family=self.script_font, size=13))
        self.engine.reset()
        self._apply_highlight()
        if self._started and not self._paused:
            self._reschedule()

    def show_english(self) -> None:
        """Return to the English letter grid without changing its keys."""
        self.local_mode = False
        self.phrase_mode = False
        if self.local_frame is not None:
            self.local_frame.grid_remove()
        self.phrase_frame.grid_remove()
        self.sugg_frame.grid()
        self.letter_frame.grid()
        self.lang_frame.grid()
        self.hint.configure(font=self._ctk.CTkFont(family="Segoe UI", size=13))
        self.engine.reset()
        self._apply_highlight()
        if self._started and not self._paused:
            self._reschedule()

    def set_suggestions(self, words: list[str]) -> None:
        padded = [word.strip() for word in words[:5]]
        while len(padded) < 5:
            padded.append("")
        self.suggestions = padded
        for button, word in zip(self.sugg_buttons, padded):
            kind = "suggestion" if word else "empty"
            size = 18 if len(word) <= 9 else 15 if len(word) <= 12 else 13
            self._paint(button, kind, "normal")
            button.configure(text=word, font=self._suggest_fonts[size])
        self.engine.clamp([len(row) for row in self._scan_rows()])
        self._apply_highlight()

    def set_row_interval(self, seconds: float) -> None:
        self.row_interval = min(2.5, max(0.5, float(seconds)))
        self._update_hint()

    def set_sound_enabled(self, enabled: bool) -> None:
        self.sound_on = bool(enabled)

    def trigger_select(self) -> None:
        """Select the highlighted row, or the highlighted key on the second call."""
        if self._paused:
            return
        rows = self._scan_rows()
        if not rows:
            return
        self.engine.clamp([len(row) for row in rows])
        if self.engine.phase == "row":
            self.engine.lock()
            self._apply_highlight()
            self._reschedule()
            self._play("lock")
            return
        row = rows[min(self.engine.row, len(rows) - 1)]
        if not row:
            return
        scan_col = min(self.engine.col, len(row) - 1)
        widget_row, widget_col = row[scan_col]
        kind, value = self._cell(widget_row, widget_col)
        if not self._activate(kind, value):
            return
        self.engine.reset()
        self._apply_highlight()
        self._reschedule()
        self._play("pick")

    def pause(self) -> None:
        """Stop the highlight timer without discarding the keyboard."""
        self._suspended = True
        self._token += 1
        if self._after_id is not None:
            try:
                self.frame.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    def resume(self) -> None:
        if self._paused:
            return
        self._suspended = False
        if self._started:
            self._schedule()

    def shutdown(self) -> None:
        self._paused = True
        self._token += 1
        if self._after_id is not None:
            try:
                self.frame.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    def _build_grid(self, parent, rows, origin: int, font_family: str = "Segoe UI", font_size: int | None = None):
        import customtkinter as ctk

        columns = max(len(row) for row in rows)
        for column in range(columns):
            parent.grid_columnconfigure(column, weight=1, uniform=f"c{origin}")
        built = []
        for r, row in enumerate(rows):
            parent.grid_rowconfigure(r, weight=1, uniform=f"r{origin}", minsize=28)
            buttons = []
            for c, (kind, _value, label) in enumerate(row):
                size = font_size if font_size is not None else 22 if kind == "letter" else 16 if kind == "phrase" else 13
                button = self._button(
                    parent,
                    label,
                    kind,
                    lambda rr=r + origin, cc=c: self._on_mouse(rr, cc),
                    ctk.CTkFont(family=font_family, size=size, weight="bold"),
                )
                button.grid(row=r, column=c, sticky="nsew", padx=3, pady=3)
                buttons.append(button)
            built.append(buttons)
        return built

    def _button(self, parent, text: str, kind: str, command, font):
        colors = PALETTE.get(kind, PALETTE["letter"])
        return self._ctk.CTkButton(
            parent,
            text=text,
            font=font,
            fg_color=colors["fg"],
            hover_color=colors["hover"],
            text_color=colors["text"],
            border_width=3,
            border_color="#1B2230",
            corner_radius=12,
            height=36,
            command=command,
        )

    def _scan_rows(self) -> list[list[tuple[int, int]]]:
        if self.local_mode:
            return [
                [(r, c) for c in range(len(row))]
                for r, row in enumerate(self.local_rows)
            ]
        if self.phrase_mode:
            rows = [
                [(r, c) for c in range(len(row))]
                for r, row in enumerate(PHRASE_ROWS)
            ]
            rows.append([(LANG_ROW, 0), (LANG_ROW, 1)])
            return rows
        suggestion_row = [(0, index) for index, word in enumerate(self.suggestions) if word]
        if not suggestion_row:
            suggestion_row = [(0, 0)]
        rows = [suggestion_row]
        rows.extend(
            [(r + 1, c) for c in range(len(row))]
            for r, row in enumerate(self.letter_rows)
        )
        rows.append([(LANG_ROW, 0), (LANG_ROW, 1)])
        return rows

    def _cell(self, row: int, col: int) -> tuple[str, str]:
        if self.local_mode:
            kind, value, _label = self.local_rows[row][col]
            return kind, value
        if row == LANG_ROW:
            if col == 1:
                return "youtube", ""
            return "language", ""
        if not self.phrase_mode and row == 0:
            word = self.suggestions[col] if 0 <= col < len(self.suggestions) else ""
            if not word:
                return "empty", ""
            return "suggestion", word
        table = PHRASE_ROWS if self.phrase_mode else self.letter_rows
        index = row if self.phrase_mode else row - 1
        kind, value, _label = table[index][col]
        return kind, value

    def _widget(self, row: int, col: int):
        if self.local_mode:
            return self.local_buttons[row][col]
        if row == LANG_ROW:
            return self.youtube_button if col == 1 else self.lang_button
        if self.phrase_mode:
            return self.phrase_buttons[row][col]
        if row == 0:
            return self.sugg_buttons[col]
        return self.letter_buttons[row - 1][col]

    def _on_suggestion(self, col: int) -> None:
        if self.phrase_mode:
            return
        self._on_mouse(0, col)

    def _on_mouse(self, row: int, col: int) -> None:
        kind, value = self._cell(row, col)
        if not self._activate(kind, value):
            return
        self.engine.reset()
        self._apply_highlight()
        self._reschedule()
        self._play("pick")

    def _activate(self, kind: str, value: str) -> bool:
        if kind == "empty":
            return False
        try:
            if kind == "phrases":
                self._show_phrases(True)
                if self.on_action:
                    self.on_action("mode", "phrases")
                return True
            if kind == "letters":
                self._show_phrases(False)
                if self.on_action:
                    self.on_action("mode", "letters")
                return True
            if self.on_action:
                self.on_action(kind, value)
            return True
        except Exception:
            log.exception("key action failed")
            return True

    def _show_phrases(self, enabled: bool) -> None:
        self.phrase_mode = enabled
        if self.local_mode:
            return
        self.lang_frame.grid()
        if enabled:
            self.sugg_frame.grid_remove()
            self.letter_frame.grid_remove()
            self.phrase_frame.grid()
        else:
            self.phrase_frame.grid_remove()
            self.sugg_frame.grid()
            self.letter_frame.grid()

    def _apply_highlight(self) -> None:
        if self.local_mode and self.local_buttons:
            for buttons, row in zip(self.local_buttons, self.local_rows):
                self._paint_board(buttons, [kind for kind, _value, _label in row])
            self._highlight_current()
            self._update_hint()
            return
        self._paint(self.lang_button, "language", "normal")
        self._paint(self.youtube_button, "youtube", "normal")
        self._paint_board(self.sugg_buttons, ["suggestion" if word else "empty" for word in self.suggestions])
        for buttons, row in zip(self.letter_buttons, self.letter_rows):
            self._paint_board(buttons, [kind for kind, _value, _label in row])
        for buttons, row in zip(self.phrase_buttons, PHRASE_ROWS):
            self._paint_board(buttons, [kind for kind, _value, _label in row])
        self._highlight_current()
        self._update_hint()

    def _highlight_current(self) -> None:
        rows = self._scan_rows()
        if not rows:
            self._update_hint()
            return
        if self.engine.row >= len(rows):
            self.engine.reset()
        current = rows[self.engine.row]
        if self.engine.phase == "row":
            for widget_row, widget_col in current:
                kind, _value = self._cell(widget_row, widget_col)
                self._paint(self._widget(widget_row, widget_col), kind, "row")
        elif current:
            widget_row, widget_col = current[min(self.engine.col, len(current) - 1)]
            kind, _value = self._cell(widget_row, widget_col)
            self._paint(self._widget(widget_row, widget_col), kind, "col")

    def _paint_board(self, buttons, kinds) -> None:
        for button, kind in zip(buttons, kinds):
            self._paint(button, kind, "normal")

    def _paint(self, button, kind: str, state: str) -> None:
        colors = PALETTE.get(kind, PALETTE["letter"])
        if state == "col":
            button.configure(
                fg_color="#6A5420",
                hover_color="#7A6224",
                border_color="#FFF1C9",
                text_color="#FFF8E8",
            )
        elif state == "row":
            button.configure(
                fg_color=colors["fg"],
                hover_color=colors["hover"],
                border_color="#FFC857",
                text_color=colors["text"],
            )
        else:
            button.configure(
                fg_color=colors["fg"],
                hover_color=colors["hover"],
                border_color="#1B2230",
                text_color=colors["text"],
            )

    def _update_hint(self) -> None:
        rows = self._scan_rows()
        total = max(1, len(rows))
        index = min(self.engine.row, total - 1)
        if self.local_mode:
            names = self.local_names
        elif self.phrase_mode:
            names = list(PHRASE_NAMES) + ["language and YouTube"]
        else:
            names = list(self.letter_names) + ["language and YouTube"]
        name = names[index] if index < len(names) else ""
        if self.engine.phase == "row":
            text = (
                f"Row {index + 1} of {total} · {name} · every {self.row_interval:.1f}s"
            )
        else:
            width = len(rows[index]) if rows else 1
            column_time = self.row_interval * COLUMN_RATIO
            text = (
                f"Key {min(self.engine.col, max(width, 1) - 1) + 1} of {width}"
                f" · {name} · every {column_time:.1f}s"
            )
        self.hint.configure(text=text + " · Space or a gesture selects")

    def _schedule(self) -> None:
        if self._paused or self._suspended:
            return
        self._token += 1
        token = self._token
        self._after_id = self.frame.after(self._delay_ms(), lambda t=token: self._tick(t))

    def _reschedule(self) -> None:
        self._token += 1
        if self._after_id is not None:
            try:
                self.frame.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None
        self._schedule()

    def _delay_ms(self) -> int:
        seconds = self.row_interval
        if self.engine.phase == "col":
            seconds *= COLUMN_RATIO
        return max(50, int(seconds * 1000))

    def _tick(self, token: int) -> None:
        if self._paused or self._suspended or token != self._token:
            return
        self._after_id = None
        self.engine.advance([len(row) for row in self._scan_rows()])
        self._apply_highlight()
        self._play("row" if self.engine.phase == "row" else "col")
        self._schedule()

    def _play(self, kind: str) -> None:
        if not self.sound_on or sys.platform != "win32":
            return
        tone = {"row": (740, 22), "col": (990, 22), "lock": (880, 32), "pick": (1480, 48)}
        freq, duration = tone.get(kind, (880, 22))

        def _beep() -> None:
            try:
                import winsound

                winsound.Beep(freq, duration)
            except Exception:
                return

        threading.Thread(target=_beep, name="ican-beep", daemon=True).start()
