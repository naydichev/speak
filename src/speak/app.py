#!/usr/bin/env python3
"""Full-screen talker: type a line, press Enter, keep typing while it talks.

    ┌──────────────────────────────────────────────────────────┐
    │ speak                          Daniel · 200wpm · say · 2 │  status
    │    1 hello there                                         │  transcript,
    │    2 how are you doing                                   │  newest last
    │    3 i'm doing fine, thanks                              │  ↑↓ picks one
    │    4 please be careful with that                         │  bold = _emph_
    │ > what i'm typing now                                    │  input
    │ ↑↓ pick · ⏎ speak · !3 redo · ⇥ saved · ^V voice · /help │  keys
    └──────────────────────────────────────────────────────────┘

Lines queue through one worker thread, so typing ahead speaks in order.
Wrap a word in _underscores_ or *stars* to emphasise it. Arrows pick a past
line to say again (wrapping at both ends); !3 says the line numbered 3.

All the logic lives in core.py, which imports no UI at all, so it is tested
headless: `uv run pytest`.
"""

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from speak import core

HINTS = "↑↓ pick · ⏎ speak · !3 redo · ⇥ saved · ^V voice · ^S save · ^C stop · /help"
CLOSED = -1                 # a Picker dismissed without choosing


def as_text(line, number=None, matched=()):
    """A transcript or picker row: dim number, bold emphasis, underlined matches."""
    out = Text()

    if number is not None:
        out.append(f"{number:>4} ", style="dim")

    if matched:                 # picker labels carry no emphasis markers
        for i, ch in enumerate(line):
            out.append(ch, style="bold underline" if i in matched else "")
        return out

    for chunk, em in core.spans(line):
        out.append(chunk, style="bold" if em else "")

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
        # priority: the focused Input claims ctrl+x for "cut"
        Binding("ctrl+x", "delete", "delete", priority=True),
        Binding("up", "move(-1)", "up", priority=True),
        Binding("down", "move(1)", "down", priority=True),
        Binding("pageup", "page(-1)", "page up", priority=True),
        Binding("pagedown", "page(1)", "page down", priority=True),
    ]

    def __init__(self, title, labels, on_delete=None):
        super().__init__()
        self.heading = title
        self.labels = list(labels)
        self.on_delete = on_delete
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

    def action_close(self) -> None:
        self.dismiss(CLOSED)


class Speak(App):
    CSS = """
    #status { dock: top; height: 1; background: $panel; padding: 0 1; }
    #hints { dock: bottom; height: 1; color: $text-muted; padding: 0 1; }
    #prompt { dock: bottom; }
    #transcript { height: 1fr; border: none; padding: 0 1; background: $surface; }

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
        width: 70; max-width: 95%; height: auto; max-height: 100%;
        padding: 1 2;
        border: round $accent; background: $surface;
        overflow-y: auto;               /* scroll rather than clip when short */
    }
    #help-body { height: auto; }
    #help-title { color: $accent; text-style: bold; }
    """

    BINDINGS = [
        Binding("ctrl+q", "quit", "quit", priority=True),
        Binding("ctrl+d", "quit", "quit", priority=True),
        Binding("ctrl+c", "stop", "stop talking", priority=True),
        Binding("tab", "saved", "saved phrases", priority=True),
        Binding("ctrl+v", "voice", "voice", priority=True),
        Binding("ctrl+s", "save_phrase", "save", priority=True),
        Binding("f1", "help", "keys", priority=True),
        # NOT priority: an app-level priority binding outranks the active
        # screen's, so an open Picker would never see its own arrows or escape.
        # The prompt Input claims none of these three, so they still arrive.
        Binding("up", "move(-1)", "up"),
        Binding("down", "move(1)", "down"),
        Binding("escape", "unpick", "unpick"),
    ]

    def __init__(self, run=None):
        super().__init__()
        self.cfg = core.load()
        self.sp = core.Speaker(self.cfg, run=run)     # run= lets tests record argv
        self.lines = core.load_transcript()
        self.sel = None

    # --- layout -------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static(id="status")
        yield OptionList(id="transcript")
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
        pending = self.sp.pending()
        bits = [
            core.voice_label(self.cfg["voice"]) or "default",
            f"{self.cfg['rate']}wpm" if self.cfg["rate"] else None,
            self.cfg["backend"],
            f"speaking +{pending - 1}" if pending > 1
            else ("speaking" if pending else None),
        ]
        right = " · ".join(b for b in bits if b)
        self.query_one("#status", Static).update(f"speak    {right}")

        if self.sp.error:
            self.note(self.sp.error)
            self.sp.error = None

    def note(self, text) -> None:
        self.query_one("#hints", Static).update(text or HINTS)

    # --- speaking -----------------------------------------------------------

    def speak(self, text) -> None:
        self.sp.error = None
        self.sp.say(text)
        self.note(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "prompt":
            return                          # a Picker's filter, not this prompt

        line = event.value.strip()
        event.input.value = ""

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

    def action_unpick(self) -> None:
        self.sel = None
        self.query_one("#transcript", OptionList).highlighted = None
        self.note(None)

    def action_stop(self) -> None:
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

        self.push_screen(
            Picker("saved   ⏎ speak · ^X delete",
                   [s["name"] for s in self.cfg["saved"]], drop),
            chosen,
        )

    def action_voice(self) -> None:
        voices = core.voice_names(self.cfg)

        if not voices:
            self.note("no voices available for this backend")
            return

        def chosen(i):
            if i is not None and i != CLOSED:
                self.cfg["voice"] = voices[i][1]
                core.save(self.cfg)
                self.note(f"voice = {voices[i][0]}")

        self.push_screen(
            Picker(f"voice · {self.cfg['backend']}", [label for label, _ in voices]),
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
            self.cfg["saved"].append({"name": core.plain(text)[:40], "text": text})
            core.save(self.cfg)
            self.note(f"saved {len(self.cfg['saved'])}: {core.plain(text)[:40]}")

    def action_help(self) -> None:
        self.push_screen(Help())

    def action_quit(self) -> None:
        self.exit()

    def on_unmount(self) -> None:
        self.sp.stop()
        core.trim_transcript(core.load_transcript())    # cap the file on the way out


def main():
    Speak().run()


if __name__ == "__main__":
    main()
