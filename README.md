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

## Keys and commands

There are two spellings, and one rule: **a chord is immediate; a `/command`
takes a typed value, or is destructive enough to be worth typing.** `^G` or
`F1` shows this list in the app.

| key | |
|---|---|
| type + `⏎` | speak it — queued, so carry on typing |
| `↑` `↓` | pick a past line, wrapping at both ends |
| `⏎` | say the picked line again |
| `Esc` | unpick |
| `!3` | say line 3; `!` alone repeats the last |
| `_word_` `*word*` | emphasise — needs `/backend av` |
| `^R` | edit the picked line in place; `⏎` saves it without saying it |
| `^X` | delete the picked line |
| `⇥` | saved phrases — type to filter, `^X` deletes |
| `^V` | voice — type to filter |
| `^S` | save the typed, picked, or last-said line |
| `^C` | stop talking and drop the queue |
| `^Q` `^D` | quit |
| `\` | speak a literal leading `/` or `!` |
| `^G` `F1` | this list (`/help` works too) |

| command | |
|---|---|
| `/voice <name>` | set by name; bare `/voice` restores the default |
| `/rate <wpm>` | e.g. `/rate 200`; bare `/rate` restores 175 |
| `/backend say\|av` | `av` = emphasis, via SSML pitch; bare restores `say` |
| `/clear` | empty the transcript (destructive, so typed) |

Quitting is a chord only — `/quit` was the one command that duplicated one.
Help is a chord for the same reason: it takes no value. `^H` would have been
the obvious key, but textual reports it as `backspace` — identical to the
backspace key — so binding it would break editing the prompt.

## The default voice has no name

With no voice set, `say` uses the **System Voice** from Settings →
Accessibility → Spoken Content. If that is a Siri voice, neither `say -v ?`
nor `AVSpeechSynthesisVoice.speechVoices()` lists it: rendering the same
sentence through all 184 named voices matched none of them. So the status bar
says `default` rather than a name, and the voice picker carries an explicit
`(system default)` row — without it, choosing a voice would be a one-way door.

Two related traps found while measuring this:

- `say -v <name-that-does-not-exist>` **exits 0** and silently substitutes a
  fallback voice. A typo is silent, not an error.
- Several familiar names (`Alex`, `Zoe`, `Samantha`) render byte-identically
  here, because only one of them is installed and the others fall back to it.

State lives in `~/.config/speak/config.json` (voice, rate, backend, saved
phrases) and `~/.local/share/speak/transcript` (the last 500 lines, reloaded
at launch).

## Two backends

A backend is just "text → argv".

- **`say`** — `/usr/bin/say`. No setup, every system voice, and a trained
  Personal Voice once it is granted.
- **`av`** — a small Swift helper (`av_speak.swift`, compiled on first use and
  cached in `~/.cache/speak/`) driving `AVSpeechSynthesizer`. It exists for
  SSML, which `say` cannot speak and which is the only route to pitch.

Personal Voice is **not** exclusive to `av`: once trained and granted it
appears in `say -v ?` and `say -v "<name>"` really speaks it — verified
against the fallback voice, since a wrong name would exit 0 and sound
plausible. `av` is worth it only for the stronger emphasis.

## Emphasis, and what macOS actually honours

`_word_` or `*word*` emphasises. Getting that to be audible took measuring,
because most of the obvious levers do nothing at all. Comparing rendered
audio byte-for-byte on macOS 26:

**It only works on `/backend av`,** and that is the whole reason that backend
exists. Every lever `/usr/bin/say` has, measured:

| lever | result |
|---|---|
| `[[emph +]]`, `[[pbas]]`, `[[volm]]` | **byte-identical audio** — parsed and discarded |
| `[[slnc N]]` | honoured, but **N is ignored** — always a fixed ~410ms |
| `[[rate N]]` absolute, whole line | works |
| `[[rate N]]` around one word | **disrupts prosody instead of stressing** |
| `[[rate -25%]]` relative | compounds and **never restores** |

The single-word case is the interesting failure. `does [[rate 87]]this[[rate
175]] change anything` comes out **faster** (1.243s) than the plain line
(1.291s) — the rate changes perturb the phrase timing more than they lengthen
the word. An earlier build shipped this anyway, on the strength of one phrase
where the noise happened to land positive. Generalising from one sample.

The relative form is no better: a `-25%` / `+33%` pair came out *slower*
(4.43s) than applying no restore at all (3.89s).

So the `say` backend speaks the words and drops the markers, and the app says
so in the status line rather than pretending.

SSML, through `AVSpeechSynthesizer`, does work — with one trap:

| lever | result |
|---|---|
| `<emphasis level="strong">` | byte-identical — dropped, like `say`'s |
| `<prosody rate="0.75">`, `pitch="1.3">` | ignored — bare numbers don't work |
| `<prosody rate="75%" pitch="+30%">` | works |

Pitch is what makes it audible, and percent form is mandatory.

Unrelated but worth recording: **175 wpm is `say`'s default.** `say -r 175` is
byte-identical to no `-r` at all, on every voice tried including a personal
one. Short samples cannot show this — some voices return identical audio for
`-r 160`, `175` and `180`, so a 6-word phrase interpolates to a wrong answer.

## Personal Voice

Needs `/backend av`. Setup, in order:

1. Create one in **Settings → Accessibility → Personal Voice** (~15 minutes of
   reading phrases aloud).
2. Turn on **Allow applications to use your Personal Voice** in that same pane.
3. **Click into the voice's own row and press "Start training…".** This is the
   step that is easy to miss: training does *not* start on its own, and the
   outer row says "Recording complete" the whole time it is waiting for you.
   Once pressed it shows "Preparing", and generation runs on-device — leave the
   Mac on power; it takes hours, not minutes.
4. Check whether the asset has landed:

   ```sh
   ~/.cache/speak/av_speak --list | grep personal
   ```

Once a row appears there, `^V` lists it marked `★ personal`. The `av` backend
works with ordinary system voices regardless, so nothing is gated on this.

### Diagnosing it

The two states look identical from the outside, so check which one you are in:

| symptom | meaning |
|---|---|
| `av_speak --list` prints a "denied" note | no app grant yet — step 2 |
| exits clean, but no `personal` row | not trained, or still preparing — step 3 |
| `~/Library/Group Containers/group.com.apple.accessibility.voicebanking/` empty | the asset has not been generated on this Mac |

The app grant is not tied to the terminal that asked for it: the request gets
attributed to whichever app is in the foreground (a bare CLI has no bundle
identity of its own), but the resulting authorization reads back as
`authorized` from any shell.

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
