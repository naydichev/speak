#!/usr/bin/env python3
"""Full-screen talker: type a line, press Enter, keep typing while it talks.

    ┌ speak ──────────────────────── Daniel · 200wpm · say · 2 ─┐
    │ hello there                                               │  transcript,
    │ how are you doing                                         │  newest last
    │▸i'm doing fine, thanks                                    │  ▸ = ↑↓ pick
    │ please be careful with that                               │  bold = _emph_
    ├───────────────────────────────────────────────────────────┤
    │ > what i'm typing now_                                    │  input
    └ ↑↓ pick · ⏎ speak · !3 redo · ⇥ saved · ^S save · ^C stop ┘

Lines queue through one worker thread, so typing ahead speaks in order.
Wrap a word in _underscores_ or *stars* to emphasise it. Arrows pick a
past line to say again (wrapping at both ends); !3 says the line
numbered 3 straight from the prompt. ⇥ and ^V open fuzzy-filtered lists.

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
        return HELP

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


QUIT = object()             # pick() saw ^Q: the caller should exit, not reopen
HELP = object()             # /help: the caller should open the modal

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


def key(k):
    """get_wch() gives str for characters and int for special keys; control
    characters arrive as str too, so fold those to their ordinal."""
    return ord(k) if isinstance(k, str) and len(k) == 1 and ord(k) < 32 else k


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


def modal(scr, title, rows):
    """Box of key/description pairs, dismissed by any key.

    ponytail: clips rather than scrolls — 15 rows fit an 80x24 terminal, and
    a shorter one can be resized. Add scrolling if the list outgrows a screen.
    """
    keyw = max(len(k) for k, _ in rows)
    body = [f"{k:>{keyw}}   {d}" for k, d in rows]

    h, w = scr.getmaxyx()
    width = min(max(len(title) + 4, max(len(l) for l in body) + 2), w - 4)
    tall = min(len(body), max(1, h - 4))

    win = curses.newwin(tall + 2, width + 2,
                        max(0, (h - tall) // 2 - 1), max(0, (w - width) // 2))
    win.keypad(True)
    win.erase()
    win.box()
    put(win, 0, 2, f" {title} ", curses.A_BOLD)

    for r, line in enumerate(body[:tall]):
        put(win, r + 1, 1, " " + line)

    win.refresh()
    win.get_wch()

    scr.touchwin()              # repaint underneath, or the border lingers
    scr.refresh()


def pick(scr, title, labels, delete=None):
    """Modal fuzzy-filtered list. Returns the chosen index, or None if cancelled.

    Typing filters the list; matched letters are underlined. `delete` is called
    with the highlighted row's index on ^X, and the row leaves this list too.
    """
    labels = list(labels)
    q, cur, sel = "", 0, 0

    try:
        while True:
            if not labels:
                return None

            hits = fuzzy(labels, q)
            sel = min(sel, max(0, len(hits) - 1))

            h, w = scr.getmaxyx()
            rows = max(1, min(max(1, len(hits)), h - 6))
            width = min(max(len(title) + 4, max(len(l) for l in labels) + 4), w - 4)

            # keep the selection inside the window
            view = min(max(0, sel - rows // 2), max(0, len(hits) - rows))

            # The box shrinks as the query narrows, so repaint the screen
            # underneath FIRST — otherwise the previous, larger border is left
            # behind around the new one.
            scr.touchwin()
            scr.refresh()

            win = curses.newwin(rows + 4, width + 2,
                                max(0, (h - rows - 4) // 2), max(0, (w - width) // 2))
            win.keypad(True)        # a fresh window does NOT inherit it
            win.erase()
            win.box()
            put(win, 0, 2, f" {title} ", curses.A_BOLD)

            if not hits:
                put(win, 1, 1, "  no match", curses.A_DIM)

            for r in range(min(rows, len(hits) - view)):
                i, pos = hits[view + r]
                attr = curses.A_REVERSE if view + r == sel else 0
                mark = "▸ " if view + r == sel else "  "

                put(win, r + 1, 1, (mark + labels[i]).ljust(width), attr)
                for j in pos:       # underline what the query matched
                    put(win, r + 1, 1 + len(mark) + j, labels[i][j],
                        attr | curses.A_UNDERLINE | curses.A_BOLD)

            count = f" {len(hits)}/{len(labels)} > "
            put(win, rows + 2, 1, (count + q).ljust(width))
            win.move(rows + 2, min(1 + len(count) + cur, width))
            win.refresh()

            k = win.get_wch()
            ctrl = key(k)           # get_wch gives '\x11' for ^Q, never 17

            if k == curses.KEY_UP and hits:
                sel = (sel - 1) % len(hits)             # wraps
            elif k == curses.KEY_DOWN and hits:
                sel = (sel + 1) % len(hits)
            elif k == curses.KEY_PPAGE:
                sel = max(0, sel - rows)
            elif k == curses.KEY_NPAGE:
                sel = min(len(hits) - 1, sel + rows)
            elif ctrl in (10, 13) or k == curses.KEY_ENTER:
                if hits:
                    return hits[sel][0]
            elif ctrl == 24 and delete and hits:        # ^X — a letter would filter
                i = hits[sel][0]
                delete(i)
                labels.pop(i)
                sel = 0
            elif ctrl == 17:                            # ^Q quits the app outright
                return QUIT
            elif ctrl in (27, 9, 3):                    # Esc, Tab, ^C just close
                return None
            else:
                q, cur = edit(q, cur, k)
                sel = 0
    finally:
        scr.touchwin()              # repaint underneath, or the border lingers
        scr.refresh()


def draw(scr, cfg, sp, lines, sel, buf, cur, msg):
    h, w = scr.getmaxyx()
    scr.erase()

    # status bar
    n = sp.pending()
    right = " · ".join(filter(None, [
        voice_label(cfg["voice"]) or "default",
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

        n = f"{i + 1:>3} "                          # !3 says line 3
        put(scr, r + 1, 1, n, attr | curses.A_DIM)

        x = 1 + len(n)
        for chunk, em in spans(lines[i]):           # markers off, bold on
            put(scr, r + 1, x, chunk, attr | (curses.A_BOLD if em else 0))
            x += len(chunk)

    # input line
    put(scr, h - 2, 0, "> " + buf)

    hint = msg or ("↑↓ pick · ⏎ speak · !3 redo · ⇥ saved · ^V voice · "
               "^S save · ^C stop · ^Q quit · /help")
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

        ctrl = key(k)

        if ctrl in (17, 4):                                     # ^Q / ^D quit
            return
        if ctrl == 3:                                           # ^C stop speech
            sp.stop()
            msg = "stopped"
            continue

        if k in (curses.KEY_UP, curses.KEY_DOWN) and lines:
            if sel is None:
                sel = len(lines) - 1 if k == curses.KEY_UP else 0
            else:
                sel = (sel + (-1 if k == curses.KEY_UP else 1)) % len(lines)
            continue

        if ctrl == 27:                                          # Esc drops the pick
            sel, msg = None, None
            continue

        if ctrl == 9:                                           # ⇥ saved phrases
            labels = [s["name"] for s in cfg["saved"]]
            if not labels:
                msg = "nothing saved yet — ^S saves the typed, picked or last-said line"
                continue

            def delete(i):
                cfg["saved"].pop(i)
                save(cfg)

            i = pick(scr, "saved  (⏎ speak · ^X delete · type to filter)",
                     labels, delete)
            if i is QUIT:
                return
            if i is not None:
                speak(cfg["saved"][i]["text"])
            continue

        if ctrl == 22:                                          # ^V voice picker
            voices = voice_names(cfg)
            if not voices:
                msg = "no voices available for this backend"
                continue

            i = pick(scr, f"voice · {cfg['backend']}  (type to filter)",
                     [label for label, _ in voices])
            if i is QUIT:
                return
            if i is not None:
                cfg["voice"] = voices[i][1]
                save(cfg)
                msg = f"voice = {voices[i][0]}"
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

                if line.startswith("\\"):
                    line = line[1:]                 # \\foo speaks a literal /foo or !3
                elif (i := bang(line, len(lines))) is not None:
                    if i < 0:
                        msg = f"no line {line[1:] or 1}"
                    else:
                        sel = i                     # so ↑↓ and ^S carry on from here
                        speak(lines[i])
                    continue
                elif line.startswith("/"):
                    out = command(line, cfg, sp)
                    if out is False:
                        return
                    if out is HELP:
                        modal(scr, "speak · keys", HELP_ROWS)
                        continue
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
        assert command("/help", cfg, sp) is HELP        # the caller opens the modal
        assert command("/", cfg, sp) is HELP
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
