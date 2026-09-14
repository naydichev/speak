"""Everything `speak` does apart from drawing: speech, config, matching.

Imports no UI toolkit, so tests/test_core.py runs headless.
"""

import json
import os
import queue
import re
import subprocess
import threading

CONFIG = os.path.expanduser("~/.config/speak/config.json")
TRANSCRIPT = os.path.expanduser("~/.local/share/speak/transcript")
DEFAULTS = {"voice": None, "rate": None, "saved": []}


SAY_BASE_WPM = 175          # `say`'s own default, measured: `say -r 175` is
                            # byte-identical to no -r at all, on every voice
                            # tried including a trained personal one
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

    # A voice stored by the old AVSpeechSynthesizer backend is an identifier,
    # which `say` cannot use — and `say` substitutes a default silently rather
    # than failing, which is near-undiagnosable.
    if str(cfg["voice"]).startswith("com.apple."):
        cfg["voice"] = None

    return cfg


def save(cfg):
    """Write the config, or leave the previous one untouched.

    Opening CONFIG directly truncates it first, so a failure part-way through
    json.dump left a half-written file and every saved phrase was gone. Write
    beside it and rename: os.replace is atomic within a filesystem.
    """
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    tmp = f"{CONFIG}.new"

    try:
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=2)

        os.replace(tmp, CONFIG)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


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


class Speaker:
    """Serialises spoken lines through one worker thread.

    `run` is injected so tests record argv instead of talking.
    """

    def __init__(self, cfg, run=None):
        self.cfg = cfg
        self.run = run or subprocess.Popen
        self.q = queue.Queue()
        self.proc = None
        self.lock = threading.Lock()

        # Written by the worker, read and cleared by the UI. A plain
        # attribute is enough: one writer, and a lost message would only
        # ever be a stale duplicate of one already shown.
        self.error = None

        threading.Thread(target=self._drain, daemon=True).start()

    def argv(self, text):
        cmd = ["say"]
        if self.cfg["voice"]:
            cmd += ["-v", self.cfg["voice"]]
        if self.cfg["rate"]:
            cmd += ["-r", str(self.cfg["rate"])]

        # `--` so a line starting with '-' is text, not a flag
        return cmd + ["--", text]

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


def voice_names():
    """[(label, name)]: the label is shown, the name goes to `say -v`.

    The first row clears the setting. It has to exist, because `say` with no
    -v uses the System Voice from Settings > Accessibility > Spoken Content,
    and if that is a Siri voice then `say -v ?` does not list it — measured:
    its audio matched none of the listed names. Without this row, picking any
    voice is a one-way door away from the default.
    """
    out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True)

    return [("(system default)", None)] + parse_say_voices(out.stdout)


# Terminal only appears in Settings > Accessibility > Personal Voice once
# something running as it has asked -- `say` itself never does, so a trained
# voice silently falls back to another one until this runs once.
#
# requestPersonalVoiceAuthorization's own completion handler cannot be
# trusted to fire: confirmed live by triggering the real system alert,
# approving it, and watching the process's own log --
#   (TextToSpeech) Did request personal voice TCC for (null). granted=1
# -- land under a second later, while `sample` kept showing the process
# still parked in the semaphore wait on that handler minutes on. The grant
# itself is real; only the callback delivery is broken for a bare binary
# like this one. personalVoiceAuthorizationStatus, the synchronous property,
# does update the instant the grant lands, so that's what this polls instead
# of trusting the handler to ever run.
PERSONAL_VOICE_SWIFT = """
import AVFoundation

func describe(_ status: AVSpeechSynthesizer.PersonalVoiceAuthorizationStatus) -> String {
    switch status {
    case .authorized: return "authorized -- Terminal can use a trained Personal Voice now"
    case .denied: return "denied -- allow Terminal in Settings > Accessibility > Personal Voice"
    case .notDetermined: return "no answer to the prompt"
    case .unsupported: return "unsupported -- train one in Settings > Accessibility > Personal Voice first"
    @unknown default: return "unknown status"
    }
}

var status = AVSpeechSynthesizer.personalVoiceAuthorizationStatus
if status == .notDetermined {
    AVSpeechSynthesizer.requestPersonalVoiceAuthorization { _ in }

    var waited = 0.0
    while status == .notDetermined && waited < 120 {
        Thread.sleep(forTimeInterval: 0.2)
        waited += 0.2
        status = AVSpeechSynthesizer.personalVoiceAuthorizationStatus
    }
}
print(describe(status))
"""


PERSONAL_VOICE_BIN = os.path.expanduser("~/.cache/speak/request_personal_voice")


def request_personal_voice(run=subprocess.run):
    """Compile the ask once, cache it, then run it. True/False on a clean
    exit, None if the Swift toolchain isn't installed.

    `swift -e` runs the code interpreted inside swift-frontend rather than as
    a real linked binary, and in that mode the completion handler for
    requestPersonalVoiceAuthorization never fires -- confirmed by sampling a
    hung run: the process sat in a semaphore wait with zero TCC activity in
    the system log the entire time. A compiled binary is required.
    """
    if not os.path.exists(PERSONAL_VOICE_BIN):
        os.makedirs(os.path.dirname(PERSONAL_VOICE_BIN), exist_ok=True)
        src = PERSONAL_VOICE_BIN + ".swift"

        with open(src, "w") as f:
            f.write(PERSONAL_VOICE_SWIFT)

        try:
            subprocess.run(
                ["swiftc", "-O", src, "-o", PERSONAL_VOICE_BIN],
                check=True, capture_output=True,
            )
        except FileNotFoundError:
            return None
        finally:
            os.remove(src)

    return run([PERSONAL_VOICE_BIN]).returncode == 0


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
    decides what to do with it rather than reading a sentinel. There is no
    /quit; ^C and ^D do it, and so does ^Q, which textual binds itself.
    """
    word, _, arg = line[1:].partition(" ")
    arg = arg.strip()

    if word in ("help", "h", "?", ""):
        return "help", None

    if word == "clear":
        return "clear", "cleared"

    if word in ("voice", "rate"):
        # A bare /command resets to the default. Nothing is lost by dropping
        # the old read-back: the status bar shows all three at all times.
        if not arg:
            cfg[word] = DEFAULTS[word]
            save(cfg)

            if word == "rate":
                return "msg", f"rate = default ({SAY_BASE_WPM} wpm)"

            return "msg", "voice = system default"

        # "0" passes isdigit() but reads as falsy later, so it would have
        # silently meant "default" instead of being rejected.
        if word == "rate" and not (arg.isdigit() and int(arg) > 0):
            return "msg", "rate takes words per minute, e.g. /rate 220"
        cfg[word] = int(arg) if word == "rate" else arg
        save(cfg)
        return "msg", f"{word} = {cfg[word]}"

    return "msg", f"unknown /{word} — /voice /rate /clear /help"


def save_target(buf, lines, sel):
    """What ^S saves: what you're typing, else the picked line, else the last said."""
    if buf.strip():
        return buf.strip()
    if sel is not None and 0 <= sel < len(lines):
        return lines[sel]

    return lines[-1] if lines else ""

# Two spellings, one rule: a key does something to what you are saying, a
# /command configures the app. A row with no description is a heading.
HELP_ROWS = [
    ("keys", None),
    ("type + ⏎",         "speak it — queued, so carry on typing"),
    ("↑ ↓",              "pick a past line, wrapping at both ends"),
    ("⏎",                "say the picked line again"),
    ("Esc",              "unpick, or stop talking if nothing is picked"),
    ("!3",               "say line 3; ! alone repeats the last"),
    ("^R",               "edit the picked line, or a saved phrase"),
    ("^X",               "delete the picked line"),
    ("⇥",                "saved phrases — filter, ^R edits, ^X deletes"),
    ("^V",               "voice — type to filter"),
    ("^S",               "save the typed, picked, or last-said line"),
    ("^C  ^D  ^Q",       "quit"),
    ("\\",               "speak a literal leading / or !"),
    ("F1",               "this list — or type /help"),

    ("commands", None),
    ("/voice <name>",    "set by name; bare /voice restores the default"),
    ("/rate <wpm>",      "e.g. /rate 200; bare /rate is 175, the default"),
    ("/clear",           "empty the transcript"),
    ("/help",            "this list"),
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
