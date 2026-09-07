# speak

Type a line, press Enter, and keep typing while macOS talks. A full-screen
front end for `say`, for when typing is faster than speaking.

```
┌──────────────────────────────────────────────────────────┐
│ speak                          Daniel · 200wpm · say · 2 │
│    1 hello there                                         │
│    2 how are you doing                                   │
│    3 i'm doing fine, thanks                              │
│    4 please be careful with that                         │
│ > what i'm typing now                                    │
│ ↑↓ pick · ⏎ speak · !3 redo · ⇥ saved · ^V voice · /help │
└──────────────────────────────────────────────────────────┘
```

Lines queue through one worker thread, so you can type ahead and they come out
in order. Nothing waits for the speech to finish.

## Install

macOS only — it shells out to `say`, and to `AVSpeechSynthesizer` for the rest.

```sh
uv sync
uv run speak
```

## Keys

| | |
|---|---|
| type + `⏎` | speak it — queued, so carry on typing |
| `↑` `↓` | pick a past line, wrapping at both ends |
| `⏎` | say the picked line again |
| `Esc` | unpick |
| `!3` | say line 3; `!` alone repeats the last |
| `_word_` `*word*` | emphasise |
| `⇥` | saved phrases — type to filter, `^X` deletes |
| `^V` | voice — type to filter |
| `^S` | save the typed, picked, or last-said line |
| `^C` | stop talking and drop the queue |
| `^Q` `^D` | quit |
| `\` | speak a literal leading `/` or `!` |
| `/voice` `/rate` | show or set, e.g. `/rate 200` |
| `/backend say\|av` | `av` = Personal Voice, and real emphasis |
| `/clear` | empty the transcript |

State lives in `~/.config/speak/config.json` (voice, rate, backend, saved
phrases) and `~/.local/share/speak/transcript` (the last 500 lines, reloaded
at launch).

## Two backends

A backend is just "text → argv".

- **`say`** — `/usr/bin/say`. No setup, every system voice.
- **`av`** — a small Swift helper (`av_speak.swift`, compiled on first use and
  cached in `~/.cache/speak/`) driving `AVSpeechSynthesizer`. Reaches two
  things `say` cannot: Personal Voice, and SSML.

## Emphasis, and what macOS actually honours

`_word_` or `*word*` emphasises. Getting that to be audible took measuring,
because most of the obvious levers do nothing at all. Comparing rendered
audio byte-for-byte on macOS 26:

| lever | result |
|---|---|
| `say` `[[emph +]]` | **byte-identical audio** — parsed and discarded |
| `say` `[[pbas]]`, `[[volm]]` | byte-identical |
| SSML `<emphasis level="strong">` | byte-identical |
| SSML `<prosody rate="0.75">`, `pitch="1.3">` | ignored — bare numbers don't work |
| SSML `<prosody rate="75%" pitch="+30%">` | works |
| `say` `[[rate N]]` absolute | works |
| `say` `[[rate -25%]]` relative | compounds and **never restores** |

That last one is worth knowing: a `-25%` / `+33%` pair came out *slower*
(4.43s) than applying no restore at all (3.89s).

So emphasis is faked from the two levers that survive:

- `say` gets absolute `[[rate]]` bracketing, which pins the whole line to one
  rate — measurably audible (1.147s → 1.253s) but weak, since rate is all it has.
- `av` gets scoped `<prosody>` pitch and rate, which is self-restoring and
  works for all 180 voices.

The four tuning numbers are named constants in `core.py` with the measurement
in the comment next to them.

## Personal Voice

Needs `/backend av`. Setup, in order:

1. Create one in **Settings → Accessibility → Personal Voice** (~15 minutes of
   reading phrases aloud).
2. Turn on **Allow applications to use your Personal Voice** in that same pane.
3. Leave the Mac **locked and on power** — macOS generates the voice on-device
   and it can take hours. "Recording complete" means the recording is done,
   not the voice.
4. Check whether the asset has landed:

   ```sh
   ~/.cache/speak/av_speak --list | grep personal
   ```

Once a row appears there, `^V` lists it marked `★ personal`. The `av` backend
works with ordinary system voices regardless, so nothing is gated on this.

## Layout

```
src/speak/core.py        backends, emphasis, voice parsing, fuzzy matching — no UI import
src/speak/app.py         the textual app
src/speak/av_speak.swift the AVSpeechSynthesizer helper
tests/test_core.py       headless
```

`core.py` deliberately imports no UI, so the logic is testable without a
terminal:

```sh
uv run pytest
```

## Licence

MIT
