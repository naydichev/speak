#!/usr/bin/env python3
"""Full-screen talker: type a line, press Enter, keep typing while it talks.

    ┌ speak ──────────────────────── Daniel · 200wpm · say · 2 ─┐
    │ hello there                                               │  transcript,
    │ how are you doing                                         │  newest last
    │▸i'm doing fine, thanks                                    │  ▸ = ↑↓ pick
    │ please be careful with that                               │  bold = _emph_
    ├───────────────────────────────────────────────────────────┤
    │ > what i'm typing now_                                    │  input
    └ ↑↓ pick · ⏎ speak · ⇥ saved · ^S save · ^C stop ────────┘

Lines queue through one worker thread, so typing ahead speaks in order.
Wrap a word in _underscores_ or *stars* to emphasise it.

A backend is just "text -> argv": /usr/bin/say, or the AVSpeechSynthesizer
helper next door, which additionally reaches Personal Voice and SSML.
"""

import curses
import json
import os
import queue
import re
import shutil
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


# --- state on disk ----------------------------------------------------------

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


# --- backends ---------------------------------------------------------------

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


def voice_names(cfg):
    """Voice names for the picker: what the chosen backend can actually use."""
    if cfg["backend"] == "av":
        out = subprocess.run([helper(), "--list"], capture_output=True, text=True)
        return [l.split("\t")[0] for l in out.stdout.splitlines() if l.strip()]

    out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True)
    return [l.split("  ")[0].strip() for l in out.stdout.splitlines() if l.strip()]


# --- emphasis ---------------------------------------------------------------

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


# --- line editor ------------------------------------------------------------

def edit(buf, cur, k):
    """Pure line editor: (text, cursor, key) -> (text, cursor).

    Keys arrive from get_wch(): str for characters, int for special keys.
    Pure so the self-test can exercise it without a terminal.
    """
    if isinstance(k, str):
        if k.isprintable():
            return buf[:cur] + k + buf[cur:], cur + len(k)
        k = ord(k)                                  # control chars come as str

    if k in (curses.KEY_BACKSPACE, 127, 8):
        return (buf[:cur - 1] + buf[cur:], cur - 1) if cur else (buf, cur)
    if k == curses.KEY_DC:
        return buf[:cur] + buf[cur + 1:], cur
    if k == curses.KEY_LEFT:
        return buf, max(0, cur - 1)
    if k == curses.KEY_RIGHT:
        return buf, min(len(buf), cur + 1)
    if k in (curses.KEY_HOME, 1):                   # ^A
        return buf, 0
    if k in (curses.KEY_END, 5):                    # ^E
        return buf, len(buf)
    if k == 21:                                     # ^U kill to start
        return buf[cur:], 0
    if k == 11:                                     # ^K kill to end
        return buf[:cur], cur
    if k == 23:                                     # ^W kill word back
        i = buf.rfind(" ", 0, len(buf[:cur].rstrip())) + 1
        return buf[:i] + buf[cur:], i

    return buf, cur


# --- settings commands ------------------------------------------------------

def save_target(buf, lines, sel):
    """What ^S saves: what you're typing, else the picked line, else the last said."""
    if buf.strip():
        return buf.strip()
    if sel is not None and 0 <= sel < len(lines):
        return lines[sel]

    return lines[-1] if lines else ""


def command(line, cfg, sp):
    """Handle a /line typed at the input. Returns a status string, or False to quit."""
    word, _, arg = line[1:].partition(" ")
    arg = arg.strip()

    if word in ("quit", "q", "exit"):
        return False

    if word in ("voice", "rate", "backend"):
        if not arg:
            return f"{word} = {cfg[word]}"
        if word == "rate" and not arg.isdigit():
            return "rate takes words per minute, e.g. /rate 220"
        if word == "backend" and arg not in ("say", "av"):
            return "backend is 'say' or 'av' (av = Personal Voice + real emphasis)"

        cfg[word] = int(arg) if word == "rate" else arg
        save(cfg)
        return f"{word} = {cfg[word]}"

    if word == "clear":
        return "cleared"                            # handled by caller

    if word in ("help", "h", "?", ""):
        return "↑↓ pick · ⏎ speak · _emphasis_ · ⇥ saved · ^V voice · ^S save · ^C stop · ^Q quit"

    return f"unknown /{word} — /voice /rate /backend /clear /help /quit"


# --- drawing ----------------------------------------------------------------

def put(scr, y, x, s, attr=0):
    """addnstr that silently clips instead of raising at the screen edge."""
    h, w = scr.getmaxyx()
    if 0 <= y < h and x < w:
        try:
            scr.addnstr(y, x, s, w - x - 1, attr)
        except curses.error:
            pass


def pick(scr, title, labels, delete=None):
    """Modal scrolling list. Returns the chosen index, or None if cancelled.

    `delete` is called with an index when 'd' is pressed; the row is then
    dropped from this list too.
    """
    labels = list(labels)
    sel = 0

    while True:
        if not labels:
            return None

        h, w = scr.getmaxyx()
        rows = max(1, min(len(labels), h - 6))
        width = min(max(len(title) + 4, *(len(l) + 6 for l in labels)), w - 4)
        top, left = (h - rows) // 2 - 1, (w - width) // 2

        # keep the selection inside the window
        view = min(max(0, sel - rows // 2), max(0, len(labels) - rows))

        win = curses.newwin(rows + 2, width + 2, top, left)
        win.keypad(True)            # a fresh window does NOT inherit it
        win.erase()
        win.box()
        put(win, 0, 2, f" {title} ", curses.A_BOLD)

        for r in range(rows):
            i = view + r
            mark = "▸" if i == sel else " "
            put(win, r + 1, 1, f"{mark}{i + 1:>3}. {labels[i]}".ljust(width),
                curses.A_REVERSE if i == sel else 0)

        win.refresh()

        k = win.get_wch()

        if k == curses.KEY_UP:
            sel = max(0, sel - 1)
        elif k == curses.KEY_DOWN:
            sel = min(len(labels) - 1, sel + 1)
        elif k == curses.KEY_PPAGE:
            sel = max(0, sel - rows)
        elif k == curses.KEY_NPAGE:
            sel = min(len(labels) - 1, sel + rows)
        elif k in ("\n", "\r", curses.KEY_ENTER):
            return sel
        elif k in ("d", "D") and delete:
            delete(sel)
            labels.pop(sel)
            sel = min(sel, max(0, len(labels) - 1))

            # the box shrinks, so repaint underneath or its old border lingers
            scr.touchwin()
            scr.refresh()
        elif k in ("\x1b", "\t", "q", 3, 17):        # Esc, Tab, q, ^C, ^Q
            return None


def draw(scr, cfg, sp, lines, sel, buf, cur, msg):
    h, w = scr.getmaxyx()
    scr.erase()

    # status bar
    n = sp.pending()
    right = " · ".join(filter(None, [
        cfg["voice"] or "default",
        f"{cfg['rate']}wpm" if cfg["rate"] else None,
        cfg["backend"],
        f"speaking +{n - 1}" if n > 1 else ("speaking" if n else None),
    ]))
    put(scr, 0, 0, " speak ".ljust(w - 1), curses.A_REVERSE)
    put(scr, 0, max(8, w - len(right) - 2), right, curses.A_REVERSE)

    # transcript, newest at the bottom, scrolled to keep the selection visible
    rows = max(1, h - 3)
    anchor = len(lines) - 1 if sel is None else sel
    view = min(max(0, anchor - rows + 1), max(0, len(lines) - rows))

    for r in range(min(rows, len(lines) - view)):
        i = view + r
        attr = curses.A_REVERSE if i == sel else 0

        put(scr, r + 1, 0, "▸" if i == sel else " ", attr)
        x = 1
        for chunk, em in spans(lines[i]):           # markers off, bold on
            put(scr, r + 1, x, chunk, attr | (curses.A_BOLD if em else 0))
            x += len(chunk)

    # input line
    put(scr, h - 2, 0, "> " + buf)

    hint = msg or "↑↓ pick · ⏎ speak · _emphasis_ · ⇥ saved · ^V voice · ^S save · ^C stop · ^Q quit"
    put(scr, h - 1, 0, hint.ljust(w - 1), curses.A_DIM)

    scr.move(h - 2, min(2 + cur, w - 1))
    scr.refresh()


# --- main loop --------------------------------------------------------------

def loop(scr, cfg, sp):
    curses.raw()                    # so ^C and ^Q arrive as keys, not signals
    scr.keypad(True)

    # Leave ESCDELAY at its 1s default: shrinking it below this timeout makes
    # ncurses give up on an arrow key whose bytes arrive split over a slow
    # link, and hand back a literal "OA" instead.
    scr.timeout(200)                # redraw the "speaking" indicator unprompted

    lines = load_transcript()
    sel, buf, cur, msg = None, "", 0, None

    def speak(text):
        nonlocal msg
        sp.error = None
        sp.say(text)
        msg = None

    while True:
        if sp.error:
            msg, sp.error = sp.error, None

        draw(scr, cfg, sp, lines, sel, buf, cur, msg)

        try:
            k = scr.get_wch()
        except curses.error:        # timeout tick: just redraw
            continue

        ctrl = ord(k) if isinstance(k, str) and len(k) == 1 and ord(k) < 32 else k

        if ctrl in (17, 4):                                     # ^Q / ^D quit
            return
        if ctrl == 3:                                           # ^C stop speech
            sp.stop()
            msg = "stopped"
            continue

        if k == curses.KEY_UP:
            sel = len(lines) - 1 if sel is None else max(0, sel - 1)
            continue
        if k == curses.KEY_DOWN:
            sel = None if sel is None or sel >= len(lines) - 1 else sel + 1
            continue

        if ctrl == 9:                                           # ⇥ saved phrases
            labels = [s["name"] for s in cfg["saved"]]
            if not labels:
                msg = "nothing saved yet — ^S saves the typed, picked or last-said line"
                continue

            def delete(i):
                cfg["saved"].pop(i)
                save(cfg)

            i = pick(scr, "saved  (⏎ speak · d delete)", labels, delete)
            if i is not None:
                speak(cfg["saved"][i]["text"])
            continue

        if ctrl == 22:                                          # ^V voice picker
            names = voice_names(cfg)
            if not names:
                msg = "no voices available for this backend"
                continue

            i = pick(scr, f"voice  ({cfg['backend']})", names)
            if i is not None:
                cfg["voice"] = names[i]
                save(cfg)
                msg = f"voice = {names[i]}"
            continue

        if ctrl == 19:                                          # ^S save a phrase
            text = save_target(buf, lines, sel)
            if not text:
                msg = "nothing to save"
            elif any(s["text"] == text for s in cfg["saved"]):
                msg = "already saved"
            else:
                cfg["saved"].append({"name": plain(text)[:40], "text": text})
                save(cfg)
                msg = f"saved {len(cfg['saved'])}: {text[:40]}"
            continue

        if ctrl in (10, 13) or k in ("\n", "\r", curses.KEY_ENTER):
            if sel is not None:
                speak(lines[sel])                   # re-say the picked line
            elif buf.strip():
                line = buf.strip()
                buf, cur = "", 0

                if line.startswith("//"):
                    line = line[1:]                 # //foo speaks "/foo"
                elif line.startswith("/"):
                    out = command(line, cfg, sp)
                    if out is False:
                        return
                    if out == "cleared":
                        lines = []
                        trim_transcript(lines)
                    msg = out
                    continue

                lines.append(line)
                append_transcript(line)
                speak(line)
            continue

        # anything else edits the input line, and drops out of picking
        before = buf
        buf, cur = edit(buf, cur, k)
        if buf != before:
            sel = None
            msg = None


def main():
    cfg = load()
    sp = Speaker(cfg)

    try:
        curses.wrapper(loop, cfg, sp)
    finally:
        sp.stop()
        trim_transcript(load_transcript())          # cap the file on the way out


# --- self-test --------------------------------------------------------------

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

    # line editor
    b, c = "", 0
    for ch in "hello wrld":
        b, c = edit(b, c, ch)
    assert (b, c) == ("hello wrld", 10)

    b, c = edit(b, c, curses.KEY_LEFT)
    b, c = edit(b, c, curses.KEY_LEFT)
    b, c = edit(b, c, curses.KEY_LEFT)
    b, c = edit(b, c, "o")
    assert b == "hello world", b

    assert edit("hello world", 11, 23) == ("hello ", 6)         # ^W
    assert edit("hello world", 11, 21) == ("", 0)               # ^U
    assert edit("hello world", 5, 11) == ("hello", 5)           # ^K
    assert edit("abc", 3, 127) == ("ab", 2)                     # backspace
    assert edit("", 0, 127) == ("", 0)                          # nothing to erase
    assert edit("abc", 3, 1) == ("abc", 0)                      # ^A
    assert edit("abc", 0, 5) == ("abc", 3)                      # ^E
    assert edit("cafe", 4, "é") == ("cafeé", 5)                 # non-ascii insert
    assert edit("abc", 3, curses.KEY_UP) == ("abc", 3)          # ignored, not eaten

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

    # settings commands
    real_save, globals()["save"] = save, lambda cfg: None
    try:
        assert command("/voice Alice", cfg, sp) and cfg["voice"] == "Alice"
        assert command("/rate 300", cfg, sp) and cfg["rate"] == 300
        assert "words per minute" in command("/rate fast", cfg, sp) and cfg["rate"] == 300
        assert "'say' or 'av'" in command("/backend nope", cfg, sp)
        assert cfg["backend"] == "say"
        assert command("/backend av", cfg, sp) and cfg["backend"] == "av"
        assert command("/voice", cfg, sp) == "voice = Alice"    # no arg reads
        assert command("/quit", cfg, sp) is False
        assert command("/nonsense", cfg, sp).startswith("unknown /nonsense")
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
    elif not shutil.which("say"):
        sys.exit("speak: /usr/bin/say not found — this is a macOS tool")
    else:
        main()
