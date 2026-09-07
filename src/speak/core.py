"""Everything `speak` does apart from drawing: backends, emphasis, matching.

Imports no UI toolkit, so tests/test_core.py runs headless.
"""

import json
import os
import queue
import re
import subprocess
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.expanduser("~/.config/speak/config.json")
TRANSCRIPT = os.path.expanduser("~/.local/share/speak/transcript")
HELPER = os.path.expanduser("~/.cache/speak/av_speak")
DEFAULTS = {"voice": None, "rate": None, "backend": "say", "saved": []}

# Emphasis. macOS ignores `say`'s [[emph +]] and SSML <emphasis> alike — both
# give byte-identical audio — so it is faked from the levers that do move the
# waveform. All four numbers below are calibration knobs, measured not guessed:
#
#   av : <prosody> pitch and rate, but ONLY in percent/keyword form.
#        pitch="1.3" and rate="0.75" are silently ignored; "+30%" and "75%" work.
#   say: [[rate N]] absolute wpm, its only working lever, and a weak one — a
#        word at 50% still only stretches ~10%. [[pbas]] and [[volm]] do nothing.
AV_EMPH_PITCH = 30          # percent above base

AV_EMPH_RATE = 75           # percent of base
SAY_EMPH_RATE = 0.5         # fraction of base wpm; pushed harder, being alone
SAY_BASE_WPM = 175          # `say`'s own default, so pinning it changes nothing:
                            # `say -r 175` is byte-identical to no -r at all,
                            # on every voice tried including a personal one
KEEP = 500                  # transcript lines carried across restarts

def load():
    # "saved" is rebuilt rather than copied from DEFAULTS: a shallow copy
    # aliases that one list, so appending a phrase mutated the default.
    cfg = {**DEFAULTS, "saved": []}

    try:
        with open(CONFIG) as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass

    return cfg

def save(cfg):
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    with open(CONFIG, "w") as f:
        json.dump(cfg, f, indent=2)

def load_transcript():
    try:
        with open(TRANSCRIPT) as f:
            return [l.rstrip("\n") for l in f][-KEEP:]
    except OSError:
        return []

def append_transcript(line):
    """Appended per line so a crash never loses what was said."""
    os.makedirs(os.path.dirname(TRANSCRIPT), exist_ok=True)
    with open(TRANSCRIPT, "a") as f:
        f.write(line + "\n")

def trim_transcript(lines):
    os.makedirs(os.path.dirname(TRANSCRIPT), exist_ok=True)
    with open(TRANSCRIPT, "w") as f:
        f.write("".join(l + "\n" for l in lines[-KEEP:]))

def helper():
    """Compile the Swift AVSpeechSynthesizer helper on first use; cached after."""
    src = os.path.join(HERE, "av_speak.swift")

    if not (os.path.exists(HELPER) and os.path.getmtime(HELPER) > os.path.getmtime(src)):
        os.makedirs(os.path.dirname(HELPER), exist_ok=True)
        subprocess.run(["swiftc", "-O", "-o", HELPER, src],
                       check=True, capture_output=True)

    return HELPER

class Speaker:
    """Serialises spoken lines through one worker thread.

    `run` is injected so the self-test records argv instead of talking.
    """

    def __init__(self, cfg, run=None):
        self.cfg = cfg
        self.run = run or subprocess.Popen
        self.q = queue.Queue()
        self.proc = None
        self.error = None
        self.lock = threading.Lock()

        threading.Thread(target=self._drain, daemon=True).start()

    def argv(self, text):
        body = render(text, self.cfg)

        if self.cfg["backend"] == "av":
            # "" = default voice. Speed rides inside the SSML, not as an arg:
            # see render().
            return [helper(), self.cfg["voice"] or "", body]

        cmd = ["say"]
        if self.cfg["voice"]:
            cmd += ["-v", self.cfg["voice"]]
        if self.cfg["rate"]:
            cmd += ["-r", str(self.cfg["rate"])]

        # `--` so a line starting with '-' is text, not a flag
        return cmd + ["--", body]

    def _drain(self):
        while True:
            text = self.q.get()

            try:
                proc = self.run(self.argv(text), stderr=subprocess.PIPE)
                with self.lock:
                    self.proc = proc

                _, err = proc.communicate()
                if proc.returncode and err:
                    self.error = err.decode(errors="replace").strip().splitlines()[0]
            except Exception as e:
                self.error = str(e)
            finally:
                with self.lock:
                    self.proc = None
                self.q.task_done()

    def say(self, text):
        self.q.put(text)

    def pending(self):
        with self.lock:
            return self.q.qsize() + (1 if self.proc else 0)

    def stop(self):
        """Drop the backlog and cut off whatever is talking now."""
        while True:
            try:
                self.q.get_nowait()
                self.q.task_done()
            except queue.Empty:
                break

        with self.lock:
            if self.proc:
                self.proc.kill()

def parse_say_voices(out):
    """[(label, name)] from `say -v ?`.

    "Eddy (German (Germany)) de_DE  # sample" — only ONE space separates that
    name from its locale, so splitting on runs of spaces glues the locale onto
    the name. `say` then silently falls back to a default voice instead of
    erroring, which looks exactly like "picking a voice does nothing".
    """
    voices = []

    for line in out.splitlines():
        head = line.split("#")[0].rsplit(None, 1)     # drop the trailing locale
        if len(head) == 2:
            name = head[0].strip()
            voices.append((f"{name}  {head[1]}", name))

    return voices

def parse_av_voices(out):
    """[(label, identifier)] from `av_speak --list`.

    Keyed by identifier, not name: fourteen different voices are called "Eddy".
    """
    voices = []

    for line in out.splitlines():
        row = line.split("\t")
        if len(row) >= 3:
            star = "  ★ personal" if len(row) > 3 else ""
            voices.append((f"{row[0]}  {row[2]}{star}", row[1]))

    return voices

def voice_names(cfg):
    """[(label, value)]: the label is shown, the value goes to the backend.

    The first row clears the setting. It has to exist, because `say` with no
    -v uses the System Voice from Settings > Accessibility > Spoken Content,
    and if that is a Siri voice then NEITHER `say -v ?` nor
    AVSpeechSynthesisVoice.speechVoices() lists it — measured: its audio
    matches none of the 184 names. Without this row, picking any voice is a
    one-way door away from the default.
    """
    if cfg["backend"] == "av":
        out = subprocess.run([helper(), "--list"], capture_output=True, text=True)
        found = parse_av_voices(out.stdout)
    else:
        out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True)
        found = parse_say_voices(out.stdout)

    return [("(system default)", None)] + found

def voice_label(v):
    """Short form for the status bar: av values are dotted identifiers."""
    return v.rsplit(".", 1)[-1] if v and v.startswith("com.apple.") else v

# _word_ or *some words*. Bounded by non-word chars so snake_case names and
# a lone 2*3 stay literal, and no markers inside the run.
EMPH = re.compile(r"(?<![\w*_])([*_])(\S[^*_]*?|\S)\1(?![\w*_])")

def spans(text):
    """Split into [(chunk, emphasised)] runs."""
    out, i = [], 0

    for m in EMPH.finditer(text):
        if m.start() > i:
            out.append((text[i:m.start()], False))
        out.append((m.group(2), True))
        i = m.end()

    if i < len(text):
        out.append((text[i:], False))

    return out or [(text, False)]

def emphasised(text):
    """Does the line mark any word for emphasis?"""
    return any(em for _, em in spans(text))


def plain(text):
    """The line without its markers — what gets read aloud, and displayed."""
    return "".join(c for c, _ in spans(text))

def render(text, cfg):
    """Text as the chosen backend wants it. Nothing to express -> untouched."""
    parts = spans(text)
    marked = any(em for _, em in parts)

    # `say` has no working way to stress one word (see the notes above the
    # constants), so it just loses the markers. The UI says so.
    if cfg["backend"] != "av":
        return plain(text)

    if not marked and not cfg["rate"]:
        return plain(text)

    # Speed is expressed HERE rather than through AVSpeechUtterance.rate, for
    # two measured reasons: that property is ignored outright on an SSML
    # utterance (identical bytes with and without it), and it is non-linear
    # anyway — mapping 225wpm onto it played 1.82x faster than the default,
    # where `say -r 225` is 1.28x. An SSML percentage IS linear in wpm, so
    # 129% matches `say -r 225` to within a percent.
    base = round(100 * (cfg["rate"] or SAY_BASE_WPM) / SAY_BASE_WPM)
    emph = round(base * AV_EMPH_RATE / 100)

    # Siblings, never nested: composition of nested rates is not something to
    # rely on, and a flat list needs no assumption about it.
    body = "".join(
        f'<prosody pitch="+{AV_EMPH_PITCH}%" rate="{emph}%">{xml(c)}</prosody>' if em
        else f'<prosody rate="{base}%">{xml(c)}</prosody>'
        for c, em in parts)

    return f"<speak>{body}</speak>"

def xml(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

BANG = re.compile(r"!(\d*)")

def bang(line, n):
    """'!3' -> index of line 3, numbered from the top; '!' alone -> the last line.

    None if the line is not a recall at all, -1 if it is but out of range.
    A line keeps its number for the whole session; only the trim at exit,
    once past KEEP lines, renumbers what survives.
    """
    m = BANG.fullmatch(line)
    if not m:
        return None

    if not m.group(1):
        return n - 1 if n else -1                   # bare ! repeats the last line

    at = int(m.group(1))

    return at - 1 if 1 <= at <= n else -1

def command(line, cfg):
    """Handle a /line typed at the prompt.

    Returns (action, message): action is "msg", "help" or "clear", so the UI
    decides what to do with it rather than reading a sentinel. Quitting is a
    chord only (^Q / ^D) — /quit was the one command that duplicated one.
    """
    word, _, arg = line[1:].partition(" ")
    arg = arg.strip()

    if word in ("help", "h", "?", ""):
        return "help", None

    if word == "clear":
        return "clear", "cleared"

    if word in ("voice", "rate", "backend"):
        # A bare /command resets to the default. Nothing is lost by dropping
        # the old read-back: the status bar shows all three at all times.
        if not arg:
            cfg[word] = DEFAULTS[word]
            save(cfg)

            if word == "rate":
                return "msg", f"rate = default ({SAY_BASE_WPM} wpm)"
            if word == "voice":
                return "msg", "voice = system default"

            return "msg", f"backend = {cfg[word]} (default)"

        if word == "rate" and not arg.isdigit():
            return "msg", "rate takes words per minute, e.g. /rate 220"
        if word == "backend" and arg not in ("say", "av"):
            return "msg", "backend is 'say' or 'av' (av emphasises with pitch too)"

        cfg[word] = int(arg) if word == "rate" else arg
        save(cfg)
        return "msg", f"{word} = {cfg[word]}"

    return "msg", f"unknown /{word} — /voice /rate /backend /clear /help"


def save_target(buf, lines, sel):
    """What ^S saves: what you're typing, else the picked line, else the last said."""
    if buf.strip():
        return buf.strip()
    if sel is not None and 0 <= sel < len(lines):
        return lines[sel]

    return lines[-1] if lines else ""

# Two spellings, one rule, stated here because it is otherwise guesswork:
# a chord is immediate; a /command takes a typed value, or is destructive
# enough to be worth typing. A row with no description is a heading.
HELP_ROWS = [
    ("keys", None),
    ("type + ⏎",         "speak it — queued, so carry on typing"),
    ("↑ ↓",              "pick a past line, wrapping at both ends"),
    ("⏎",                "say the picked line again"),
    ("Esc",              "unpick"),
    ("!3",               "say line 3; ! alone repeats the last"),
    ("_word_  *word*",   "emphasise — needs /backend av"),
    ("^R",               "edit the picked line; ⏎ saves it, silently"),
    ("^X",               "delete the picked line"),
    ("⇥",                "saved phrases — type to filter, ^X deletes"),
    ("^V",               "voice — type to filter"),
    ("^S",               "save the typed, picked, or last-said line"),
    ("^C",               "stop talking and drop the queue"),
    ("^Q  ^D",           "quit"),
    ("\\",               "speak a literal leading / or !"),
    ("^G  F1",           "this list (or /help)"),

    ("commands", None),
    ("/voice <name>",    "set by name; bare /voice restores the default"),
    ("/rate <wpm>",      "e.g. /rate 200; bare /rate is 175, the default"),
    ("/backend say|av",  "av emphasises with pitch; bare /backend is say"),
    ("/clear",           "empty the transcript (destructive, so typed)"),
]

def fuzzy(labels, q):
    """[(index, matched positions)] for labels matching q as a subsequence.

    Ranked tightest-span first, then earliest match, then original order, so
    "al" puts Albert (a-l adjacent) above Alice (a..l). Empty q keeps all.
    """
    if not q:
        return [(i, ()) for i in range(len(labels))]

    q = q.lower()
    hits = []

    for i, label in enumerate(labels):
        hay, pos, at = label.lower(), [], 0

        for ch in q:
            at = hay.find(ch, at)
            if at < 0:
                break
            pos.append(at)
            at += 1
        else:
            hits.append(((pos[-1] - pos[0], pos[0], i), i, tuple(pos)))

    hits.sort()

    return [(i, pos) for _, i, pos in hits]
