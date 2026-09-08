# speak

Type a line, press Enter, and keep typing while macOS talks. A full-screen
front end for `say`, for when typing is faster than speaking.

```
┌──────────────────────────────────────────────────────────┐
│ speak                     Daniel · 200wpm · speaking +2 │
│    1 hello there                                         │
│    2 how are you doing                                   │
│    3 i'm doing fine, thanks                              │
│    4 ^R edits one, ^X deletes one                        │
│ > what i'm typing now                                    │
│ ↑↓ pick · ⏎ speak · !3 redo · ⇥ saved · ^V voice · /help │
└──────────────────────────────────────────────────────────┘
```

Lines queue through one worker thread, so you can type ahead and they come out
in order. Nothing waits for the speech to finish.

## Install

macOS only — it shells out to `say`.

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
| `^R` | edit the picked line, or a saved phrase; `⏎` saves it silently |
| `^X` | delete the picked line |
| `⇥` | saved phrases — type to filter, `^R` edits, `^X` deletes |
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

## What `say` will and will not do

Two traps worth knowing, both measured:

- `say -v <name-that-does-not-exist>` **exits 0** and silently substitutes a
  fallback voice. A typo is invisible, not an error. (This is why a voice left
  behind by an older version of this tool is discarded on load — it stored
  identifiers, which `say` cannot use.)
- **175 wpm is the default.** `say -r 175` is byte-identical to no `-r` at all,
  on every voice tried including a trained personal one. Short samples cannot
  show this: some voices return identical audio for `-r 160`, `175` and `180`,
  so a 6-word phrase interpolates to a wrong answer.

### Emphasis was tried, and cut

An earlier version marked words with `_word_` / `*word*`. It is gone, because
nothing macOS offers actually stresses one word:

| lever | result |
|---|---|
| `say` `[[emph +]]`, `[[pbas]]`, `[[volm]]` | **byte-identical audio** — parsed and discarded |
| `say` `[[slnc N]]` | honoured, but **N is ignored** — always a fixed ~410ms |
| `say` `[[rate N]]` around one word | **faster** than the plain line (1.243s vs 1.291s) |
| SSML `<emphasis level="strong">` | byte-identical — dropped |
| SSML `<prosody pitch="+30%">` | audibly different, but reads as a glitch, not stress |

The last row is the honest reason. Overriding one word's pitch fights the
contour the synthesiser is already applying to the phrase, so it sounds wrong
rather than emphatic. Cutting it removed a Swift helper, a second speech
backend, a compile-on-first-run step, and two incompatible voice-naming
schemes — for a feature that never worked.

Leftover markers are harmless: `say` ignores `_` and `*` outright, so an old
habit costs nothing but the characters on screen.

## Personal Voice

Setup, in order:

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
   say -v '?' | grep -i personal
   ```

Once trained and granted it appears in `say -v ?` like any other voice, so
`^V` lists it and nothing else is needed.

### Diagnosing it

The two states look identical from the outside, so check which one you are in:

| symptom | meaning |
|---|---|
| the voice never appears in `say -v ?` | not granted, not trained, or still preparing |
| `~/Library/Group Containers/group.com.apple.accessibility.voicebanking/` empty | the asset has not been generated on this Mac |

The app grant is attributed to whichever app is in the foreground when it is
requested (a bare CLI has no bundle identity of its own), but it applies
everywhere once given.

## Layout

```
src/speak/core.py    speech, config, voice parsing, fuzzy matching — no UI import
src/speak/app.py     the textual app
tests/test_core.py   headless
tests/test_app.py    driven through textual's pilot
```

`core.py` deliberately imports no UI, so the logic is testable without a
terminal:

```sh
uv run pytest
```

## Licence

MIT
