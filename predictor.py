"""Offline word prediction for the I Can keyboard.

Suggestions come from a local frequency list (``data/words.txt``) plus a
small set of assistive next-word patterns. Nothing here touches the
network or the UI, so it is safe to call on every keystroke.
"""

from __future__ import annotations

import bisect
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
WORD_LIST = APP_DIR / "data" / "words.txt"

# Shown when a sentence is empty or the previous word has no pattern.
STARTERS = ("i", "the", "please", "thank", "help")

# Communication words that should stay reachable even on a general list.
ESSENTIALS = (
    "i", "a", "the", "please", "thank", "thanks", "help", "want", "need",
    "water", "yes", "no", "hungry", "thirsty", "tired", "bathroom", "pain",
    "stop", "food", "doctor", "nurse", "medicine", "family", "come", "sorry",
    "love", "hello", "goodbye", "hurt", "hurts", "sick", "cold", "hot",
    "better", "wait", "more", "now", "bed", "chair", "home", "mom", "dad",
    "call", "bring", "give", "feel", "okay", "hi", "bye", "toilet", "blanket",
    "wheelchair", "headache", "pillow", "up", "down", "open", "close",
)

# Modest lifts so care words surface once the prefix is real, without
# burying ordinary English on the first letter.
AAC_BOOST = {
    "please": 3.0,
    "help": 3.2,
    "thank": 2.6,
    "thanks": 2.2,
    "water": 2.8,
    "bathroom": 3.0,
    "pain": 2.8,
    "hungry": 2.6,
    "thirsty": 3.0,
    "tired": 2.2,
    "medicine": 2.6,
    "hurt": 2.4,
    "hurts": 2.4,
    "sick": 2.2,
    "nurse": 2.2,
    "doctor": 1.8,
    "wheelchair": 3.0,
    "toilet": 2.4,
    "blanket": 2.2,
    "family": 1.6,
    "sorry": 1.6,
    "hello": 1.6,
    "goodbye": 1.8,
    "stop": 1.6,
    "yes": 1.4,
    "no": 1.3,
    "need": 1.4,
    "want": 1.3,
    "food": 1.5,
    "call": 1.3,
}

# Next-word patterns aimed at face-to-face communication.
BIGRAMS = {
    "i": ("want", "need", "am", "can", "feel"),
    "want": ("to", "water", "help", "food", "some"),
    "need": ("help", "water", "to", "a", "the"),
    "am": ("hungry", "thirsty", "tired", "okay", "in"),
    "im": ("hungry", "thirsty", "tired", "okay", "in"),
    "feel": ("sick", "better", "cold", "tired", "good"),
    "can": ("you", "i", "we", "someone", "help"),
    "could": ("you", "i", "we", "someone", "please"),
    "would": ("you", "like", "love", "want", "please"),
    "please": ("help", "bring", "give", "come", "call"),
    "thank": ("you", "i", "the", "please", "help"),
    "thanks": ("you", "i", "please", "so", "help"),
    "the": ("bathroom", "door", "doctor", "water", "light"),
    "my": ("head", "stomach", "back", "medicine", "family"),
    "to": ("go", "eat", "drink", "sleep", "talk"),
    "a": ("drink", "little", "doctor", "break", "hug"),
    "you": ("please", "help", "are", "can", "know"),
    "are": ("you", "we", "they", "okay", "ready"),
    "is": ("it", "this", "that", "the", "my"),
    "it": ("hurts", "is", "helps", "was", "feels"),
    "do": ("you", "not", "i", "we", "that"),
    "dont": ("want", "like", "know", "need", "understand"),
    "help": ("me", "please", "you", "now", "i"),
    "me": ("please", "up", "down", "water", "now"),
    "give": ("me", "water", "help", "some", "the"),
    "bring": ("me", "water", "the", "help", "my"),
    "call": ("the", "my", "doctor", "family", "someone"),
    "go": ("to", "home", "outside", "now", "back"),
    "come": ("here", "help", "please", "in", "back"),
    "let": ("me", "us", "go", "him", "her"),
    "turn": ("on", "off", "the", "left", "right"),
    "good": ("morning", "night", "afternoon", "thank", "bye"),
    "how": ("are", "do", "much", "about", "can"),
    "what": ("is", "do", "time", "about", "can"),
    "where": ("is", "are", "do", "can", "should"),
    "when": ("is", "can", "will", "do", "are"),
    "who": ("is", "are", "can", "will", "do"),
    "yes": ("please", "i", "thank", "that", "now"),
    "no": ("thank", "please", "i", "more", "dont"),
    "water": ("please", "now", "thank", "bottle", "i"),
    "food": ("please", "now", "thank", "i", "more"),
    "in": ("pain", "bed", "the", "here", "my"),
    "on": ("the", "my", "please", "now", "it"),
    "this": ("hurts", "is", "please", "one", "helps"),
    "that": ("hurts", "is", "please", "one", "helps"),
    "hurts": ("please", "now", "bad", "a", "lot"),
    "pain": ("please", "in", "here", "now", "medicine"),
    "hungry": ("please", "now", "food", "i", "very"),
    "thirsty": ("please", "water", "now", "i", "very"),
    "tired": ("please", "now", "bed", "i", "very"),
    "sick": ("please", "now", "help", "i", "very"),
    "love": ("you", "this", "my", "to", "i"),
    "like": ("to", "this", "that", "some", "water"),
    "have": ("pain", "a", "to", "some", "water"),
    "will": ("you", "be", "help", "go", "come"),
    "not": ("hungry", "thirsty", "now", "good", "okay"),
    "okay": ("thank", "please", "now", "i", "yes"),
    "ok": ("thank", "please", "now", "i", "yes"),
    "hello": ("how", "i", "please", "can", "my"),
    "hi": ("how", "i", "please", "can", "my"),
    "sorry": ("i", "please", "can", "about", "my"),
    "more": ("please", "water", "food", "time", "help"),
    "stop": ("please", "now", "that", "it", "i"),
    "wait": ("please", "a", "moment", "here", "i"),
    "open": ("the", "door", "window", "please", "it"),
    "close": ("the", "door", "window", "please", "it"),
    "sit": ("up", "down", "please", "here", "with"),
    "stand": ("up", "please", "here", "with", "me"),
    "get": ("up", "me", "water", "help", "the"),
    "put": ("on", "it", "the", "me", "back"),
    "take": ("me", "this", "off", "the", "some"),
    "tell": ("me", "them", "the", "my", "someone"),
    "again": ("please", "now", "later", "i", "can"),
    "now": ("please", "i", "thank", "you", "the"),
    "someone": ("help", "please", "call", "come", "here"),
    "family": ("please", "now", "call", "is", "here"),
    "doctor": ("please", "now", "come", "is", "the"),
    "nurse": ("please", "now", "help", "come", "is"),
    "bathroom": ("please", "now", "help", "i", "the"),
    "bed": ("please", "now", "is", "i", "the"),
    "medicine": ("please", "now", "time", "i", "my"),
    "head": ("hurts", "please", "is", "i", "my"),
    "back": ("hurts", "please", "to", "i", "my"),
    "stomach": ("hurts", "please", "is", "i", "my"),
}


def _normalize(word: str) -> str:
    return "".join(ch for ch in word.lower() if ch.isalpha())


class WordPredictor:
    """Frequency completions and a few high-value next-word patterns."""

    def __init__(self, wordlist_path: Path | None = None) -> None:
        self.rank: dict[str, int] = {}
        self.pretty: dict[str, str] = {}
        self.alpha: list[str] = []
        self.top: list[str] = []
        self._load(wordlist_path or WORD_LIST)

    def complete(self, prefix: str, n: int = 5) -> list[str]:
        """Most likely endings for the word currently being typed."""
        key = _normalize(prefix)
        if not key:
            return self.next_words(None, n)
        start = bisect.bisect_left(self.alpha, key)
        end = bisect.bisect_right(self.alpha, key + "\uffff")
        exact = key if key in self.rank else None
        pool = [
            word
            for word in self.alpha[start:end]
            if not (len(word) == 1 and word not in {"a", "i", key})
        ]
        pool.sort(key=self._score, reverse=True)
        if exact is not None:
            others = [word for word in pool if word != exact]
            best = self._score(others[0]) if others else 0.0
            # Keep a finished word in front when it is nearly as likely as the
            # longer guesses. A rare fragment such as "th" stays behind "the".
            if not others or self._score(exact) >= best * 0.2:
                pool = [exact, *others]
        if not pool:
            pool = [key]
        return [self._label(word) for word in pool[:n]]

    def next_words(self, previous: str | None, n: int = 5) -> list[str]:
        """Suggestions after a space, or when the board is empty."""
        prev = _normalize(previous or "")
        picked: list[str] = []
        for word in BIGRAMS.get(prev, ()):
            self._push(picked, word, prev, n)
        if not picked:
            for word in STARTERS:
                self._push(picked, word, prev, n)
        if len(picked) < n:
            for word in (*STARTERS, *self.top):
                self._push(picked, word, prev, n)
                if len(picked) >= n:
                    break
        return [self._label(word) for word in picked[:n]]

    def _push(self, picked: list[str], word: str, previous: str, limit: int) -> None:
        key = _normalize(word)
        if not key or key == previous or key in picked or len(picked) >= limit:
            return
        picked.append(key)

    def _score(self, word: str) -> float:
        rank = self.rank.get(word, 100_000)
        return AAC_BOOST.get(word, 1.0) / rank

    def _label(self, key: str) -> str:
        return self.pretty.get(key, key).upper()

    def _add(self, raw: str, rank: int) -> None:
        cleaned = raw.strip().lower()
        if not cleaned or any(not (ch.isalpha() or ch == "'") for ch in cleaned):
            return
        key = _normalize(cleaned)
        if not key:
            return
        current = self.rank.get(key)
        if current is None or rank < current:
            self.rank[key] = rank
        if "'" in cleaned:
            self.pretty[key] = cleaned

    def _load(self, path: Path) -> None:
        if path.exists():
            for index, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
                word = line.strip()
                if word and not word.startswith("#"):
                    self._add(word, index + 1)
        if len(self.rank) < 50:
            for index, word in enumerate(ESSENTIALS):
                self._add(word, index + 1)
        for word in ESSENTIALS:
            if _normalize(word) not in self.rank:
                self._add(word, 6500)
        self.pretty.setdefault("i", "I")
        self.alpha = sorted(self.rank)
        self.top = sorted(self.rank, key=self.rank.get)[:40]


def _self_check() -> None:
    predictor = WordPredictor()
    starters = predictor.next_words(None)
    assert starters == ["I", "THE", "PLEASE", "THANK", "HELP"], starters
    assert predictor.next_words("I")[0] == "WANT"
    assert "PLEASE" in predictor.complete("ple")
    assert predictor.complete("th")[0] == "THE"
    print("predictor ok")


if __name__ == "__main__":
    _self_check()
