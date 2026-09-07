"""Checks for everything in speak.core. No UI, so these run headless.

Each one was written against a mutation: change the line it guards and it
fails. The comments say what the mutation was, because several of these
assertions look arbitrary until you know which bug they caught.
"""

import pytest

from speak import core


class FakeProc:
    returncode = 0

    def communicate(self):
        return b"", b""

    def kill(self):
        pass


@pytest.fixture
def spoken():
    """A Speaker whose subprocess call records argv instead of talking.

    Recording the argument is the point: a double that ignored it would let
    a wrong voice or a mangled line pass.
    """
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return FakeProc()

    def make(**over):
        return core.Speaker({**core.DEFAULTS, **over}, run=run), calls

    return make


# --- the `say` argv -----------------------------------------------------------

def test_argv_carries_voice_and_rate(spoken):
    sp, _ = spoken(voice="Daniel", rate=220)
    assert sp.argv("hi") == ["say", "-v", "Daniel", "-r", "220", "--", "hi"]


def test_argv_separates_text_that_looks_like_a_flag(spoken):
    sp, _ = spoken(voice="Daniel", rate=220)
    assert sp.argv("-n") == ["say", "-v", "Daniel", "-r", "220", "--", "-n"]


def test_argv_stays_bare_without_settings(spoken):
    sp, _ = spoken()
    assert sp.argv("hi") == ["say", "--", "hi"]


# --- queue ------------------------------------------------------------------

def test_lines_speak_in_the_order_typed(spoken):
    sp, calls = spoken(voice="Daniel")

    for word in ["one", "two", "three"]:
        sp.say(word)
    sp.q.join()

    assert [c[-1] for c in calls] == ["one", "two", "three"]


def test_stop_drops_the_backlog(spoken):
    sp, calls = spoken()

    sp.q.put("never")
    sp.stop()
    sp.q.join()

    assert "never" not in [c[-1] for c in calls]


# --- voice lists ------------------------------------------------------------

def test_say_voice_names_split_on_the_trailing_locale():
    """`say -v ?` puts ONE space before the locale on a long name. Splitting on
    runs of spaces glued it on, and `say` then silently used a default voice."""
    out = ("Albert              en_US    # Hello! My name is Albert.\n"
           "Eddy (German (Germany)) de_DE    # Hallo! Ich heiße Eddy.\n"
           "Bad News            en_US    # Hello! My name is Bad News.\n")

    assert core.parse_say_voices(out) == [
        ("Albert  en_US", "Albert"),
        ("Eddy (German (Germany))  de_DE", "Eddy (German (Germany))"),
        ("Bad News  en_US", "Bad News"),
    ]


def test_a_voice_left_by_the_old_backend_is_discarded(paths):
    """It stored identifiers, which `say` cannot use — and `say` substitutes a
    default silently rather than failing, which is near-undiagnosable."""
    core.save({**core.DEFAULTS, "voice": "com.apple.speech.personalvoice.ABC"})
    assert core.load()["voice"] is None

    core.save({**core.DEFAULTS, "voice": "Eddy (U.S.)"})
    assert core.load()["voice"] == "Eddy (U.S.)"     # a real name with dots survives


def test_voice_list_offers_a_way_back_to_the_default(monkeypatch):
    """`say`'s no--v default is a System Voice that neither API enumerates, so
    without this row picking a voice is a one-way door."""
    monkeypatch.setattr(core.subprocess, "run", lambda *a, **k: type(
        "R", (), {"stdout": "Albert              en_US    # hi\n"})())

    voices = core.voice_names()

    assert voices[0] == ("(system default)", None)
    assert voices[1] == ("Albert  en_US", "Albert")


# --- fuzzy matching ---------------------------------------------------------

NAMES = ["Daniel", "Alice", "Albert"]


def test_fuzzy_ranks_the_tightest_span_first():
    """"al" is adjacent in Alice/Albert but four apart in Daniel."""
    assert [NAMES[i] for i, _ in core.fuzzy(NAMES, "al")] == ["Alice", "Albert", "Daniel"]
    assert [NAMES[i] for i, _ in core.fuzzy(NAMES, "ae")][0] == "Albert"


def test_fuzzy_is_case_insensitive():
    assert core.fuzzy(NAMES, "AL") == core.fuzzy(NAMES, "al")


def test_fuzzy_returns_match_positions_for_underlining():
    assert core.fuzzy(NAMES, "dan") == [(0, (0, 1, 2))]


def test_fuzzy_edges():
    assert core.fuzzy(NAMES, "zz") == []
    assert [i for i, _ in core.fuzzy(NAMES, "")] == [0, 1, 2]   # empty keeps all


# --- !N recall --------------------------------------------------------------

@pytest.mark.parametrize("typed, index", [
    ("!1", 0),
    ("!3", 2),
    ("!", 2),                # bare ! repeats the last line
    ("!4", -1),              # out of range, reported not crashed
    ("!0", -1),
])
def test_bang_indexes_the_number_shown_on_the_row(typed, index):
    assert core.bang(typed, 3) == index


@pytest.mark.parametrize("typed", ["hello", "!3 please", "3", "!!"])
def test_bang_ignores_anything_that_is_not_a_bare_recall(typed):
    assert core.bang(typed, 3) is None


def test_bang_on_an_empty_transcript():
    assert core.bang("!1", 0) == -1
    assert core.bang("!", 0) == -1


def test_bang_round_trips_every_row():
    rows = ["first", "second", "third"]
    assert [rows[core.bang(f"!{i}", 3)] for i in (1, 2, 3)] == rows


# --- text reaches `say` untouched -------------------------------------------

@pytest.mark.parametrize("text", [
    "plain line",
    "say _this_ loud",              # markers are literal now, not markup
    "a < b & c",                    # nothing is escaped, there is no markup
    "2 * 3 = 6",
])
def test_the_line_is_spoken_exactly_as_typed(spoken, text):
    sp, _ = spoken()
    assert sp.argv(text)[-1] == text


# --- ^S target --------------------------------------------------------------

@pytest.mark.parametrize("typed, lines, sel, want", [
    ("typing", ["said"], 0, "typing"),          # what you're typing wins
    ("  ", ["a", "b", "c"], 1, "b"),            # then the picked line
    ("", ["a", "b", "c"], None, "c"),           # then the last said
    ("", [], None, ""),                         # nothing at all
])
def test_save_target_prefers_the_nearest_thing(typed, lines, sel, want):
    assert core.save_target(typed, lines, sel) == want


# --- settings commands ------------------------------------------------------

@pytest.fixture
def cfg(monkeypatch):
    """A config that never touches disk."""
    monkeypatch.setattr(core, "save", lambda cfg: None)
    return {**core.DEFAULTS, "voice": "Daniel", "rate": 220}


def test_command_sets_by_name(cfg):
    assert core.command("/voice Alice", cfg) == ("msg", "voice = Alice")
    assert cfg["voice"] == "Alice"


@pytest.mark.parametrize("word, default", [
    ("voice", None),
    ("rate", None),
])
def test_a_bare_command_restores_the_default(cfg, word, default):
    """No value resets it. The status bar already shows all three, so there is
    nothing for a read-back to add."""
    cfg.update(voice="Alice", rate=300)

    action, message = core.command(f"/{word}", cfg)

    assert action == "msg"
    assert cfg[word] == default
    assert "default" in message


def test_a_bare_rate_names_the_number_it_restores(cfg):
    assert core.command("/rate", cfg) == ("msg", "rate = default (175 wpm)")


def test_command_rejects_a_non_numeric_rate(cfg):
    assert "words per minute" in core.command("/rate fast", cfg)[1]
    assert cfg["rate"] == 220               # rejected, so left alone


@pytest.mark.parametrize("typed, action", [
    ("/help", "help"),
    ("/", "help"),
    ("/clear", "clear"),
])
def test_command_reports_the_action_for_the_ui(typed, action):
    assert core.command(typed, {**core.DEFAULTS})[0] == action


def test_command_names_what_it_did_not_understand(cfg):
    assert core.command("/nonsense", cfg)[1].startswith("unknown /nonsense")


# --- files ------------------------------------------------------------------

@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setattr(core, "TRANSCRIPT", str(tmp_path / "transcript"))
    return tmp_path


def test_config_round_trips(paths):
    core.save({**core.DEFAULTS, "voice": "Fred", "saved": [{"name": "a", "text": "b"}]})

    assert core.load()["voice"] == "Fred"
    assert core.load()["saved"] == [{"name": "a", "text": "b"}]


def test_transcript_appends_in_order(paths):
    core.append_transcript("first")
    core.append_transcript("second")

    assert core.load_transcript() == ["first", "second"]


def test_transcript_is_capped_on_disk(paths):
    """Counted from the raw file: load_transcript() slices too, and asserting
    through it hid an uncapped write."""
    core.trim_transcript([str(i) for i in range(core.KEEP + 50)])

    kept = (paths / "transcript").read_text().splitlines()
    assert len(kept) == core.KEEP
    assert kept[0] == "50"                  # oldest dropped


def test_load_never_aliases_the_module_defaults(paths):
    """A shallow copy shared one list, so a saved phrase leaked into DEFAULTS
    and into every later config."""
    first = core.load()
    first["saved"].append({"name": "leak", "text": "leak"})

    assert core.DEFAULTS["saved"] == []
    assert core.load()["saved"] == []


def test_missing_files_fall_back_instead_of_crashing(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "CONFIG", str(tmp_path / "nope" / "config.json"))
    monkeypatch.setattr(core, "TRANSCRIPT", str(tmp_path / "nope" / "transcript"))

    assert core.load() == core.DEFAULTS
    assert core.load_transcript() == []
