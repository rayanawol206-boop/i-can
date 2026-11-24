"""I Can — a row-column scanning communicator.

Thread ownership:
- This file's window, keyboard highlight, and ``after`` poll run on the Tk thread.
- ``FaceTracker`` reads the camera and MediaPipe on its own thread and only
  shares a snapshot through ``poll()``.
- ``TTSEngine`` owns the only pyttsx3 engine, on a third thread.
Closing the window stops the highlight, the camera, and speech before destroy.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import customtkinter as ctk
from PIL import Image

from ai_assistant import answer as assistant_answer
from ai_assistant import asking_status, load_guides, question_board
from keyboard_ui import ScanningKeyboard
from languages import (
    cache_lines,
    category_board,
    language_caption,
    language_name,
    load_catalog,
    next_language,
    phrase_board,
    pick_script_font,
)
from predictor import WordPredictor
from tts_engine import TTSEngine
from youtube_player import (
    OPENING,
    OPENED,
    category_board as youtube_categories,
    close_board,
    close_browser_tab,
    load_topics,
    offline_message,
    open_video,
    topic_board,
    topic_query,
)


APP_DIR = Path(__file__).resolve().parent
HISTORY_PATH = APP_DIR / "history.txt"
PHRASE_PATH = APP_DIR / "local_phrases.json"

BG = "#10141C"
CARD = "#171D29"
GOLD = "#FFC857"
TEAL = "#3DDC97"
TEXT = "#F4F7FB"
MUTED = "#9AA3B5"
BORDER = "#2C3548"
DISPLAY = "#0C1018"
PLACEHOLDER = "Your words will appear here"

log = logging.getLogger("ican")


def _alarm_sound() -> None:
    """Urgent beep off the UI thread. Windows uses the system alert plus a tone."""

    def run() -> None:
        try:
            import winsound

            winsound.MessageBeep(winsound.MB_ICONHAND)
            for freq, duration in ((988, 160), (784, 160), (988, 180), (659, 320)):
                winsound.Beep(freq, duration)
        except Exception:
            try:
                sys.stdout.write("\a")
                sys.stdout.flush()
            except Exception:
                return

    threading.Thread(target=run, name="ican-alarm", daemon=True).start()


def _font(size: int, weight: str = "normal") -> ctk.CTkFont:
    return ctk.CTkFont(family="Segoe UI", size=size, weight=weight)


class _OfflineTracker:
    """Keeps the keyboard usable when the camera stack cannot start."""

    def start(self) -> None:
        return

    def stop(self) -> None:
        return

    def set_mode(self, mode: str) -> None:
        return

    def set_sensitivity(self, value: float) -> None:
        return

    def request_calibration(self) -> None:
        return

    def set_return_watch(self, enabled: bool) -> None:
        return

    def pause(self) -> None:
        return

    def resume(self) -> None:
        return

    def poll(self):
        return _empty_view("Camera unavailable — use Space")


class _View:
    def __init__(self, status: str) -> None:
        self.frame = None
        self.status = status
        self.signal = 0.0
        self.triggered = False
        self.flash = False
        self.camera_ok = False
        self.calibrating = False
        self.face_visible = False
        self.emergency = False
        self.come_back = False


def _empty_view(status: str) -> _View:
    return _View(status)


class ICanApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        # CustomTkinter enlarges every widget by the monitor DPI (150% here)
        # but leaves the window in the same coordinates, so the keyboard was
        # pushed off the bottom. Keep widget sizes at the values below.
        from customtkinter.windows.widgets.scaling.scaling_tracker import ScalingTracker

        dpi = ScalingTracker.get_widget_scaling(self) or 1.0
        if dpi > 1.05:
            ctk.set_widget_scaling(1.0 / dpi)
        self.title("I Can")
        self.configure(fg_color=BG)
        self.minsize(800, 480)
        self._closing = False
        self._poll_id = None
        self._last_trigger = 0.0
        self._space_held = False
        self._buffer = ""
        self._history_lock = threading.Lock()
        self._preview_image = None
        self._border = ""
        self._sentence_token = 0
        self._last_emergency = 0.0
        self._calib_shown = False
        self.history_path = HISTORY_PATH

        self.predictor = WordPredictor()
        self.catalog = load_catalog(PHRASE_PATH)
        self.topics = load_topics()
        self.guides = load_guides()
        self.language = "en"
        self._panel = ""
        self._yt_category = ""
        self._yt_busy = False
        self._ai_busy = False
        self._overlay = ""
        self._floated = False
        self._saved_geometry = ""
        self._screen = "front"
        self._saved_clicks = None
        self.is_paused = False
        self._pause_reason = ""
        self._chat = None
        self._chat_busy = False
        self._chat_language = "en"
        self._ignore_map_until = 0.0
        self.script_font = pick_script_font(set(self.tk.splitlist(self.tk.call("font", "families"))))
        self.tts = TTSEngine()
        self.tts.precache(cache_lines(self.catalog))
        self.tracker = self._make_tracker()

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1, minsize=200)
        self._build_header()
        self._build_sentence()
        self.keyboard = ScanningKeyboard(self, self._on_action)
        self.keyboard.grid(row=2, column=0, sticky="nsew", padx=16, pady=8)
        self.keyboard.set_script_font(self.script_font)
        self.keyboard.set_language_caption(self._language_caption())
        self.keyboard.set_sound_enabled(False)
        self._build_settings()
        self._build_front()
        self._build_register()
        self._build_hub()
        self._sync()
        self._place_window()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Map>", self._on_map)
        self.bind_all("<KeyPress>", self._on_key)
        self.bind_all("<KeyPress-space>", self._space_down)
        self.bind_all("<KeyRelease-space>", self._space_up)
        self.after(120, lambda: self._capture_space(self))
        self.after(200, self.focus_force)
        self.tracker.start()
        self.after(250, self.keyboard.start)
        self._show_front()
        self._poll_id = self.after(60, self._poll)

    def trigger_select(self) -> None:
        """Select the highlighted row, then the highlighted key."""
        if self._closing or self.is_paused or self._screen != "work":
            return
        now = time.monotonic()
        if now - self._last_trigger < 0.08:
            return
        self._last_trigger = now
        self.keyboard.trigger_select()

    def report_callback_exception(self, exc, val, tb) -> None:
        log.error("interface error", exc_info=(exc, val, tb))
        try:
            self._set_status("The keyboard hit an error and is still running.")
        except Exception:
            return

    def _make_tracker(self):
        try:
            from face_tracker import FaceTracker

            return FaceTracker()
        except Exception:
            log.exception("camera tools failed to load")
            return _OfflineTracker()

    def _build_header(self) -> None:
        header = ctk.CTkFrame(self, fg_color="transparent")
        self.header = header
        header.grid(row=0, column=0, sticky="ew", padx=16, pady=(6, 0))

        self.camera_card = ctk.CTkFrame(
            header,
            fg_color="#0E1420",
            corner_radius=14,
            border_width=3,
            border_color=BORDER,
        )
        self.camera_card.pack(side="right", anchor="n")

        left = ctk.CTkFrame(header, fg_color="transparent")
        left.pack(side="left", anchor="n")
        accent = ctk.CTkFrame(left, width=8, height=52, fg_color=GOLD, corner_radius=4)
        accent.pack_propagate(False)
        accent.pack(side="left", padx=(0, 12), pady=2)
        titles = ctk.CTkFrame(left, fg_color="transparent")
        titles.pack(side="left", fill="y")
        ctk.CTkLabel(titles, text="I Can", font=_font(32, "bold"), text_color=TEXT, anchor="w").pack(anchor="w")
        ctk.CTkLabel(
            titles,
            text="Speak with a look, a twitch, or the space bar.",
            font=_font(15),
            text_color=MUTED,
            anchor="w",
        ).pack(anchor="w", pady=(2, 0))
        ctk.CTkLabel(
            titles,
            text="Caregiver keys: type letters, Backspace, Enter speaks, Esc clears.",
            font=_font(13),
            text_color="#6E788C",
            anchor="w",
        ).pack(anchor="w", pady=(2, 0))
        self.language_button = ctk.CTkButton(
            titles,
            text=self._language_caption(),
            width=220,
            height=32,
            font=ctk.CTkFont(family=self.script_font, size=14, weight="bold"),
            fg_color="#3A3418",
            hover_color="#4E4622",
            text_color="#FFE7A3",
            command=self._cycle_language,
        )
        self.language_button.pack(anchor="w", pady=(8, 0))
        actions = ctk.CTkFrame(titles, fg_color="transparent")
        actions.pack(anchor="w", pady=(8, 0))
        for label, color, command in (
            ("First page", "#F2C14E", self._show_front),
            ("Hear", "#7DDFC3", self._hear),
            ("Stop", "#F08A7E", self._quiet),
            ("AI ASSISTANT", "#8EB6FF", self._open_chat),
            ("YouTube", "#F2A3C7", self._open_youtube),
            ("Story", "#C9A6FF", self._open_story),
        ):
            ctk.CTkButton(
                actions,
                text=label,
                width=120,
                height=32,
                corner_radius=16,
                font=_font(13, "bold"),
                fg_color=color,
                hover_color=color,
                text_color="#241428",
                command=command,
            ).pack(side="left", padx=(0, 6))

        self.preview_label = ctk.CTkLabel(self.camera_card, text="Camera", width=140, height=92, text_color=MUTED)
        self.preview_label.grid(row=0, column=0, columnspan=2, padx=8, pady=(8, 4))
        self.camera_dot = ctk.CTkLabel(self.camera_card, text="●", width=16, font=_font(14), text_color="#E07A7A")
        self.camera_dot.grid(row=1, column=0, padx=(10, 0), pady=(0, 2), sticky="nw")
        self.camera_status = ctk.CTkLabel(
            self.camera_card,
            text="Starting camera…",
            font=_font(12),
            text_color=TEXT,
            anchor="w",
            justify="left",
            wraplength=210,
        )
        self.camera_status.grid(row=1, column=1, sticky="ew", padx=(4, 10), pady=(0, 2))
        self.signal_bar = ctk.CTkProgressBar(
            self.camera_card,
            width=220,
            height=8,
            fg_color="#222A3A",
            progress_color=TEAL,
        )
        self.signal_bar.grid(row=2, column=0, columnspan=2, padx=10, pady=(2, 4), sticky="ew")
        self.signal_bar.set(0)
        self.signal_readout = ctk.CTkLabel(
            self.camera_card,
            text="Switch  0%",
            font=_font(11),
            text_color=MUTED,
            anchor="w",
        )
        self.signal_readout.grid(row=3, column=0, columnspan=2, padx=12, pady=(0, 8), sticky="w")

    def _build_sentence(self) -> None:
        holder = ctk.CTkFrame(self, fg_color="transparent", height=60)
        self.sentence_holder = holder
        holder.grid(row=1, column=0, sticky="ew", padx=16, pady=(4, 2))
        holder.grid_propagate(False)
        holder.grid_columnconfigure(0, weight=1)
        holder.grid_rowconfigure(0, weight=1)
        self.sentence_box = ctk.CTkTextbox(
            holder,
            fg_color=DISPLAY,
            text_color=TEXT,
            border_width=2,
            border_color=BORDER,
            corner_radius=16,
            font=_font(22, "bold"),
            wrap="word",
            padx=14,
            pady=6,
            takefocus=0,
        )
        self.sentence_box.grid(row=0, column=0, sticky="nsew")

    def _build_settings(self) -> None:
        bar = ctk.CTkFrame(self, fg_color=CARD, corner_radius=16)
        self.settings = bar
        bar.grid(row=3, column=0, sticky="ew", padx=16, pady=(4, 8))
        top = ctk.CTkFrame(bar, fg_color="transparent")
        top.pack(fill="x", padx=10, pady=(6, 2))
        bottom = ctk.CTkFrame(bar, fg_color="transparent")
        bottom.pack(fill="x", padx=10, pady=(2, 6))

        ctk.CTkLabel(top, text="Scan", font=_font(13, "bold"), text_color=MUTED).pack(side="left", padx=(2, 8))
        self.speed = ctk.CTkSlider(
            top,
            from_=0.5,
            to=2.5,
            number_of_steps=20,
            width=120,
            command=self._on_speed,
            fg_color="#2A3348",
            progress_color=GOLD,
            button_color=GOLD,
            button_hover_color="#FFE1A0",
        )
        self.speed.set(1.2)
        self.speed.pack(side="left")
        self.speed_value = ctk.CTkLabel(top, text="1.2s", width=42, font=_font(13, "bold"), text_color=TEXT)
        self.speed_value.pack(side="left", padx=(6, 14))

        ctk.CTkLabel(top, text="Switch", font=_font(13, "bold"), text_color=MUTED).pack(side="left", padx=(4, 8))
        self.mode_switch = ctk.CTkSegmentedButton(
            top,
            values=["Eyebrow", "Mouth", "Blink"],
            command=self._on_mode,
            font=_font(13, "bold"),
            height=32,
            selected_color="#1F6F5B",
            selected_hover_color="#27866D",
            unselected_color="#222A3A",
            unselected_hover_color="#2E3850",
            text_color=TEXT,
        )
        self.mode_switch.set("Eyebrow")
        self.mode_switch.pack(side="left", padx=(0, 14))

        ctk.CTkLabel(top, text="Sensitivity", font=_font(13, "bold"), text_color=MUTED).pack(side="left", padx=(4, 8))
        self.sensitivity = ctk.CTkSlider(
            top,
            from_=20,
            to=100,
            number_of_steps=80,
            width=110,
            command=self._on_sensitivity,
            fg_color="#2A3348",
            progress_color=TEAL,
            button_color=TEAL,
            button_hover_color="#B9F6DE",
        )
        self.sensitivity.set(65)
        self.sensitivity.pack(side="left")
        self.sens_value = ctk.CTkLabel(top, text="65", width=32, font=_font(13, "bold"), text_color=TEXT)
        self.sens_value.pack(side="left", padx=(6, 8))
        self.sound_switch = ctk.CTkSwitch(
            top,
            text="Scan clicks",
            font=_font(13),
            text_color=TEXT,
            progress_color=GOLD,
            command=self._on_sound,
        )
        self.sound_switch.pack(side="left", padx=(4, 0))

        self.calibrate_btn = ctk.CTkButton(
            bottom,
            text="Calibrate neutral face",
            width=168,
            height=30,
            font=_font(13, "bold"),
            fg_color="#1C3F38",
            hover_color="#27594F",
            text_color="#C8F8E4",
            command=self._calibrate,
        )
        self.calibrate_btn.pack(side="left", padx=(2, 8))
        self.voice_menu = ctk.CTkOptionMenu(
            bottom,
            values=["Female", "Male", "Robotic"],
            command=self._on_voice,
            width=108,
            height=30,
            font=_font(13, "bold"),
            fg_color="#222A3A",
            button_color="#343E54",
            button_hover_color="#44506A",
            dropdown_fg_color=CARD,
            dropdown_hover_color="#2A3348",
            dropdown_text_color=TEXT,
            text_color=TEXT,
        )
        self.voice_menu.set("Female")
        self.voice_menu.pack(side="left", padx=(0, 8))
        ctk.CTkLabel(bottom, text="Rate", font=_font(13, "bold"), text_color=MUTED).pack(side="left", padx=(2, 6))
        self.rate = ctk.CTkSlider(
            bottom,
            from_=110,
            to=220,
            number_of_steps=22,
            width=110,
            command=self._on_rate,
            fg_color="#2A3348",
            progress_color=GOLD,
            button_color=GOLD,
            button_hover_color="#FFE1A0",
        )
        self.rate.set(170)
        self.rate.pack(side="left")
        self.rate_value = ctk.CTkLabel(bottom, text="170", width=36, font=_font(13, "bold"), text_color=TEXT)
        self.rate_value.pack(side="left", padx=(6, 8))
        self.save_btn = ctk.CTkButton(
            bottom,
            text="Save / Log",
            width=96,
            height=30,
            font=_font(13, "bold"),
            fg_color="#222A3A",
            hover_color="#2E3850",
            text_color=TEXT,
            command=self._save,
        )
        self.save_btn.pack(side="left", padx=(0, 8))
        self.layout_switch = ctk.CTkSwitch(
            bottom,
            text="Frequency keys",
            font=_font(13),
            text_color=TEXT,
            progress_color=TEAL,
            command=self._on_layout,
        )
        self.layout_switch.pack(side="left", padx=(2, 8))
        self.status = ctk.CTkLabel(
            bottom,
            text="Ready. Space selects the highlight.",
            font=_font(13),
            text_color=MUTED,
            anchor="w",
        )
        self.status.pack(side="left", fill="x", expand=True)

    def _build_front(self) -> None:
        page = ctk.CTkFrame(self, fg_color="#2C1454")
        self.front = page
        card = ctk.CTkFrame(page, fg_color="#FFF6E4", corner_radius=28)
        card.place(relx=0.5, rely=0.5, anchor="center")
        ctk.CTkLabel(card, text="I Can", font=_font(64, "bold"), text_color="#E25B45").pack(padx=48, pady=(36, 0))
        ctk.CTkLabel(
            card,
            text="Give hope a voice.",
            font=_font(26, "bold"),
            text_color="#1F8A70",
        ).pack(padx=48, pady=(4, 12))
        ctk.CTkLabel(
            card,
            text="You do not have to speak loudly to be heard.\nA look, a breath, or a single word is enough.",
            font=_font(18),
            text_color="#5C3D73",
            justify="center",
        ).pack(padx=42, pady=(0, 8))
        ctk.CTkLabel(
            card,
            text="This place was made for you.",
            font=_font(16, "bold"),
            text_color="#C46B2C",
        ).pack(pady=(0, 18))
        ctk.CTkButton(
            card,
            text="Begin",
            width=220,
            height=52,
            corner_radius=26,
            font=_font(20, "bold"),
            fg_color="#F2A63A",
            hover_color="#FFC56A",
            text_color="#2A1238",
            command=self._show_register,
        ).pack(pady=(0, 36))

    def _build_register(self) -> None:
        page = ctk.CTkFrame(self, fg_color="#123C46")
        self.register = page
        card = ctk.CTkFrame(page, fg_color="#FFF8EE", corner_radius=28)
        card.place(relx=0.5, rely=0.5, anchor="center")
        ctk.CTkLabel(card, text="A small hello", font=_font(32, "bold"), text_color="#E25B45").pack(padx=36, pady=(28, 4))
        ctk.CTkLabel(
            card,
            text="A caregiver can fill this in. You can skip it.",
            font=_font(15),
            text_color="#5C3D73",
        ).pack(pady=(0, 12))
        self.name_entry = self._field(card, "Your name")
        self.origin_entry = self._field(card, "Where you are from")
        self.condition_entry = self._field(card, "What you are living with")
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(pady=(8, 28))
        ctk.CTkButton(
            row,
            text="Continue",
            width=160,
            height=46,
            corner_radius=23,
            font=_font(16, "bold"),
            fg_color="#1F8A70",
            hover_color="#2AAB8C",
            text_color="#FFFFFF",
            command=self._save_profile,
        ).pack(side="left", padx=6)
        ctk.CTkButton(
            row,
            text="Skip",
            width=120,
            height=46,
            corner_radius=23,
            font=_font(16, "bold"),
            fg_color="#F2C14E",
            hover_color="#FFD56A",
            text_color="#2A1238",
            command=self._show_hub,
        ).pack(side="left", padx=6)

    def _field(self, parent, label: str):
        ctk.CTkLabel(parent, text=label, font=_font(14, "bold"), text_color="#1F8A70", anchor="w").pack(
            fill="x", padx=36, pady=(8, 2)
        )
        entry = ctk.CTkEntry(
            parent,
            width=420,
            height=42,
            corner_radius=14,
            font=_font(16),
            fg_color="#FFFFFF",
            text_color="#241428",
            border_color="#F2A63A",
        )
        entry.pack(padx=36)
        return entry

    def _build_hub(self) -> None:
        page = ctk.CTkFrame(self, fg_color="#3A1860")
        self.hub = page
        ctk.CTkLabel(page, text="Where shall we go?", font=_font(34, "bold"), text_color="#FFF6E4").pack(pady=(28, 6))
        ctk.CTkLabel(
            page,
            text="Choose one. First page returns to the welcome.",
            font=_font(16),
            text_color="#F2C14E",
        ).pack(pady=(0, 16))
        grid = ctk.CTkFrame(page, fg_color="transparent")
        grid.pack(expand=True)
        choices = (
            ("Talk", "#7DDFC3", self._open_talk),
            ("Phrases", "#F2C14E", self._open_phrases),
            ("AI Assistant", "#8EB6FF", self._open_chat),
            ("A story", "#C9A6FF", self._open_story),
            ("YouTube", "#F2A3C7", self._open_youtube),
            ("First page", "#F08A7E", self._show_front),
        )
        for index, (label, color, command) in enumerate(choices):
            button = ctk.CTkButton(
                grid,
                text=label,
                width=240,
                height=84,
                corner_radius=24,
                font=_font(22, "bold"),
                fg_color=color,
                hover_color=color,
                text_color="#241428",
                command=command,
            )
            button.grid(row=index // 2, column=index % 2, padx=12, pady=10)

    def _show_front(self) -> None:
        self._screen = "front"
        self._hide_work()
        self.register.place_forget()
        self.hub.place_forget()
        self.front.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.front.lift()

    def _show_register(self) -> None:
        self._screen = "register"
        self._hide_work()
        self.front.place_forget()
        self.hub.place_forget()
        self.register.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.register.lift()

    def _show_hub(self) -> None:
        self._screen = "hub"
        self._hide_work()
        self.front.place_forget()
        self.register.place_forget()
        self.hub.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.hub.lift()

    def _hide_work(self) -> None:
        self.header.grid_remove()
        self.sentence_holder.grid_remove()
        self.keyboard.frame.grid_remove()
        self.settings.grid_remove()

    def _show_work(self) -> None:
        self._screen = "work"
        self.front.place_forget()
        self.register.place_forget()
        self.hub.place_forget()
        self.header.grid(row=0, column=0, sticky="ew", padx=16, pady=(6, 0))
        self.sentence_holder.grid(row=1, column=0, sticky="ew", padx=16, pady=(4, 2))
        self.keyboard.frame.grid(row=2, column=0, sticky="nsew", padx=16, pady=8)
        self.settings.grid(row=3, column=0, sticky="ew", padx=16, pady=(4, 8))

    def _save_profile(self) -> None:
        profile = {
            "name": self.name_entry.get().strip(),
            "origin": self.origin_entry.get().strip(),
            "condition": self.condition_entry.get().strip(),
        }
        try:
            (APP_DIR / "profile.json").write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            log.exception("profile save failed")
        self._show_hub()

    def _open_talk(self) -> None:
        self._show_work()
        if self.language == "en":
            self.keyboard.show_english()
        else:
            self._show_categories()
        self._set_status("The keyboard is ready. Nothing speaks until you press Hear.")

    def _open_phrases(self) -> None:
        self._show_work()
        if self.language == "en":
            self.keyboard._show_phrases(True)
            self.keyboard.engine.reset()
            self._set_status("Quick phrases. Press Hear to speak the one you choose.")
        else:
            self._show_categories()

    def _open_chat(self) -> None:
        """A normal typed chat for a nurse or family member. Gestures do not drive it."""
        if self._chat is not None:
            try:
                if self._chat.winfo_exists():
                    self._chat.lift()
                    self._chat.focus_force()
                    return
            except Exception:
                self._chat = None
        self._pause_input("chat")
        win = ctk.CTkToplevel(self)
        self._chat = win
        win.title("AI Assistant")
        win.geometry("680x620")
        win.minsize(520, 460)
        win.configure(fg_color="#F6F0E6")
        win.grid_columnconfigure(0, weight=1)
        win.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(
            win,
            text="For a nurse or family member. Type with the keyboard.",
            font=_font(16, "bold"),
            text_color="#5C3D73",
            anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 4))
        self._chat_history = ctk.CTkScrollableFrame(win, fg_color="#FFFDF8", corner_radius=16)
        self._chat_history.grid(row=1, column=0, sticky="nsew", padx=16, pady=8)
        self._chat_history.grid_columnconfigure(0, weight=1)
        bar = ctk.CTkFrame(win, fg_color="transparent")
        bar.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 8))
        self._chat_lang = ctk.CTkOptionMenu(
            bar,
            values=["English", "አማርኛ", "Afaan Oromo", "ትግርኛ"],
            width=150,
            font=_font(13, "bold"),
            fg_color="#8EB6FF",
            button_color="#6E96E0",
            button_hover_color="#6E96E0",
            text_color="#241428",
            command=self._set_chat_language,
        )
        names = {"en": "English", "am": "አማርኛ", "om": "Afaan Oromo", "ti": "ትግርኛ"}
        self._chat_language = self.language if self.language in names else "en"
        self._chat_lang.set(names[self._chat_language])
        self._chat_lang.pack(side="left", padx=(0, 8))
        self._chat_entry = ctk.CTkEntry(
            bar,
            height=42,
            font=_font(16),
            fg_color="#FFFFFF",
            text_color="#241428",
            placeholder_text="Type a question, then press Enter",
        )
        self._chat_entry.pack(side="left", fill="x", expand=True)
        self._chat_entry.bind("<Return>", self._send_chat)
        ctk.CTkButton(
            bar,
            text="Send",
            width=88,
            height=42,
            font=_font(15, "bold"),
            fg_color="#1F8A70",
            hover_color="#2AAB8C",
            text_color="#FFFFFF",
            command=self._send_chat,
        ).pack(side="left", padx=(8, 0))
        self._append_chat(
            "Assistant",
            "Ask in English, Amharic, Afaan Oromo, or Tigrinya. Nothing is spoken unless you press Read aloud.",
            None,
        )
        win.protocol("WM_DELETE_WINDOW", self._close_chat)
        self.after(50, self._chat_entry.focus_set)

    def _set_chat_language(self, label: str) -> None:
        codes = {"English": "en", "አማርኛ": "am", "Afaan Oromo": "om", "ትግርኛ": "ti"}
        self._chat_language = codes.get(label, "en")

    def _close_chat(self) -> None:
        win = self._chat
        self._chat = None
        self._chat_busy = False
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass
        if self._pause_reason == "chat":
            self._resume_input()

    def _send_chat(self, _event=None) -> str:
        if self._chat is None or self._chat_busy:
            return "break"
        text = self._chat_entry.get().strip()
        if not text:
            return "break"
        self._chat_entry.delete(0, "end")
        self._append_chat("You", text, None)
        self._chat_busy = True
        language = self._chat_language

        def work() -> None:
            try:
                reply = assistant_answer(language, "typed", text, APP_DIR)
            except Exception:
                log.exception("assistant failed")
                reply = None
            try:
                self.after(0, lambda: self._chat_reply(reply, language))
            except Exception:
                return

        threading.Thread(target=work, name="ican-ai", daemon=True).start()
        return "break"

    def _chat_reply(self, reply, language: str) -> None:
        self._chat_busy = False
        if self._chat is None or not self._chat.winfo_exists():
            return
        if reply is None:
            self._append_chat("Assistant", "The assistant could not answer just now.", None)
            return
        self._append_chat("Assistant", reply.text, language)

    def _append_chat(self, who: str, text: str, read_language: str | None) -> None:
        if self._chat is None:
            return
        bubble = ctk.CTkFrame(self._chat_history, fg_color="#E7EEFF" if who == "Assistant" else "#FFF3D6", corner_radius=14)
        bubble.grid(sticky="ew", pady=6)
        bubble.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(bubble, text=who, font=_font(12, "bold"), text_color="#1F8A70", anchor="w").grid(
            row=0, column=0, sticky="w", padx=12, pady=(8, 0)
        )
        family = self.script_font if read_language in {"am", "ti"} else "Segoe UI"
        ctk.CTkLabel(
            bubble,
            text=text,
            font=ctk.CTkFont(family=family, size=15),
            text_color="#241428",
            anchor="w",
            justify="left",
            wraplength=560,
        ).grid(row=1, column=0, sticky="ew", padx=12, pady=(2, 8))
        if read_language:
            ctk.CTkButton(
                bubble,
                text="Read aloud",
                width=120,
                height=32,
                font=_font(13, "bold"),
                fg_color="#7DDFC3",
                hover_color="#9AEBD4",
                text_color="#241428",
                command=lambda spoken=text, code=read_language: self._read_aloud(spoken, code),
            ).grid(row=1, column=1, padx=12, pady=8)

    def _read_aloud(self, text: str, language: str) -> None:
        self.tts.set_language(language)
        self.tts.speak(text, repeats=1)
        self.tts.set_language(self.language)

    def _pause_input(self, reason: str) -> None:
        self.is_paused = True
        self._pause_reason = reason
        self.keyboard.pause()
        self.tracker.pause()

    def _resume_input(self) -> None:
        self.is_paused = False
        self._pause_reason = ""
        self.keyboard.resume()
        self.tracker.resume()

    def _on_map(self, event) -> None:
        if event.widget is not self or self._closing:
            return
        if time.monotonic() < self._ignore_map_until:
            return
        if self._pause_reason == "youtube" and str(self.state()) == "normal":
            self._resume_input()

    def _open_youtube(self) -> None:
        self._show_work()
        self._show_youtube()

    def _open_story(self) -> None:
        self._show_work()
        self._set_status("A story is on its way. It stays quiet until you press Hear.")
        self._ask_assistant("story")

    def _hear(self) -> None:
        text = self._overlay.strip() or self._buffer.strip()
        if not text or text == PLACEHOLDER:
            self._set_status("Nothing to read yet")
            return
        self._speak_text(text)

    def _quiet(self) -> None:
        self.tts.stop()
        self._set_status("Quiet")

    def _place_window(self) -> None:
        self.update_idletasks()
        if sys.platform == "win32":
            import ctypes

            class _Rect(ctypes.Structure):
                _fields_ = (
                    ("left", ctypes.c_long),
                    ("top", ctypes.c_long),
                    ("right", ctypes.c_long),
                    ("bottom", ctypes.c_long),
                )

            area = _Rect()
            ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(area), 0)
            x = int(area.left) + 16
            y = int(area.top) + 8
            width = min(1180, max(980, int(area.right - area.left) - 48))
            height = min(628, max(560, int(area.bottom - area.top) - 44))
            self.geometry(f"{width}x{height}+{x}+{y}")
            return
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        self.geometry(f"{screen_w - 40}x{screen_h - 80}+20+20")

    def _on_action(self, kind: str, value: str) -> None:
        if kind == "letter":
            self._append_letter(value)
        elif kind == "space":
            self._space()
        elif kind == "backspace":
            self._backspace()
        elif kind == "clear":
            self._clear()
        elif kind == "speak":
            self._speak()
        elif kind == "period":
            self._period()
        elif kind == "suggestion":
            self._apply_suggestion(value)
        elif kind == "phrase":
            self._phrase(value)
        elif kind == "category":
            self._show_category(value)
        elif kind == "back":
            self.after(10, self._go_back)
        elif kind == "language":
            self._cycle_language()
        elif kind == "assistant":
            self.after(10, self._show_assistant)
        elif kind == "youtube":
            self.after(10, self._show_youtube)
        elif kind == "ytcat":
            self.after(10, lambda cid=value: self._show_yt_topics(cid))
        elif kind == "watch":
            self._watch_topic(value)
        elif kind == "ai":
            self._ask_assistant(value)
        elif kind == "close":
            self._close_youtube()
        elif kind == "mode":
            self._set_status("Quick phrases" if value == "phrases" else "Letter keyboard")

    def _append_letter(self, character: str) -> None:
        letter = character.upper()
        if len(letter) != 1 or not letter.isalpha():
            return
        self._clear_overlay()
        self._buffer += letter
        self._sync()

    def _space(self) -> None:
        if not self._buffer or self._buffer.endswith(" "):
            return
        self._buffer += " "
        self._sync()

    def _backspace(self) -> None:
        if not self._buffer:
            return
        self._buffer = self._buffer[:-1]
        self._sync()

    def _clear(self) -> None:
        self._clear_overlay()
        self._buffer = ""
        self._sync()

    def _period(self) -> None:
        self._buffer = self._buffer.rstrip()
        if not self._buffer:
            return
        if self._buffer[-1] not in ".!?":
            self._buffer += "."
        self._buffer += " "
        self._sync()

    def _apply_suggestion(self, word: str) -> None:
        word = " ".join(word.split())
        if not word:
            return
        if not self._buffer or self._buffer.endswith(" "):
            self._buffer += word + " "
        else:
            head, sep, _partial = self._buffer.rpartition(" ")
            self._buffer = f"{head}{sep}{word} "
        self._sync()

    def _phrase(self, text: str) -> None:
        self._overlay = ""
        self._buffer = text.strip() + " "
        self._apply_sentence_font()
        self._sync()
        self._set_status("Press Hear when you want this spoken")

    def _speak(self) -> None:
        text = self._buffer.strip()
        if not text:
            self._set_status("Nothing to speak yet")
            return
        self._speak_text(text)

    def _speak_text(self, text: str) -> None:
        def done(ok: bool, err: str, spoken: str = text) -> None:
            if self._closing:
                return
            try:
                self.after(0, lambda: self._speech_done(spoken, ok, err))
            except Exception:
                return

        if not self.tts.speak(text, on_done=done):
            self._set_status("Speech isn't available")
            return
        self._set_status("Speaking…")
        self.sentence_box.configure(border_color=TEAL)
        token = self._sentence_token
        self.after(700, lambda t=token: self._restore_sentence_border(t))

    def _speech_done(self, text: str, ok: bool, err: str) -> None:
        if self._closing:
            return
        if ok:
            self._log(text, "SPOKEN")
            self._set_status("Spoken — saved to history.txt")
        else:
            self._set_status("Couldn't speak that")
            if err:
                log.error("speech error: %s", err)

    def _restore_sentence_border(self, token: int | None = None) -> None:
        if self._closing or (token is not None and token != self._sentence_token):
            return
        self.sentence_box.configure(border_color=BORDER, fg_color=DISPLAY)

    def _save(self) -> None:
        text = self._buffer.strip()
        if not text:
            self._set_status("Nothing to save yet")
            return
        self._log(text, "SAVED")
        self._set_status("Saved to history.txt")

    def _log(self, text: str, kind: str) -> None:
        cleaned = " ".join(text.split())
        if not cleaned:
            return
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"{stamp} | {kind} | {cleaned}\n"
        try:
            with self._history_lock:
                with self.history_path.open("a", encoding="utf-8") as handle:
                    handle.write(line)
        except OSError as exc:
            self._set_status("Couldn't write history.txt")
            log.error("history write failed: %s", exc)

    def _sync(self) -> None:
        self._render()
        current = self._current_token()
        if current:
            words = self.predictor.complete(current)
        else:
            words = self.predictor.next_words(self._previous_token())
        self.keyboard.set_suggestions(words)

    def _current_token(self) -> str:
        if not self._buffer or self._buffer.endswith(" "):
            return ""
        return self._buffer.split()[-1]

    def _previous_token(self) -> str | None:
        parts = self._buffer.strip().split()
        if not parts:
            return None
        if self._buffer.endswith(" "):
            return parts[-1]
        if len(parts) >= 2:
            return parts[-2]
        return None

    def _render(self) -> None:
        box = self.sentence_box
        if self._overlay:
            shown, quiet = self._overlay, False
        else:
            shown, quiet = (PLACEHOLDER, True) if not self._buffer else (self._buffer, False)
        box.configure(state="normal", text_color=MUTED if quiet else TEXT)
        box.delete("1.0", "end")
        box.insert("1.0", shown)
        box.see("end")
        box.configure(state="disabled")

    def _set_status(self, text: str) -> None:
        self.status.configure(text=text)

    def _on_speed(self, value: float) -> None:
        self.keyboard.set_row_interval(float(value))
        self.speed_value.configure(text=f"{float(value):.1f}s")

    def _on_mode(self, value: str) -> None:
        text = value.lower()
        if "blink" in text:
            mode, label = "blink", "Switch: hold a blink for about half a second"
        elif "mouth" in text:
            mode, label = "mouth", "Switch: cheek or mouth"
        else:
            mode, label = "eyebrow", "Switch: eyebrow raise"
        self.tracker.set_mode(mode)
        self._set_status(label)

    def _on_layout(self) -> None:
        enabled = bool(self.layout_switch.get())
        self.keyboard.set_frequency_layout(enabled)
        if enabled:
            self._set_status("Frequency layout — E, T, A, O, I, N, S, H, R come first")
        else:
            self._set_status("ABC layout")

    def _on_sensitivity(self, value: float) -> None:
        level = int(round(float(value)))
        self.tracker.set_sensitivity(level / 100)
        self.sens_value.configure(text=str(level))

    def _language_caption(self) -> str:
        return language_caption(self.language, self.catalog)

    def _cycle_language(self) -> None:
        self.language = next_language(self.language)
        self.tts.set_language(self.language)
        caption = self._language_caption()
        self.language_button.configure(text=caption)
        self.keyboard.set_language_caption(caption)
        self._apply_sentence_font()
        if self._overlay:
            self._style_answer()
        if self._panel:
            self.after(10, self._refresh_panel)
            return
        if self.language == "en":
            self._set_status("English")
            self.after(10, self._show_english)
            return
        note = self.catalog[self.language].get("voice_note", "")
        self._set_status(f"{language_name(self.language, self.catalog)}. {note}".strip())
        self.after(10, self._show_categories)

    def _show_english(self) -> None:
        if self._closing or self.language != "en":
            return
        self._panel = ""
        self.keyboard.show_english()
        self._sync()

    def _show_categories(self) -> None:
        if self.language == "en":
            return
        self._panel = ""
        rows, names = category_board(self.language, self.catalog, self._language_caption())
        rows.append([("youtube", "", "YOUTUBE")])
        names.append("YouTube")
        self.keyboard.show_local_board(rows, names)

    def _back_label(self) -> str:
        if self.language == "en":
            return "BACK"
        return str(self.catalog[self.language].get("back") or "BACK")

    def _show_youtube(self) -> None:
        if self._closing:
            return
        self._restore_window()
        self._panel = "yt"
        self._yt_category = ""
        rows, names = youtube_categories(self.language, self.topics, self._back_label(), self._language_caption())
        self.keyboard.show_local_board(rows, names)
        self._set_status("YouTube")

    def _show_yt_topics(self, category_id: str) -> None:
        if self._closing:
            return
        self._panel = "yt-topics"
        self._yt_category = category_id
        rows, names = topic_board(
            self.language, self.topics, category_id, self._back_label(), self._language_caption()
        )
        self.keyboard.show_local_board(rows, names)

    def _show_assistant(self) -> None:
        if self._closing:
            return
        self._restore_window()
        self._panel = "ai"
        rows, names = question_board(self.language, self.guides, self._back_label(), self._language_caption())
        self.keyboard.show_local_board(rows, names)
        self._set_status("AI assistant")

    def _refresh_panel(self) -> None:
        if self._closing:
            return
        if self._panel == "yt":
            self._show_youtube()
        elif self._panel == "yt-topics" and self._yt_category:
            self._show_yt_topics(self._yt_category)
        elif self._panel == "yt-close":
            self._show_close_board()
        elif self._panel == "ai":
            self._show_assistant()

    def _go_back(self) -> None:
        if self._closing:
            return
        if self._panel == "yt-topics":
            self._show_youtube()
            return
        if self._panel in {"yt", "ai", "yt-close"}:
            self._home()
            return
        if self.language == "en":
            self._show_english()
            return
        self._show_categories()

    def _home(self) -> None:
        self._panel = ""
        self._yt_category = ""
        self._restore_window()
        self._clear_overlay()
        self.tracker.set_return_watch(False)
        if self._saved_clicks is not None:
            self.keyboard.set_sound_enabled(self._saved_clicks)
            self._saved_clicks = None
        if self.language == "en":
            self.keyboard.show_english()
            self._sync()
            return
        self._show_categories()

    def _watch_topic(self, topic_id: str) -> None:
        if self._closing:
            return
        query = topic_query(self.topics, topic_id)
        if not query:
            return
        from youtube_player import direct_watch_url

        self._pause_input("youtube")
        self._ignore_map_until = time.monotonic() + 1.5
        self.keyboard.set_sound_enabled(False)
        self._set_status("YouTube is opening. This window will stay out of the way.")
        self.update_idletasks()
        try:
            self.iconify()
        except Exception:
            log.exception("could not minimize")

        def work() -> None:
            try:
                open_video(direct_watch_url(query))
            except Exception:
                log.exception("youtube open failed")
                try:
                    self.after(0, self._youtube_failed)
                except Exception:
                    return

        threading.Thread(target=work, name="ican-youtube", daemon=True).start()

    def _youtube_failed(self) -> None:
        if self._closing:
            return
        try:
            self.deiconify()
        except Exception:
            pass
        self._resume_input()
        self._set_status(offline_message(self.language))

    def _youtube_offline(self, language: str) -> None:
        self._yt_busy = False
        if self._closing:
            return
        text = offline_message(language)
        self._set_status(text)
        self._speak_text(text)

    def _youtube_opened(self, language: str) -> None:
        self._yt_busy = False
        if self._closing:
            return
        self._show_close_board()
        self._set_status(OPENED.get(language, OPENED["en"]))

    def _show_close_board(self) -> None:
        self._panel = "yt-close"
        rows, names = close_board(self.language)
        self._float_for_video()
        self.keyboard.show_local_board(rows, names, font_size=22)

    def _close_youtube(self) -> None:
        if self._closing:
            return
        self._set_status("Closing the video…")
        try:
            self.attributes("-topmost", False)
        except Exception:
            pass

        def work() -> None:
            try:
                closed = close_browser_tab()
            except Exception:
                log.exception("close youtube failed")
                closed = False
            self.after(0, lambda: self._youtube_closed(closed))

        threading.Thread(target=work, name="ican-youtube-close", daemon=True).start()

    def _youtube_closed(self, closed: bool) -> None:
        if self._closing:
            return
        self._home()
        if closed:
            self._set_status("YouTube closed. You are back in I Can.")
        else:
            self._set_status("Back in I Can. The browser tab could not be closed from here.")

    def _ask_assistant(self, preset_id: str) -> None:
        if self._ai_busy or self._closing:
            return
        self._ai_busy = True
        language = self.language
        typed = self._buffer.strip()
        self._set_status(asking_status(language))

        def work() -> None:
            try:
                reply = assistant_answer(language, preset_id, typed, APP_DIR)
            except Exception:
                log.exception("assistant failed")
                reply = None
            self.after(0, lambda: self._assistant_ready(reply))

        threading.Thread(target=work, name="ican-ai", daemon=True).start()

    def _assistant_ready(self, reply) -> None:
        self._ai_busy = False
        if self._closing or reply is None:
            if not self._closing:
                self._set_status("The assistant could not answer just now.")
            return
        self._overlay = reply.text
        self.sentence_holder.configure(height=132)
        self._style_answer()
        self._render()
        self._set_status(reply.status)

    def _style_answer(self) -> None:
        family = "Segoe UI" if self.language == "en" else self.script_font
        self.sentence_box.configure(font=ctk.CTkFont(family=family, size=16, weight="bold"))

    def _clear_overlay(self) -> None:
        if not self._overlay:
            return
        self._overlay = ""
        self.sentence_holder.configure(height=60)
        self._apply_sentence_font()

    def _float_for_video(self) -> None:
        if not self._floated:
            self._saved_geometry = self.geometry()
            self._floated = True
        self.header.grid_remove()
        self.sentence_holder.grid_remove()
        self.settings.grid_remove()
        self.minsize(640, 180)
        try:
            self.attributes("-topmost", True)
        except Exception:
            pass
        width = max(900, self.winfo_width())
        x = max(0, self.winfo_x())
        self.geometry(f"{width}x210+{x}+12")
        self.lift()

    def _restore_window(self) -> None:
        if not self._floated:
            return
        self._floated = False
        try:
            self.attributes("-topmost", False)
        except Exception:
            pass
        self.header.grid(row=0, column=0, sticky="ew", padx=16, pady=(6, 0))
        self.sentence_holder.grid(row=1, column=0, sticky="ew", padx=16, pady=(4, 2))
        self.settings.grid(row=3, column=0, sticky="ew", padx=16, pady=(4, 8))
        self.minsize(800, 480)
        if self._saved_geometry:
            self.geometry(self._saved_geometry)
        self.lift()

    def _show_category(self, category_id: str) -> None:
        if self.language == "en":
            return
        self.after(10, lambda cid=category_id: self._open_category(cid))

    def _open_category(self, category_id: str) -> None:
        if self._closing or self.language == "en":
            return
        rows, names = phrase_board(self.language, self.catalog, category_id, self._language_caption())
        self.keyboard.show_local_board(rows, names)

    def _apply_sentence_font(self) -> None:
        if self.language == "en":
            self.sentence_box.configure(font=_font(22, "bold"))
            return
        self.sentence_box.configure(font=ctk.CTkFont(family=self.script_font, size=22, weight="bold"))

    def _on_voice(self, value: str) -> None:
        self.tts.set_style(value)
        self._set_status(f"Voice: {value}")

    def _on_rate(self, value: float) -> None:
        rate = int(round(float(value)))
        self.tts.set_rate(rate)
        self.rate_value.configure(text=str(rate))

    def _on_sound(self) -> None:
        self.keyboard.set_sound_enabled(bool(self.sound_switch.get()))

    def _calibrate(self) -> None:
        self.tracker.request_calibration()
        self._set_status("Calibrating — hold a relaxed face for one second")

    def _on_key(self, event) -> str | None:
        if event.state & 0x4:
            return None
        if event.keysym in {"space", "Shift_L", "Shift_R", "Control_L", "Control_R"}:
            return None
        if event.keysym == "BackSpace":
            self._backspace()
            return "break"
        if event.keysym == "Return":
            if self._chat is not None:
                try:
                    if self._chat.winfo_exists():
                        return None
                except Exception:
                    pass
            self._speak()
            return "break"
        if event.keysym == "Escape":
            self._clear()
            return "break"
        if event.char and event.char.isalpha():
            self._append_letter(event.char)
            return "break"
        return None

    def _space_down(self, event=None) -> str | None:
        focused = self.focus_get()
        if focused is not None and focused.winfo_class() in {"Entry", "Text"}:
            return None
        if self._space_held:
            return "break"
        self._space_held = True
        self.trigger_select()
        return "break"

    def _space_up(self, _event=None) -> str:
        self._space_held = False
        return "break"

    def _capture_space(self, widget) -> None:
        for sequence, handler in (
            ("<KeyPress-space>", self._space_down),
            ("<KeyRelease-space>", self._space_up),
        ):
            try:
                widget.bind(sequence, handler)
            except Exception:
                try:
                    widget.bind(sequence, handler, add="+")
                except Exception:
                    pass
        for child in widget.winfo_children():
            self._capture_space(child)

    def _poll(self) -> None:
        if self._closing:
            return
        try:
            view = self.tracker.poll()
            self._show_camera(view)
            if self.is_paused:
                pass
            elif view.come_back and self._panel == "yt-close":
                self._home()
                self._set_status("Your mouth brought you back. Press Hear to speak.")
            elif view.emergency:
                self._emergency()
            elif view.triggered and self._screen == "work":
                self.trigger_select()
        except Exception:
            log.exception("camera display failed")
        if not self._closing:
            self._poll_id = self.after(50, self._poll)

    def _show_camera(self, view) -> None:
        if view.frame is not None:
            image = Image.fromarray(view.frame)
            self._preview_image = ctk.CTkImage(light_image=image, dark_image=image, size=(140, 92))
            self.preview_label.configure(image=self._preview_image, text="")
        self.camera_status.configure(text=view.status)
        self.signal_bar.set(max(0.0, min(1.0, view.signal)))
        self.signal_readout.configure(text=f"Switch  {int(view.signal * 100)}%")
        if view.flash:
            dot = TEAL
            bar = TEAL
        elif view.face_visible:
            dot = TEAL
            bar = GOLD if view.signal >= 0.85 else TEAL
        elif view.camera_ok:
            dot = "#E0B050"
            bar = "#E0B050"
        else:
            dot = "#E07A7A"
            bar = "#6E3A3A"
        self.camera_dot.configure(text_color=dot)
        self.signal_bar.configure(progress_color=bar)
        border = TEAL if view.flash else GOLD if view.calibrating else BORDER if view.face_visible else "#8A6A2F" if view.camera_ok else "#6E3A3A"
        if border != self._border:
            self._border = border
            self.camera_card.configure(border_color=border)
        if view.calibrating != self._calib_shown:
            self._calib_shown = view.calibrating
            if view.calibrating:
                self.calibrate_btn.configure(state="disabled", text="Calibrating…")
            else:
                self.calibrate_btn.configure(state="normal", text="Calibrate neutral face")

    def _emergency(self) -> None:
        now = time.monotonic()
        if now - self._last_emergency < 60.0:
            return
        self._last_emergency = now
        self._show_work()
        self._restore_window()
        phrase = "Emergency! I need immediate assistance."
        self._log(phrase, "ALERT")
        self._set_status("Emergency — calling for help")
        self._sentence_token += 1
        self._pulse_sentence(self._sentence_token, 0)
        _alarm_sound()
        self.tts.speak(phrase, on_done=self._emergency_spoken, repeats=3)

    def _emergency_spoken(self, ok: bool, err: str) -> None:
        if self._closing:
            return

        def finish() -> None:
            if self._closing:
                return
            if ok:
                self._set_status("Help called — saved to history.txt")
            else:
                self._set_status("Alarm sounded. Speech did not play.")
                if err:
                    log.error("emergency speech failed: %s", err)

        try:
            self.after(0, finish)
        except Exception:
            return

    def _pulse_sentence(self, token: int, step: int) -> None:
        if self._closing or token != self._sentence_token:
            return
        if step >= 8:
            self.sentence_box.configure(border_color=BORDER, fg_color=DISPLAY)
            return
        self.sentence_box.configure(
            border_color="#FF5C5C" if step % 2 == 0 else "#8C1D1D",
            fg_color="#3A1418" if step % 2 == 0 else DISPLAY,
        )
        self.after(180, lambda: self._pulse_sentence(token, step + 1))

    def _on_close(self) -> None:
        if self._closing:
            return
        self._closing = True
        if self._poll_id is not None:
            try:
                self.after_cancel(self._poll_id)
            except Exception:
                pass
        try:
            self.keyboard.shutdown()
        except Exception:
            log.exception("keyboard shutdown failed")
        try:
            self.tracker.stop()
        except Exception:
            log.exception("camera shutdown failed")
        try:
            self.tts.shutdown()
        except Exception:
            log.exception("speech shutdown failed")
        self.destroy()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("dark-blue")
    app = ICanApp()
    app.mainloop()


if __name__ == "__main__":
    main()
