"""Interaction checks, driven through textual's pilot.

These exist because the interaction layer is where the bugs actually landed:
a picker that never repainted, ^Q swallowed inside an overlay, and — after
the port — ⏎ eaten by the focused Input, and ^X taken by its "cut" binding.
Each test below names the mistake it would catch.
"""

import sys

import pytest
from textual.widgets import Input, Static

from speak import __version__, core
from speak.app import Help, Picker, Speak, main


@pytest.fixture
def app(tmp_path, monkeypatch):
    """A Speak whose config, transcript and speech call are all disposable."""
    monkeypatch.setattr(core, "CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setattr(core, "TRANSCRIPT", str(tmp_path / "transcript"))

    spoken = []

    class FakeProc:
        returncode = 0

        def communicate(self):
            return b"", b""

        def kill(self):
            pass

    def run(argv, **kw):
        spoken.append(argv[-1])         # record it, or a wrong line would pass
        return FakeProc()

    instance = Speak(run=run)
    instance.spoken = spoken

    return instance


def hint(instance):
    return str(instance.query_one("#hints", Static).content)


async def seed(pilot, instance, *lines):
    for line in lines:
        instance.query_one("#prompt", Input).value = line
        await pilot.press("enter")

    instance.sp.q.join()
    instance.spoken.clear()


# --- the command line ---

@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_help_prints_instead_of_launching(flag, monkeypatch, capsys):
    """It used to swallow the flag and then block on a terminal it hadn't got."""
    monkeypatch.setattr(sys, "argv", ["speak", flag])

    main()

    assert "usage: speak" in capsys.readouterr().out


def test_version_prints(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["speak", "--version"])

    main()

    assert __version__ in capsys.readouterr().out


def test_no_terminal_exits_rather_than_hanging(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["speak"])
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)

    with pytest.raises(SystemExit) as raised:
        main()

    assert "needs a terminal" in str(raised.value)


# --- speaking ---------------------------------------------------------------

async def test_typing_a_line_speaks_it_and_records_it(app):
    async with app.run_test() as pilot:
        await seed(pilot, app, "hello there")

        assert app.lines == ["hello there"]
        assert core.load_transcript() == ["hello there"]


async def test_the_line_is_spoken_exactly_as_typed(app):
    """No markup of any kind now: emphasis never worked and was cut."""
    async with app.run_test() as pilot:
        app.query_one("#prompt", Input).value = "be _careful_ now"
        await pilot.press("enter")
        app.sp.q.join()

        assert app.spoken == ["be _careful_ now"]


# --- picking ----------------------------------------------------------------

async def test_enter_on_an_empty_prompt_repeats_the_picked_line(app):
    """The focused Input swallows ⏎, so OptionList never sees it — this was
    silent until the empty-submit path spoke the pick itself."""
    async with app.run_test() as pilot:
        await seed(pilot, app, "one", "two", "three")

        await pilot.press("up", "up")           # line 2
        await pilot.press("enter")
        app.sp.q.join()

        assert app.spoken == ["two"]


async def test_enter_on_an_empty_prompt_with_no_pick_stays_silent(app):
    async with app.run_test() as pilot:
        await seed(pilot, app, "one")

        await pilot.press("enter")
        app.sp.q.join()

        assert app.spoken == []


async def test_arrows_enter_at_the_near_end_and_wrap(app):
    async with app.run_test() as pilot:
        await seed(pilot, app, "one", "two", "three")

        await pilot.press("up")
        assert app.sel == 2                     # ↑ enters at the newest
        await pilot.press("up", "up")
        assert app.sel == 0
        await pilot.press("up")
        assert app.sel == 2                     # wraps past the top

        await pilot.press("escape")
        await pilot.press("down")
        assert app.sel == 0                     # ↓ enters at the top
        await pilot.press("up")
        assert app.sel == 2                     # wraps the other way


async def test_escape_unpicks(app):
    async with app.run_test() as pilot:
        await seed(pilot, app, "one", "two")

        await pilot.press("up")
        assert app.sel is not None

        await pilot.press("escape")
        assert app.sel is None


async def test_bang_speaks_the_numbered_line_and_leaves_it_picked(app):
    async with app.run_test() as pilot:
        await seed(pilot, app, "one", "two", "three")

        app.query_one("#prompt", Input).value = "!2"
        await pilot.press("enter")
        app.sp.q.join()

        assert app.spoken == ["two"]
        assert app.sel == 1                     # so ↑↓ and ^S carry on from here


async def test_bang_out_of_range_reports_instead_of_speaking(app):
    async with app.run_test() as pilot:
        await seed(pilot, app, "one")

        app.query_one("#prompt", Input).value = "!9"
        await pilot.press("enter")

        assert app.spoken == []
        assert "no line 9" in hint(app)


async def test_backslash_escapes_a_leading_bang(app):
    async with app.run_test() as pilot:
        await seed(pilot, app, "one")

        app.query_one("#prompt", Input).value = "\\!2"
        await pilot.press("enter")
        app.sp.q.join()

        assert app.spoken == ["!2"]


# --- reserved keys ----------------------------------------------------------

async def test_ctrl_c_stops_talking_instead_of_quitting(app):
    """textual reserves ctrl+c for quit; the binding overrides it."""
    async with app.run_test() as pilot:
        await seed(pilot, app, "one")

        await pilot.press("ctrl+c")

        assert app.is_running
        assert hint(app) == "stopped"


async def test_tab_opens_the_saved_list_instead_of_moving_focus(app):
    async with app.run_test() as pilot:
        await seed(pilot, app, "one")
        app.cfg["saved"] = [{"name": "yes", "text": "yes please"}]

        await pilot.press("tab")
        await pilot.pause()

        assert isinstance(app.screen, Picker)


async def test_tab_with_nothing_saved_says_so(app):
    async with app.run_test() as pilot:
        await pilot.press("tab")
        await pilot.pause()

        assert not isinstance(app.screen, Picker)
        assert "nothing saved" in hint(app)


# --- saving and the pickers -------------------------------------------------

async def test_ctrl_s_saves_the_last_said_line(app):
    async with app.run_test() as pilot:
        await seed(pilot, app, "one", "two")

        await pilot.press("ctrl+s")

        assert [s["text"] for s in app.cfg["saved"]] == ["two"]


async def test_ctrl_s_saves_what_is_typed_before_the_transcript(app):
    async with app.run_test() as pilot:
        await seed(pilot, app, "one")
        app.query_one("#prompt", Input).value = "not sent yet"

        await pilot.press("ctrl+s")

        assert [s["text"] for s in app.cfg["saved"]] == ["not sent yet"]


async def test_ctrl_s_labels_a_saved_phrase_with_its_own_text(app):
    async with app.run_test() as pilot:
        await seed(pilot, app, "be careful now")

        await pilot.press("ctrl+s")

        assert app.cfg["saved"] == [{"name": "be careful now", "text": "be careful now"}]


async def test_picking_a_saved_phrase_speaks_it(app):
    async with app.run_test() as pilot:
        await seed(pilot, app, "one")
        app.cfg["saved"] = [{"name": "yes", "text": "yes please"}]

        await pilot.press("tab")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        app.sp.q.join()

        assert app.spoken == ["yes please"]


async def test_ctrl_x_deletes_from_the_saved_list(app):
    """The focused filter Input claims ctrl+x for "cut" without the override."""
    async with app.run_test() as pilot:
        app.cfg["saved"] = [{"name": "a", "text": "a"}, {"name": "b", "text": "b"}]

        await pilot.press("tab")
        await pilot.pause()
        await pilot.press("ctrl+x")
        await pilot.pause()

        assert [s["name"] for s in app.cfg["saved"]] == ["b"]


async def test_arrows_move_the_picker_not_the_transcript(app):
    """An app-level priority binding would steal these from the open picker."""
    async with app.run_test() as pilot:
        await seed(pilot, app, "one", "two", "three")
        app.cfg["saved"] = [{"name": "a", "text": "a"},
                            {"name": "b", "text": "b"},
                            {"name": "c", "text": "c"}]

        await pilot.press("tab")
        await pilot.pause()

        await pilot.press("down")
        await pilot.pause()
        assert app.screen.query_one("#picker-list").highlighted == 1
        assert app.sel is None              # the transcript must not have moved

        await pilot.press("up", "up")       # wraps within the picker
        await pilot.pause()
        assert app.screen.query_one("#picker-list").highlighted == 2


async def test_escape_closes_a_picker_without_choosing(app):
    async with app.run_test() as pilot:
        await seed(pilot, app, "one")
        app.cfg["saved"] = [{"name": "yes", "text": "yes please"}]

        await pilot.press("tab")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

        assert not isinstance(app.screen, Picker)
        assert app.spoken == []


async def test_typing_in_a_picker_filters_it(app, monkeypatch):
    monkeypatch.setattr(core, "voice_names", lambda: [
        ("Albert  en_US", "Albert"),
        ("Alva  sv_SE", "Alva"),
        ("Daniel  en_GB", "Daniel"),
    ])

    async with app.run_test() as pilot:
        await pilot.press("ctrl+v")
        await pilot.pause()

        await pilot.press("d", "a", "n")
        await pilot.pause()

        assert [app.screen.labels[i] for i, _ in app.screen.hits] == ["Daniel  en_GB"]

        await pilot.press("enter")
        await pilot.pause()

        assert app.cfg["voice"] == "Daniel"     # the value, not the shown label


@pytest.mark.parametrize("chord", ["f1", "ctrl+g"])
async def test_a_chord_opens_the_key_list_without_typing(app, chord):
    async with app.run_test() as pilot:
        await pilot.press(chord)
        await pilot.pause()

        assert isinstance(app.screen, Help)


async def test_backspace_still_edits_the_prompt(app):
    """ctrl+h is deliberately unbound: textual reports it as `backspace`, so
    binding it to help would eat this."""
    async with app.run_test() as pilot:
        prompt = app.query_one("#prompt", Input)
        prompt.value = "abc"
        prompt.cursor_position = 3

        await pilot.press("backspace")

        assert prompt.value == "ab"
        assert not isinstance(app.screen, Help)


async def test_a_stray_key_does_not_close_the_key_list(app):
    """It closed on ANY key, so the modifiers of a cmd+shift+4 screenshot
    dismissed it mid-capture."""
    async with app.run_test() as pilot:
        await pilot.press("f1")
        await pilot.pause()

        await pilot.press("a", "4", "z")
        await pilot.pause()
        assert isinstance(app.screen, Help)

        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, Help)


async def test_voice_picker_can_return_to_the_system_default(app, monkeypatch):
    """The default is a System Voice neither API enumerates, so the list has to
    offer an explicit way back to it."""
    monkeypatch.setattr(core, "voice_names", lambda: [
        ("(system default)", None),
        ("Daniel  en_GB", "Daniel"),
    ])

    async with app.run_test() as pilot:
        app.cfg["voice"] = "Daniel"

        await pilot.press("ctrl+v")
        await pilot.pause()
        await pilot.press("enter")          # first row
        await pilot.pause()

        assert app.cfg["voice"] is None


# --- editing and deleting history ---

async def test_ctrl_r_loads_the_picked_line_for_editing(app):
    async with app.run_test() as pilot:
        await seed(pilot, app, "one", "twe", "three")

        await pilot.press("up", "up")           # line 2
        await pilot.press("ctrl+r")

        assert app.query_one("#prompt", Input).value == "twe"
        assert app.editing == ("line", 1)
        assert "replace line 2" in hint(app)


async def test_submitting_an_edit_replaces_the_line_without_saying_it(app):
    async with app.run_test() as pilot:
        await seed(pilot, app, "one", "twe", "three")

        await pilot.press("up", "up")
        await pilot.press("ctrl+r")
        app.query_one("#prompt", Input).value = "two"
        await pilot.press("enter")
        app.sp.q.join()

        assert app.lines == ["one", "two", "three"]     # replaced, not appended
        assert core.load_transcript() == ["one", "two", "three"]
        assert app.spoken == []                         # fixing the record, not saying it
        assert app.editing is None


async def test_an_edited_line_stays_picked_so_enter_can_say_it(app):
    async with app.run_test() as pilot:
        await seed(pilot, app, "one", "twe")

        await pilot.press("up")
        await pilot.press("ctrl+r")
        app.query_one("#prompt", Input).value = "two"
        await pilot.press("enter")               # replaces, silently
        assert app.sel == 1

        await pilot.press("enter")               # now say it
        app.sp.q.join()
        assert app.spoken == ["two"]


async def test_escape_abandons_an_edit_and_keeps_the_line(app):
    async with app.run_test() as pilot:
        await seed(pilot, app, "one", "twe")

        await pilot.press("up")
        await pilot.press("ctrl+r")
        app.query_one("#prompt", Input).value = "rewritten"
        await pilot.press("escape")

        assert app.lines == ["one", "twe"]
        assert app.editing is None
        assert app.query_one("#prompt", Input).value == ""


async def test_ctrl_x_deletes_one_line_not_the_transcript(app):
    async with app.run_test() as pilot:
        await seed(pilot, app, "one", "two", "three")

        await pilot.press("up", "up")           # line 2
        await pilot.press("ctrl+x")

        assert app.lines == ["one", "three"]
        assert core.load_transcript() == ["one", "three"]


async def test_deleting_the_last_line_steps_the_selection_back(app):
    async with app.run_test() as pilot:
        await seed(pilot, app, "one", "two")

        await pilot.press("up")                 # newest, index 1
        await pilot.press("ctrl+x")
        assert app.sel == 0

        await pilot.press("ctrl+x")
        assert app.lines == []
        assert app.sel is None                  # nothing left to point at


async def test_the_line_keys_need_a_picked_line(app):
    async with app.run_test() as pilot:
        await seed(pilot, app, "one")

        await pilot.press("ctrl+x")
        assert app.lines == ["one"]
        assert "pick a line" in hint(app)


async def test_the_hint_bar_follows_the_selection(app):
    async with app.run_test() as pilot:
        await seed(pilot, app, "one")

        assert "^D quit" in hint(app)
        await pilot.press("up")
        assert "^R edit" in hint(app) and "^X delete" in hint(app)
        await pilot.press("escape")
        assert "^D quit" in hint(app)


async def test_ctrl_r_in_the_saved_list_edits_that_phrase(app):
    """The transcript had ^R and the saved list did not, so a typo in a saved
    phrase could only be fixed by deleting and retyping it."""
    async with app.run_test() as pilot:
        app.cfg["saved"] = [{"name": "yes plaese", "text": "yes plaese"}]

        await pilot.press("tab")
        await pilot.pause()
        await pilot.press("ctrl+r")
        await pilot.pause()

        assert not isinstance(app.screen, Picker)       # back on the prompt
        assert app.query_one("#prompt", Input).value == "yes plaese"
        assert app.editing == ("saved", 0)
        assert "saved phrase 1" in hint(app)


async def test_rewriting_a_saved_phrase_updates_it_without_saying_it(app):
    async with app.run_test() as pilot:
        app.cfg["saved"] = [{"name": "yes plaese", "text": "yes plaese"}]

        await pilot.press("tab")
        await pilot.pause()
        await pilot.press("ctrl+r")
        await pilot.pause()

        app.query_one("#prompt", Input).value = "yes please"
        await pilot.press("enter")
        app.sp.q.join()

        assert app.cfg["saved"] == [{"name": "yes please", "text": "yes please"}]
        assert app.spoken == []                         # fixing it, not saying it
        assert app.lines == []                          # and not a new transcript line
        assert app.editing is None


async def test_escape_abandons_a_saved_phrase_edit(app):
    async with app.run_test() as pilot:
        app.cfg["saved"] = [{"name": "keep me", "text": "keep me"}]

        await pilot.press("tab")
        await pilot.pause()
        await pilot.press("ctrl+r")
        await pilot.pause()

        app.query_one("#prompt", Input).value = "clobbered"
        await pilot.press("escape")

        assert app.cfg["saved"] == [{"name": "keep me", "text": "keep me"}]
        assert app.editing is None


# --- settings ---------------------------------------------------------------

async def test_slash_command_sets_the_rate(app):
    async with app.run_test() as pilot:
        app.query_one("#prompt", Input).value = "/rate 220"
        await pilot.press("enter")

        assert app.cfg["rate"] == 220
        assert hint(app) == "rate = 220"


async def test_slash_clear_empties_the_transcript(app):
    async with app.run_test() as pilot:
        await seed(pilot, app, "one", "two")

        app.query_one("#prompt", Input).value = "/clear"
        await pilot.press("enter")

        assert app.lines == []
        assert core.load_transcript() == []
