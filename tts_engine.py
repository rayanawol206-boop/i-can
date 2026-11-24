"""Speech that never runs on the UI thread.

Online speech uses edge-tts and is played with pygame. The first play of a
sentence is saved under ``audio_cache/`` so the same recording works with no
network. pyttsx3 is the last fallback. Ordinary sentences play once.
An emergency call plays three times, with a 1.2 second pause between plays.

The pyttsx3 engine stays on this worker thread. pygame is started here too.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Callable


log = logging.getLogger("ican.tts")

SpeakCallback = Callable[[bool, str], None]
REPEAT_PAUSE = 1.2
APP_DIR = Path(__file__).resolve().parent
CACHE_DIR = APP_DIR / "audio_cache"

NEURAL_VOICES = {
    "en": {"Female": "en-US-AriaNeural", "Male": "en-US-GuyNeural", "Robotic": "en-US-AriaNeural"},
    "am": {"Female": "am-ET-MekdesNeural", "Male": "am-ET-AmehaNeural", "Robotic": "am-ET-MekdesNeural"},
    # No Oromo neural voice exists. Aria reads the Latin spelling.
    "om": {"Female": "en-US-AriaNeural", "Male": "en-US-GuyNeural", "Robotic": "en-US-AriaNeural"},
    # No Tigrinya neural voice exists. Mekdes can read the Fidel.
    "ti": {"Female": "am-ET-MekdesNeural", "Male": "am-ET-AmehaNeural", "Robotic": "am-ET-MekdesNeural"},
}


class TTSEngine:
    """Speak one sentence at a time without blocking the scanner."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._pending: tuple | None = None
        self._rate = 170
        self._style = "Female"
        self._language = "en"
        self._speaking = False
        self._running = True
        self._error = ""
        self._mixer = False
        self._cancel = False
        self._engine = None
        self._cache_lock = threading.Lock()
        self._thread = threading.Thread(target=self._worker, name="ican-tts", daemon=True)
        self._thread.start()

    def speak(self, text: str, on_done: SpeakCallback | None = None, repeats: int = 1) -> bool:
        """Queue ``text`` once, unless ``repeats`` asks for the emergency call."""
        spoken = " ".join(text.split())
        if not spoken:
            return False
        with self._lock:
            if not self._running:
                return False
            self._cancel = False
            self._pending = (spoken, self._rate, self._style, self._language, on_done, max(1, int(repeats)))
            self._error = ""
        self._wake.set()
        return True

    def stop(self) -> None:
        """Cut off whatever is being said. The next requested sentence can still play."""
        with self._lock:
            self._cancel = True
            self._pending = None
            engine = self._engine
        self._stop_playback()
        if engine is not None:
            try:
                engine.stop()
            except Exception:
                return

    def set_rate(self, rate: int) -> None:
        with self._lock:
            self._rate = max(90, min(240, int(rate)))

    def set_style(self, style: str) -> None:
        with self._lock:
            if style in {"Female", "Male", "Robotic"}:
                self._style = style

    def set_language(self, language: str) -> None:
        with self._lock:
            self._language = language if language in NEURAL_VOICES else "en"

    def precache(self, lines: list[tuple[str, str]]) -> None:
        """Save local phrases in the background. Playback is unchanged if this fails."""
        snapshot = list(lines)
        threading.Thread(
            target=self._precache,
            args=(snapshot,),
            name="ican-cache",
            daemon=True,
        ).start()

    def is_speaking(self) -> bool:
        with self._lock:
            return self._speaking

    def last_error(self) -> str:
        with self._lock:
            return self._error

    def shutdown(self) -> None:
        with self._lock:
            self._running = False
            self._pending = None
        self._wake.set()
        self._stop_playback()
        self._thread.join(timeout=4.0)

    def _worker(self) -> None:
        _init_com()
        engine = None
        voices: list = []
        try:
            engine, voices = self._open_engine()
            self._speak_loop(engine, voices)
        finally:
            self._stop_playback()
            _stop_engine(engine)
            _uninit_com()

    def _open_engine(self):
        try:
            engine = self._new_engine()
            voices = list(engine.getProperty("voices") or [])
            engine.setProperty("volume", 1.0)
            return engine, voices
        except Exception as exc:
            log.exception("speech engine failed to start")
            with self._lock:
                self._error = str(exc)
            return None, []

    def _speak_loop(self, engine, voices: list) -> None:
        while True:
            self._wake.wait()
            with self._lock:
                pending = self._pending
                self._pending = None
                self._wake.clear()
                running = self._running
            if not running:
                break
            if pending is None:
                continue
            text, rate, style, language, callback, repeats = pending
            ok = False
            error = ""
            try:
                with self._lock:
                    self._speaking = True
                    self._engine = engine
                if self._cancelled():
                    ok = False
                else:
                    audio = self._ensure_audio(text, rate, style, language)
                    if audio is not None and self._play_times(audio, repeats):
                        ok = True
                    else:
                        if self._cancelled():
                            ok = False
                        else:
                            if engine is None:
                                engine, voices = self._open_engine()
                                with self._lock:
                                    self._engine = engine
                            ok = self._pyttsx_times(engine, voices, text, rate, style, repeats)
                            if not ok and not self._cancelled():
                                error = self.last_error() or "speech failed"
            except Exception as exc:
                error = str(exc)
                log.exception("speech failed")
                with self._lock:
                    self._error = error
                engine = _stop_engine(engine)
            finally:
                with self._lock:
                    self._speaking = False
            if callback is not None:
                try:
                    callback(ok, error)
                except Exception:
                    log.exception("speech callback failed")

    def _ensure_audio(self, text: str, rate: int, style: str, language: str) -> Path | None:
        voice = neural_voice(language, style)
        rate_text = edge_rate(rate, style == "Robotic")
        path = cache_path(voice, rate_text, text)
        if path.exists() and path.stat().st_size > 800:
            return path
        if self._save_edge(text, voice, rate_text, path):
            return path
        return None

    def _save_edge(self, text: str, voice: str, rate_text: str, path: Path) -> bool:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f".{threading.get_ident()}.part")
        try:
            import edge_tts

            async def _write() -> None:
                communicate = edge_tts.Communicate(text, voice, rate=rate_text)
                await asyncio.wait_for(communicate.save(str(temporary)), timeout=25)

            asyncio.run(_write())
            if not temporary.exists() or temporary.stat().st_size < 800:
                temporary.unlink(missing_ok=True)
                return False
            with self._cache_lock:
                if path.exists() and path.stat().st_size > 800:
                    temporary.unlink(missing_ok=True)
                    return True
                temporary.replace(path)
            return True
        except Exception as exc:
            log.info("online voice unavailable: %s", exc)
            try:
                temporary.unlink(missing_ok=True)
            except Exception:
                pass
            return False

    def _cancelled(self) -> bool:
        with self._lock:
            return self._cancel or not self._running

    def _play_times(self, path: Path, repeats: int) -> bool:
        try:
            import pygame
        except Exception:
            log.info("pygame is not available")
            return False
        try:
            if not self._mixer:
                pygame.mixer.init()
                self._mixer = True
            for repeat in range(max(1, repeats)):
                if self._cancelled():
                    self._stop_playback()
                    return False
                pygame.mixer.music.load(str(path))
                pygame.mixer.music.play()
                while pygame.mixer.music.get_busy():
                    if self._cancelled():
                        self._stop_playback()
                        return False
                    time.sleep(0.05)
                pygame.mixer.music.unload()
                if repeat < repeats - 1 and not self._pause():
                    return False
            return True
        except Exception:
            log.exception("could not play saved speech")
            return False

    def _pause(self) -> bool:
        end = time.monotonic() + REPEAT_PAUSE
        while time.monotonic() < end:
            if self._cancelled():
                return False
            time.sleep(0.05)
        return True

    def _pyttsx_times(self, engine, voices: list, text: str, rate: int, style: str, repeats: int) -> bool:
        if engine is None:
            with self._lock:
                self._error = "speech isn't available"
            return False
        try:
            voice_id = _choose_voice(voices, style)
            if voice_id:
                engine.setProperty("voice", voice_id)
            engine.setProperty("rate", _rate_for(style, rate))
            for repeat in range(max(1, repeats)):
                if self._cancelled():
                    return False
                engine.say(text)
                engine.runAndWait()
                if repeat < repeats - 1 and not self._pause():
                    return False
            return True
        except Exception as exc:
            log.exception("fallback speech failed")
            with self._lock:
                self._error = str(exc)
            return False

    def _precache(self, lines: list[tuple[str, str]]) -> None:
        saved = 0
        for language, text in lines:
            with self._lock:
                if not self._running:
                    return
                style = self._style
                rate = self._rate
            voice = neural_voice(language, "Female" if style == "Robotic" else style)
            rate_text = edge_rate(rate, style == "Robotic")
            path = cache_path(voice, rate_text, text)
            if path.exists() and path.stat().st_size > 800:
                continue
            if not self._save_edge(text, voice, rate_text, path):
                log.info("stopped phrase cache after %s files", saved)
                return
            saved += 1
        if saved:
            log.info("cached %s phrases for offline speech", saved)

    def _stop_playback(self) -> None:
        if not self._mixer:
            return
        try:
            import pygame

            pygame.mixer.music.stop()
        except Exception:
            return

    @staticmethod
    def _new_engine():
        import pyttsx3

        if sys.platform == "win32":
            try:
                return pyttsx3.init("sapi5")
            except Exception:
                log.info("sapi5 init failed, trying the default driver")
        return pyttsx3.init()


def neural_voice(language: str, style: str) -> str:
    table = NEURAL_VOICES.get(language, NEURAL_VOICES["en"])
    return table.get(style, table["Female"])


def edge_rate(rate: int, robotic: bool) -> str:
    paced = int(rate * 0.75) if robotic else rate
    percent = int(round((paced - 170) / 170 * 100))
    percent = max(-40, min(40, percent))
    return f"{percent:+d}%"


def cache_path(voice: str, rate_text: str, text: str) -> Path:
    digest = hashlib.sha1(f"{voice}|{rate_text}|{text}".encode("utf-8")).hexdigest()[:20]
    safe_voice = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in voice)
    return CACHE_DIR / safe_voice / f"{digest}.mp3"


def _init_com() -> None:
    if sys.platform != "win32":
        return
    try:
        import pythoncom

        pythoncom.CoInitialize()
    except Exception:
        log.info("COM was already initialized for speech")


def _uninit_com() -> None:
    if sys.platform != "win32":
        return
    try:
        import pythoncom

        pythoncom.CoUninitialize()
    except Exception:
        return


def _stop_engine(engine):
    if engine is None:
        return None
    try:
        engine.stop()
    except Exception:
        pass
    return None


def _rate_for(style: str, rate: int) -> int:
    if style == "Robotic":
        return max(105, min(145, int(rate * 0.75)))
    return rate


def _blob(voice) -> str:
    parts = [
        str(getattr(voice, "name", "") or ""),
        str(getattr(voice, "id", "") or ""),
        str(getattr(voice, "gender", "") or ""),
    ]
    for language in getattr(voice, "languages", []) or []:
        if isinstance(language, bytes):
            language = language.decode("utf-8", "ignore")
        parts.append(str(language))
    return " ".join(parts).lower()


def _is_english(voice) -> bool:
    blob = _blob(voice)
    return "en" in blob or "english" in blob


def _gender(voice) -> str:
    blob = _blob(voice)
    if any(token in blob for token in ("female", "zira", "hazel", "susan", "samantha", "victoria", "karen", "moira")):
        return "female"
    if any(token in blob for token in ("male", "david", "mark", "george", "alex", "daniel")):
        return "male"
    return ""


def _choose_voice(voices: list, style: str) -> str | None:
    if not voices:
        return None
    want = "male" if style in {"Male", "Robotic"} else "female"
    english = [voice for voice in voices if _is_english(voice)] or list(voices)
    for voice in english:
        if _gender(voice) == want:
            return voice.id
    for voice in voices:
        if _gender(voice) == want:
            return voice.id
    if want == "female" and len(voices) > 1:
        return voices[1].id
    return voices[0].id
