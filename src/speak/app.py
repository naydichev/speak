#!/usr/bin/env python3
"""A full-screen TUI for macOS's built-in `say`.

Type a line, press Enter, and keep typing while it talks.

    ┌──────────────────────────────────────────────────────────────┐
    │ speak                          Daniel · 200wpm · speaking +2 │  status
    │    1 hello there                                             │  transcript,
    │    2 how are you doing                                       │  newest last
    │    3 i'm doing fine, thanks                                  │  ↑↓ picks one
    │    4 was ist das                                             │
    │ ──────────────────────────────────────────────────────────── │
    │ > what i'm typing now                                        │  input
    │ ⏎ speak · !3 redo · ⇥ saved · ^V voice · Esc stop · /help   │  keys
    └──────────────────────────────────────────────────────────────┘

Lines queue through one worker thread, so typing ahead speaks in order.
Arrows pick a past line to say again (wrapping at both ends); !3 says the
line numbered 3, ^R edits one and ^X deletes one. /help lists every key.

All the logic lives in core.py, which imports no UI at all, so it is tested
headless: `uv run pytest`.
"""

import signal
import sys

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from speak import core

# The bar follows what you are doing, which is also how the line keys stay
# discoverable without a bar too wide to fit.
HINTS = "⏎ speak · !3 redo · ⇥ saved · ^V voice · ^S save · Esc stop · ^C quit · /help"
HINTS_PICKED = "⏎ say again · ^R edit · ^X delete · ^S save · Esc unpick · /help"
HINTS_EDITING = {
    "line": "⏎ replace line {n}, without saying it · Esc cancel",
    "saved": "⏎ rewrite saved phrase {n} · Esc cancel",
}
CLOSED = -1                 # a Picker dismissed without choosing


def as_text(line, number=None, matched=()):
    """A transcript or picker row: dim line number, underlined filter matches."""
    out = Text()

    if number is not None:
        out.append(f"{number:>4} ", style="dim")

    if matched:                 # a picker row: underline what the filter hit
        for i, ch in enumerate(line):
            out.append(ch, style="bold underline" if i in matched else "")
    else:
        out.append(line)

    return out


class Help(ModalScreen):
    """The key list.

    Closes on a named key only. Closing on *any* key meant the modifiers of
    a cmd+shift+4 screenshot dismissed it while you were capturing it.
    """

    BINDINGS = [Binding("escape,enter,space,q,question_mark", "close", "close")]

    def compose(self) -> ComposeResult:
        keyw = max(len(k) for k, d in core.HELP_ROWS if d)
        body = Text()

        for k, d in core.HELP_ROWS:
            if d is None:               # a heading, not a key
                body.append(f"\n{k}\n", style="bold underline")
                continue

            body.append(f"{k:>{keyw}}", style="bold")
            body.append(f"   {d}\n")

        with Vertical(id="help"):
            yield Static("speak · keys", id="help-title")
            yield Static(body, id="help-body")

    def action_close(self) -> None:
        self.dismiss()


class Picker(ModalScreen[int]):
    """Fuzzy-filtered list; dismisses with the chosen index into `labels`.

    Textual owns the box, the scrolling and the repaint. The two bugs this
    replaces were both a hand-drawn window failing to clean up after itself.
    """

    BINDINGS = [
        Binding("escape", "close", "close"),
        Binding("tab", "close", "close", priority=True),
        Binding("up", "move(-1)", "up", priority=True),
        Binding("down", "move(1)", "down", priority=True),
        Binding("pageup", "page(-1)", "page up", priority=True),
        Binding("pagedown", "page(1)", "page down", priority=True),
    ]

    def __init__(self, title, labels, on_delete=None, on_edit=None):
        super().__init__()
        self.heading = title
        self.labels = list(labels)
        self.on_delete = on_delete
        self.on_edit = on_edit
        self.hits = []

    def compose(self) -> ComposeResult:
        with Vertical(id="picker"):
            yield Static(id="picker-title")
            yield OptionList(id="picker-list")
            yield Input(placeholder="type to filter", id="picker-filter")

    def on_mount(self) -> None:
        self.repopulate()
        self.query_one("#picker-filter", Input).focus()

    def repopulate(self) -> None:
        query = self.query_one("#picker-filter", Input).value
        self.hits = core.fuzzy(self.labels, query)

        options = self.query_one("#picker-list", OptionList)
        options.clear_options()
        options.add_options(
            Option(as_text(self.labels[i], matched=set(pos))) for i, pos in self.hits
        )
        if self.hits:
            options.highlighted = 0

        self.query_one("#picker-title", Static).update(
            f"{self.heading}    {len(self.hits)}/{len(self.labels)}"
        )

    def on_input_changed(self) -> None:
        self.repopulate()

    def on_input_submitted(self) -> None:
        self.choose()

    def on_option_list_option_selected(self) -> None:
        self.choose()

    def choose(self) -> None:
        options = self.query_one("#picker-list", OptionList)

        if self.hits and options.highlighted is not None:
            self.dismiss(self.hits[options.highlighted][0])

    def action_move(self, delta: int) -> None:
        if not self.hits:
            return

        options = self.query_one("#picker-list", OptionList)
        options.highlighted = ((options.highlighted or 0) + delta) % len(self.hits)

    def action_page(self, direction: int) -> None:
        if not self.hits:
            return

        options = self.query_one("#picker-list", OptionList)
        page = max(1, options.size.height)
        at = (options.highlighted or 0) + direction * page
        options.highlighted = max(0, min(len(self.hits) - 1, at))

    def action_delete(self) -> None:
        options = self.query_one("#picker-list", OptionList)

        if not (self.hits and self.on_delete and options.highlighted is not None):
            return

        i = self.hits[options.highlighted][0]
        self.on_delete(i)
        self.labels.pop(i)
        self.repopulate()

    def action_edit(self) -> None:
        """Hand the row back to the caller, which owns the prompt."""
        options = self.query_one("#picker-list", OptionList)

        if not (self.hits and self.on_edit and options.highlighted is not None):
            return

        i = self.hits[options.highlighted][0]
        self.dismiss(CLOSED)
        self.on_edit(i)

    def action_close(self) -> None:
        self.dismiss(CLOSED)


class Speak(App):
    CSS = """
    #status { dock: top; height: 1; background: $panel; padding: 0 1; }
    #transcript { height: 1fr; border: none; padding: 0 1; background: $surface; }

    /* Docking the prompt and the bar separately made them contend for the
       same rows, and the Input's lower rule was the one that lost. */
    #bottom { dock: bottom; height: 3; }
    #promptrow { height: 2; border-top: solid $accent; }
    #caret { width: 3; padding: 0 0 0 1; color: $accent; }
    #prompt { border: none; height: 1; padding: 0; }
    #hints { height: 1; color: $text-muted; padding: 0 1; }

    Picker, Help { align: center middle; background: $background 60%; }

    #picker {
        width: 74; max-width: 90%; height: auto; max-height: 80%;
        padding: 0 1;
        border: round $accent; background: $surface;
    }
    #picker-title { color: $accent; text-style: bold; height: 1; }
    #picker-list { height: auto; max-height: 20; border: none; background: $surface; }

    /* width must be explicit: `auto` on this container collapses it */
    #help {
        width: 74; max-width: 95%; height: auto; max-height: 100%;
        padding: 1 2;
        border: round $accent; background: $surface;
        overflow-y: auto;               /* scroll rather than clip when short */
    }
    #help-body { height: auto; }
    #help-title { color: $accent; text-style: bold; }
    """

    BINDINGS = [
        Binding("ctrl+d", "quit", "quit", priority=True),
        Binding("ctrl+c", "quit", "quit", priority=True),
        Binding("tab", "saved", "saved phrases", priority=True),
        Binding("ctrl+v", "voice", "voice", priority=True),
        Binding("ctrl+s", "save_phrase", "save", priority=True),
        # ^R rather than ^E: Input binds ctrl+e to end-of-line. ^X costs the
        # prompt its "cut", which nothing here needs, and matches the pickers.
        Binding("ctrl+r", "edit_line", "edit the picked line", priority=True),
        Binding("ctrl+x", "delete_line", "delete the picked line", priority=True),
        # No ctrl+h: textual reports it as `backspace`, same as the backspace
        # key, so binding it would break editing the prompt. F1 needs no
        # mnemonic, and /help is there for keyboards where F1 is awkward.
        Binding("f1", "help", "keys", priority=True),
        # NOT priority: an app-level priority binding outranks the active
        # screen's, so an open Picker would never see its own arrows or escape.
        # The prompt Input claims none of these three, so they still arrive.
        Binding("up", "move(-1)", "up"),
        Binding("down", "move(1)", "down"),
        Binding("escape", "cancel", "cancel"),
    ]

    def __init__(self, run=None):
        super().__init__()
        self.cfg = core.load()
        self.sp = core.Speaker(self.cfg, run=run)     # run= lets tests record argv
        self.lines = core.load_transcript()
        self.sel = None
        self.editing = None         # ("line" | "saved", index), if any

    # --- layout -------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static(id="status")
        yield OptionList(id="transcript")

        with Vertical(id="bottom"):
            with Horizontal(id="promptrow"):
                yield Static(">", id="caret")
                yield Input(placeholder="say something", id="prompt")

            yield Static(HINTS, id="hints")

    def on_mount(self) -> None:
        self.repopulate()
        self.query_one("#prompt", Input).focus()
        self.set_interval(0.2, self.show_status)    # drives the "speaking" indicator

    def repopulate(self) -> None:
        options = self.query_one("#transcript", OptionList)
        options.clear_options()
        options.add_options(
            Option(as_text(line, number=i + 1)) for i, line in enumerate(self.lines)
        )
        options.highlighted = self.sel

        if self.lines and self.sel is None:
            options.scroll_end(animate=False)       # newest stays in view

    def show_status(self) -> None:
        # Fires every 0.2s, including while the widgets are being torn down,
        # so it cannot assume they are still mounted. Raising here throws from
        # a timer callback: a traceback on exit, and a flaky test suite.
        if not self.query("#status"):
            return

        pending = self.sp.pending()
        bits = [
            self.cfg["voice"] or "default",
            f"{self.cfg['rate']}wpm" if self.cfg["rate"] else None,
            f"speaking +{pending - 1}" if pending > 1
            else ("speaking" if pending else None),
        ]
        right = " · ".join(b for b in bits if b)
        self.query_one("#status", Static).update(f"speak    {right}")

        if self.sp.error:
            self.note(self.sp.error)
            self.sp.error = None

    def note(self, text=None) -> None:
        """Show a message, or fall back to the keys for the current context."""
        if text is None:
            if self.editing is not None:
                kind, at = self.editing
                text = HINTS_EDITING[kind].format(n=at + 1)
            elif self.sel is not None:
                text = HINTS_PICKED
            else:
                text = HINTS

        self.query_one("#hints", Static).update(text)

    # --- speaking -----------------------------------------------------------

    def speak(self, text) -> None:
        self.sp.error = None
        self.sp.say(text)
        self.note()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "prompt":
            return                          # a Picker's filter, not this prompt

        line = event.value.strip()
        event.input.value = ""

        if self.editing is not None:
            (kind, at), self.editing = self.editing, None

            if not line:
                self.note("edit cancelled")     # submitted empty
            elif kind == "line":
                self.lines[at] = line
                core.trim_transcript(self.lines)
                self.sel = at
                self.repopulate()
                # deliberately silent: editing is fixing the record, not
                # saying it. It stays picked, so ⏎ again speaks it.
                self.note(f"line {at + 1} replaced — ⏎ says it")
            else:
                self.cfg["saved"][at] = {"name": line[:40], "text": line}
                core.save(self.cfg)
                self.note(f"saved phrase {at + 1} is now: {line[:40]}")
            return

        if not line:
            # ⏎ on an empty prompt says the picked line again. The Input has
            # focus, so it swallows the key before OptionList ever sees it.
            if self.sel is not None:
                self.speak(self.lines[self.sel])
            return

        if line.startswith("\\"):
            line = line[1:]                 # \foo speaks a literal /foo or !3

        elif (i := core.bang(line, len(self.lines))) is not None:
            if i < 0:
                self.note(f"no line {line[1:] or 1}")
            else:
                self.sel = i                # so ↑↓ and ^S carry on from here
                self.query_one("#transcript", OptionList).highlighted = i
                self.speak(self.lines[i])
            return

        elif line.startswith("/"):
            action, message = core.command(line, self.cfg)

            if action == "help":
                self.push_screen(Help())
            elif action == "clear":
                self.lines, self.sel = [], None
                core.trim_transcript(self.lines)
                self.repopulate()

            self.note(message)
            return

        self.lines.append(line)
        core.append_transcript(line)
        self.sel = None
        self.repopulate()
        self.speak(line)

    # --- picking ------------------------------------------------------------

    def action_move(self, delta: int) -> None:
        if not self.lines:
            return

        # entering the list starts at the near end, then wraps at both
        if self.sel is None:
            self.sel = len(self.lines) - 1 if delta < 0 else 0
        else:
            self.sel = (self.sel + delta) % len(self.lines)

        self.query_one("#transcript", OptionList).highlighted = self.sel
        self.note()

    def action_edit_line(self) -> None:
        """^R edits the highlighted row — a picker's, or the transcript's."""
        if isinstance(self.screen, Picker):
            self.screen.action_edit()
            return

        if self.screen is not self.screen_stack[0]:
            return                          # some other overlay owns the keys

        if self.sel is None:
            self.note("pick a line with ↑↓ first")
            return

        self.start_edit(("line", self.sel), self.lines[self.sel])

    def start_edit(self, target, text) -> None:
        prompt = self.query_one("#prompt", Input)
        prompt.value = text
        prompt.cursor_position = len(text)
        prompt.focus()

        self.editing = target
        self.note()

    def action_delete_line(self) -> None:
        """^X deletes the highlighted row, wherever you are.

        An app-level priority binding outranks the active screen's, so an open
        Picker never sees its own ^X — this has to hand it over by hand. The
        priority is needed at all because the focused Input claims ctrl+x for
        "cut", which nothing here uses.
        """
        if isinstance(self.screen, Picker):
            self.screen.action_delete()
            return

        if self.sel is None:
            self.note("pick a line with ↑↓ first")
            return

        gone = self.lines.pop(self.sel)
        core.trim_transcript(self.lines)

        # stay where you were, or step back off the end
        self.sel = min(self.sel, len(self.lines) - 1) if self.lines else None
        self.editing = None
        self.repopulate()
        self.note(f"deleted: {gone[:40]}")

    def action_cancel(self) -> None:
        """Esc means cancel, and what there is to cancel depends on where you are.

        Stopping the speech is deliberately last. Esc is also how you get back
        to typing from a picked line, and silently dropping a queue you had
        just typed ahead would be a nasty thing for a navigation key to do.
        """
        if self.editing is not None:
            self.editing = None
            self.query_one("#prompt", Input).value = ""
            self.note("edit cancelled")
            return

        if self.sel is not None:
            self.sel = None
            self.query_one("#transcript", OptionList).highlighted = None
            self.note()
            return

        self.sp.stop()
        self.note("stopped")

    # --- overlays -----------------------------------------------------------

    def action_saved(self) -> None:
        if not self.cfg["saved"]:
            self.note("nothing saved yet — ^S saves the typed, picked or last line")
            return

        def drop(i):
            self.cfg["saved"].pop(i)
            core.save(self.cfg)

        def chosen(i):
            if i is not None and i != CLOSED:
                self.speak(self.cfg["saved"][i]["text"])

        def rewrite(i):
            self.start_edit(("saved", i), self.cfg["saved"][i]["text"])

        self.push_screen(
            Picker("saved   ⏎ speak · ^R edit · ^X delete",
                   [s["name"] for s in self.cfg["saved"]], drop, rewrite),
            chosen,
        )

    def action_voice(self) -> None:
        voices = core.voice_names()

        if not voices:
            self.note("no voices available")
            return

        def chosen(i):
            if i is not None and i != CLOSED:
                self.cfg["voice"] = voices[i][1]
                core.save(self.cfg)
                self.note(f"voice = {voices[i][0]}")

        self.push_screen(
            Picker("voice", [label for label, _ in voices]),
            chosen,
        )

    def action_save_phrase(self) -> None:
        typed = self.query_one("#prompt", Input).value
        text = core.save_target(typed, self.lines, self.sel)

        if not text:
            self.note("nothing to save")
        elif any(s["text"] == text for s in self.cfg["saved"]):
            self.note("already saved")
        else:
            self.cfg["saved"].append({"name": text[:40], "text": text})
            core.save(self.cfg)
            self.note(f"saved {len(self.cfg['saved'])}: {text[:40]}")

    def action_help(self) -> None:
        self.push_screen(Help())

    def action_quit(self) -> None:
        self.exit()

    def on_unmount(self) -> None:
        self.sp.stop()
        core.trim_transcript(core.load_transcript())    # cap the file on the way out


USAGE = """speak — a full-screen TUI for macOS's built-in `say`.

usage: speak [--version]

No options worth having: everything is a key or a /command inside the app.
Type /help there for the list. State lives in
~/.config/speak/config.json and ~/.local/share/speak/transcript.
"""


def main():
    # A bare TUI that swallows --help and then blocks on a terminal it hasn't
    # got is no fun to discover from a script.
    if {"-h", "--help"} & set(sys.argv[1:]):
        print(USAGE, end="")
        return

    if "--version" in sys.argv[1:]:
        from speak import __version__

        print(f"speak {__version__}")
        return

    if not sys.stdout.isatty():
        sys.exit("speak: needs a terminal (stdout is not a tty)")

    app = Speak()

    # Python's default handlers for these exit without unwinding, so textual
    # never restores the terminal. Measured: the app turns mouse tracking on
    # (?1000h ?1003h ?1006h) and neither signal emitted a matching `l` —
    # leaving the shell echoing raw mouse reports as text over a dead screen.
    # SIGHUP is what a closing terminal window sends, so it is not exotic.
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_: app.exit())

    app.run()


if __name__ == "__main__":
    main()
