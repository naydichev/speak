"""Everything `speak` does apart from drawing: backends, emphasis, matching.

Imports no UI toolkit, so `python3 core.py --selftest` runs on its own.
"""

import json
import os
import queue
import re
import subprocess
import sys
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
SAY_BASE_WPM = 175          # pinned only when a `say` line has emphasis in it
KEEP = 500                  # transcript lines carried across restarts

def load():
    try:
        with open(CONFIG) as f:
            return {**DEFAULTS, **json.load(f)}
    except (OSError, ValueError):
        return dict(DEFAULTS)

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

    def __init__(self, cfg, run=subprocess.Popen):
        self.cfg = cfg
        self.run = run
        self.q = queue.Queue()
        self.proc = None
        self.error = None
        self.lock = threading.Lock()

        threading.Thread(target=self._drain, daemon=True).start()

    def argv(self, text):
        body = render(text, self.cfg)

        if self.cfg["backend"] == "av":
            # "" = default voice, 0 = default rate; helper maps wpm -> rate
            return [helper(), self.cfg["voice"] or "", str(self.cfg["rate"] or 0), body]

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
    """[(label, value)]: the label is shown, the value goes to the backend."""
    if cfg["backend"] == "av":
        out = subprocess.run([helper(), "--list"], capture_output=True, text=True)
        return parse_av_voices(out.stdout)

    out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True)
    return parse_say_voices(out.stdout)

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

def plain(text):
    """The line without its markers — what gets read aloud, and displayed."""
    return "".join(c for c, _ in spans(text))

def render(text, cfg):
    """Text as the chosen backend wants it. No emphasis -> untouched."""
    parts = spans(text)

    if not any(em for _, em in parts):
        return plain(text)

    if cfg["backend"] == "av":
        body = "".join(
            f'<prosody pitch="+{AV_EMPH_PITCH}%" rate="{AV_EMPH_RATE}%">{xml(c)}</prosody>'
            if em else xml(c)
            for c, em in parts)
        return f"<speak>{body}</speak>"

    # `say` only honours an *absolute* [[rate]] — the relative form compounds
    # and never returns to base — so the whole line gets pinned to one rate.
    base = cfg["rate"] or SAY_BASE_WPM
    body = "".join(
        f"[[rate {int(base * SAY_EMPH_RATE)}]]{c}[[rate {base}]]" if em else c
        for c, em in parts)

    return f"[[rate {base}]]{body}"

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

    Returns (action, message): action is "msg", "quit", "help" or "clear",
    so the UI decides what to do with it rather than reading a sentinel.
    """
    word, _, arg = line[1:].partition(" ")
    arg = arg.strip()

    if word in ("quit", "q", "exit"):
        return "quit", None

    if word in ("help", "h", "?", ""):
        return "help", None

    if word == "clear":
        return "clear", "cleared"

    if word in ("voice", "rate", "backend"):
        if not arg:
            return "msg", f"{word} = {cfg[word]}"
        if word == "rate" and not arg.isdigit():
            return "msg", "rate takes words per minute, e.g. /rate 220"
        if word == "backend" and arg not in ("say", "av"):
            return "msg", "backend is 'say' or 'av' (av = Personal Voice + real emphasis)"

        cfg[word] = int(arg) if word == "rate" else arg
        save(cfg)
        return "msg", f"{word} = {cfg[word]}"

    return "msg", f"unknown /{word} — /voice /rate /backend /clear /help /quit"


def save_target(buf, lines, sel):
    """What ^S saves: what you're typing, else the picked line, else the last said."""
    if buf.strip():
        return buf.strip()
    if sel is not None and 0 <= sel < len(lines):
        return lines[sel]

    return lines[-1] if lines else ""

HELP_ROWS = [
    ("type + ⏎",         "speak it — queued, so carry on typing"),
    ("↑ ↓",              "pick a past line, wrapping at both ends"),
    ("⏎",                "say the picked line again"),
    ("Esc",              "unpick"),
    ("!3",               "say line 3; ! alone repeats the last"),
    ("_word_  *word*",   "emphasise"),
    ("⇥",                "saved phrases — type to filter, ^X deletes"),
    ("^V",               "voice — type to filter"),
    ("^S",               "save the typed, picked, or last-said line"),
    ("^C",               "stop talking and drop the queue"),
    ("^Q  ^D",           "quit"),
    ("\\",               "speak a literal leading / or !"),
    ("/voice  /rate",    "show or set, e.g. /rate 200"),
    ("/backend say|av",  "av = Personal Voice, and real emphasis"),
    ("/clear",           "empty the transcript"),
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

def selftest():
    calls = []

    class FakeProc:
        returncode = 0
        def communicate(self): return b"", b""
        def kill(self): pass

    def fake_run(argv, **kw):
        calls.append(argv)          # record the argument, so a wrong argv fails
        return FakeProc()

    cfg = {**DEFAULTS, "voice": "Daniel", "rate": 220}
    sp = Speaker(cfg, run=fake_run)

    # argv construction, including a line that looks like a flag
    assert sp.argv("hi") == ["say", "-v", "Daniel", "-r", "220", "--", "hi"], sp.argv("hi")
    assert sp.argv("-n") == ["say", "-v", "Daniel", "-r", "220", "--", "-n"]
    assert Speaker(dict(DEFAULTS), run=fake_run).argv("hi") == ["say", "--", "hi"]

    # queue speaks in order
    for word in ["one", "two", "three"]:
        sp.say(word)
    sp.q.join()
    assert [c[-1] for c in calls] == ["one", "two", "three"], calls

    # stop() drops the backlog
    sp.q.put("never")
    sp.stop()
    sp.q.join()
    assert "never" not in [c[-1] for c in calls], calls

    # voice list parsing: `say -v ?` uses ONE space before the locale, so a
    # naive split on runs of spaces glues it onto the name and `say` then
    # silently substitutes a default voice
    say_out = ("Albert              en_US    # Hello! My name is Albert.\n"
               "Eddy (German (Germany)) de_DE    # Hallo! Ich heiße Eddy.\n"
               "Bad News            en_US    # Hello! My name is Bad News.\n")
    assert parse_say_voices(say_out) == [
        ("Albert  en_US", "Albert"),
        ("Eddy (German (Germany))  de_DE", "Eddy (German (Germany))"),
        ("Bad News  en_US", "Bad News"),
    ], parse_say_voices(say_out)

    # av keys on identifier, since fourteen distinct voices are named "Eddy"
    av_out = ("Eddy\tcom.apple.eloquence.en-US.Eddy\ten-US\n"
              "Eddy\tcom.apple.eloquence.de-DE.Eddy\tde-DE\n"
              "Mine\tcom.apple.speech.personal.abc\ten-US\tpersonal\n")
    assert parse_av_voices(av_out) == [
        ("Eddy  en-US", "com.apple.eloquence.en-US.Eddy"),
        ("Eddy  de-DE", "com.apple.eloquence.de-DE.Eddy"),
        ("Mine  en-US  ★ personal", "com.apple.speech.personal.abc"),
    ], parse_av_voices(av_out)

    assert voice_label("com.apple.eloquence.en-US.Eddy") == "Eddy"
    assert voice_label("Bad News") == "Bad News"        # say names pass through
    assert voice_label("Eddy (U.S.)") == "Eddy (U.S.)"  # dots alone aren't an id
    assert voice_label(None) is None

    # fuzzy filtering: tightest span first, so a-l adjacent beats a...l apart
    names = ["Daniel", "Alice", "Albert"]
    assert [names[i] for i, _ in fuzzy(names, "al")] == ["Alice", "Albert", "Daniel"]
    assert fuzzy(names, "AL") == fuzzy(names, "al")                # case-insensitive
    assert fuzzy(names, "dan") == [(0, (0, 1, 2))]                 # positions, to underline
    assert fuzzy(names, "zz") == []
    assert [i for i, _ in fuzzy(names, "")] == [0, 1, 2]           # empty keeps all
    assert [names[i] for i, _ in fuzzy(names, "ae")][0] == "Albert"  # span 3 beats 4

    # !N is the number shown on the row, counting from the top
    three = ["first", "second", "third"]
    assert bang("!1", 3) == 0
    assert bang("!3", 3) == 2
    assert bang("!", 3) == 2                            # bare ! repeats the last
    assert bang("!4", 3) == -1                          # out of range, not a crash
    assert bang("!0", 3) == -1
    assert bang("hello", 3) is None                     # not a recall at all
    assert bang("!3 please", 3) is None                 # only a bare recall counts
    assert bang("!1", 0) == -1                          # empty transcript
    assert bang("!", 0) == -1
    assert [three[bang(f"!{i}", 3)] for i in (1, 2, 3)] == three

    # emphasis parsing
    assert spans("plain line") == [("plain line", False)]
    assert spans("say _this_ loud") == [("say ", False), ("this", True), (" loud", False)]
    assert spans("*two words* first") == [("two words", True), (" first", False)]
    assert spans("snake_case_name") == [("snake_case_name", False)]      # not emphasis
    assert spans("2 * 3 = 6") == [("2 * 3 = 6", False)]                  # lone stars
    assert spans("cost_2_ ok") == [("cost_2_ ok", False)]                # opener mid-word
    assert spans("a*b* c") == [("a*b* c", False)]
    assert plain("say _this_ loud") == "say this loud"

    # no emphasis -> the argv is byte-identical to before the feature existed
    assert Speaker(dict(DEFAULTS), run=fake_run).argv("hi") == ["say", "--", "hi"]

    # say backend pins the line to one rate, because relative [[rate]] never restores
    plain_cfg = {**DEFAULTS, "rate": 200}
    assert render("say _this_ loud", plain_cfg) == \
        "[[rate 200]]say [[rate 100]]this[[rate 200]] loud", render("say _this_ loud", plain_cfg)
    assert render("no markers", plain_cfg) == "no markers"               # untouched

    # av backend uses scoped SSML, and escapes the text it wraps
    av_cfg = {**DEFAULTS, "backend": "av"}
    # percent form matters: macOS ignores a bare numeric rate/pitch outright
    assert render("say _this_ loud", av_cfg) == (
        '<speak>say <prosody pitch="+30%" rate="75%">this</prosody> loud</speak>')
    assert render("a < b & _c_", av_cfg) == (
        '<speak>a &lt; b &amp; <prosody pitch="+30%" rate="75%">c</prosody></speak>')
    assert render("a < b", av_cfg) == "a < b"        # no emphasis, no markup, no escaping

    # ^S target: typed line wins, then the picked line, then the last said
    assert save_target("typing", ["said"], 0) == "typing"
    assert save_target("  ", ["a", "b", "c"], 1) == "b"        # picked
    assert save_target("", ["a", "b", "c"], None) == "c"       # last said
    assert save_target("", [], None) == ""                     # nothing at all

    # settings commands, now returning (action, message)
    real_save, globals()["save"] = save, lambda cfg: None
    try:
        assert command("/voice Alice", cfg) == ("msg", "voice = Alice")
        assert cfg["voice"] == "Alice"
        assert command("/rate 300", cfg) == ("msg", "rate = 300")
        assert "words per minute" in command("/rate fast", cfg)[1]
        assert cfg["rate"] == 300                       # rejected, so unchanged
        assert "'say' or 'av'" in command("/backend nope", cfg)[1]
        assert cfg["backend"] == "say"
        assert command("/backend av", cfg)[0] == "msg" and cfg["backend"] == "av"
        assert command("/voice", cfg) == ("msg", "voice = Alice")   # no arg reads
        assert command("/quit", cfg) == ("quit", None)
        assert command("/help", cfg) == ("help", None)
        assert command("/", cfg) == ("help", None)
        assert command("/clear", cfg) == ("clear", "cleared")
        assert command("/nonsense", cfg)[1].startswith("unknown /nonsense")
    finally:
        globals()["save"] = real_save

    # config + transcript round-trip
    real_cfg, real_tr = CONFIG, TRANSCRIPT
    globals()["CONFIG"] = "/tmp/speak-selftest.json"
    globals()["TRANSCRIPT"] = "/tmp/speak-selftest-transcript"
    try:
        for path in (CONFIG, TRANSCRIPT):
            os.path.exists(path) and os.remove(path)

        save({**DEFAULTS, "voice": "Fred", "saved": [{"name": "a", "text": "b"}]})
        assert load()["voice"] == "Fred"
        assert load()["saved"] == [{"name": "a", "text": "b"}]

        append_transcript("first")
        append_transcript("second")
        assert load_transcript() == ["first", "second"], load_transcript()

        # count the raw file, not load_transcript() — the loader slices too, and
        # would hide an uncapped write
        trim_transcript([str(i) for i in range(KEEP + 50)])
        with open(TRANSCRIPT) as f:
            kept = f.read().splitlines()
        assert len(kept) == KEEP, len(kept)
        assert kept[0] == "50"                                  # oldest dropped

        globals()["CONFIG"] = "/tmp/does-not-exist/x.json"
        globals()["TRANSCRIPT"] = "/tmp/does-not-exist/t"
        assert load() == DEFAULTS                               # missing files
        assert load_transcript() == []                          # fall back, no crash
    finally:
        for path in ("/tmp/speak-selftest.json", "/tmp/speak-selftest-transcript"):
            os.path.exists(path) and os.remove(path)
        globals()["CONFIG"], globals()["TRANSCRIPT"] = real_cfg, real_tr

    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        sys.exit("core.py holds the logic; run speak.py for the app")
