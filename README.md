# speak

Type a line, press Enter, and keep typing while macOS says it. A full-screen
front end for `say`, for when typing is faster than speaking.

```
┌──────────────────────────────────────────────────────────────┐
│ speak                          Daniel · 200wpm · speaking +2 │
│    1 hello there                                             │
│    2 how are you doing                                       │
│    3 i'm doing fine, thanks                                  │
│    4 was ist das                                             │
│ ──────────────────────────────────────────────────────────── │
│ > what i'm typing now                                        │
│ ⏎ speak · !3 redo · ⇥ saved · ^V voice · ^C stop · ^G keys   │
└──────────────────────────────────────────────────────────────┘
```

Lines queue through one worker thread, so you can type ahead and they come out
in order — nothing waits for the speech to finish. Past lines stay on screen
to say again, edit, or save as a phrase.

macOS only: it shells out to `say`.

## Status

A personal tool, built for one person and tested on one Mac. It is also vibe
coded — written end to end in conversation with Claude.

What that did and did not cost is worth stating, because the failure mode of
vibe coding is confident wrong answers, and this hit it repeatedly. An
emphasis feature shipped twice on measurements that turned out to be a single
lucky sample, and was cut entirely once every lever was actually measured. So
the rule here became: every non-obvious constant cites the audio comparison
that justifies it, in the comment beside it and in the commit that introduced
it. `git log` is the evidence, and the *Notes on `say`* section below is what
survived being checked.

Issues and pull requests are best-effort.

## Install

```sh
uv tool install --editable .
speak
```

`--editable` keeps the code in this checkout, so edits apply on next launch.
Adding a dependency later needs `uv tool upgrade speak`.

## Keys and commands

Two spellings, one rule: **a chord is immediate; a `/command` takes a typed
value, or is destructive enough to be worth typing.** `^G` or `F1` shows this
list in the app.

| key | |
|---|---|
| type + `⏎` | speak it — queued, so carry on typing |
| `↑` `↓` | pick a past line, wrapping at both ends |
| `⏎` | say the picked line again |
| `Esc` | unpick |
| `!3` | say line 3; `!` alone repeats the last |
| `^R` | edit the picked line, or a saved phrase — saves silently |
| `^X` | delete the picked line |
| `⇥` | saved phrases — type to filter, `^R` edits, `^X` deletes |
| `^V` | voice — type to filter |
| `^S` | save the typed, picked, or last-said line |
| `^C` | stop talking and drop the queue |
| `^Q` `^D` | quit |
| `\` | speak a literal leading `/` or `!` |
| `^G` `F1` | this list |

| command | |
|---|---|
| `/voice <name>` | set by name; bare `/voice` restores the default |
| `/rate <wpm>` | e.g. `/rate 200`; bare `/rate` restores 175 |
| `/clear` | empty the transcript (destructive, so typed) |

Quitting and help are chords only — neither takes a value, and `/quit`
duplicated `^Q`.

State lives in `~/.config/speak/config.json` (voice, rate, saved phrases) and
`~/.local/share/speak/transcript` (the last 500 lines, reloaded at launch).

## Personal Voice

A trained Personal Voice appears in `say -v ?` like any other voice, so `^V`
lists it and nothing else is needed. Getting it trained is the fiddly part:

1. Create one in **Settings → Accessibility → Personal Voice** (~15 minutes of
   reading phrases aloud).
2. Turn on **Allow applications to use your Personal Voice** in that pane.
3. **Open the voice's own row and press "Start training…".** Easy to miss:
   training does not begin on its own, and the outer row reads "Recording
   complete" the whole time it is waiting for you. It then shows "Preparing",
   and generation runs on-device — leave the Mac on power, it takes hours.
4. Check whether it has landed: `say -v '?' | grep -i personal`

The three failure states look identical from outside, so:

| symptom | meaning |
|---|---|
| never appears in `say -v ?` | not granted, not trained, or still preparing |
| `~/Library/Group Containers/group.com.apple.accessibility.voicebanking/` is empty | not generated on this Mac yet |

The app grant is attributed to whichever app is in the foreground when it is
requested — a bare CLI has no bundle identity of its own — but it applies
everywhere once given.

## Notes on `say`

Things measured while building this, none of them documented anywhere obvious.

**A wrong voice name is silent.** `say -v <name-that-does-not-exist>` exits 0
and substitutes a fallback voice. A typo is invisible rather than an error.
Voices left behind by an older version of this tool are discarded on load for
the same reason — it stored identifiers, which `say` cannot use.

**175 wpm is the default.** `say -r 175` is byte-identical to no `-r` at all,
on every voice tried including a trained personal one. Short samples cannot
show this: some voices return identical audio for `-r 160`, `175` and `180`,
so a 6-word phrase interpolates to a wrong answer.

**The default voice has no name.** With no `-v`, `say` uses the System Voice
from Settings → Accessibility → Spoken Content. If that is a Siri voice, `say
-v ?` does not list it — rendering one sentence through every listed voice
matched none of them. So the status bar says `default`, and the voice picker
carries an explicit `(system default)` row; without it, choosing a voice would
be a one-way door.

**Nothing stresses a single word.** Emphasis (`_word_`, `*word*`) was built and
cut. Every lever:

| lever | result |
|---|---|
| `[[emph +]]`, `[[pbas]]`, `[[volm]]` | **byte-identical audio** — parsed and discarded |
| `[[slnc N]]` | honoured, but **N is ignored** — always a fixed ~410ms |
| `[[rate N]]` around one word | **faster** than the plain line (1.243s vs 1.291s) |
| `[[rate -25%]]` relative | compounds and never restores |
| SSML `<emphasis level="strong">` | byte-identical — dropped |
| SSML `<prosody pitch="+30%">` | audibly different, but reads as a glitch |

The single-word `[[rate]]` case is the interesting failure: the rate changes
perturb phrase timing more than they lengthen the word. SSML pitch does change
the audio, but overriding one word fights the contour the synthesiser is
already applying, so it sounds wrong rather than emphatic. Cutting it removed a
Swift helper, a second speech backend, a compile-on-first-run step and two
incompatible voice-naming schemes.

Leftover markers are harmless: `say` ignores `_` and `*` outright.

## Development

```
src/speak/core.py         speech, config, voice parsing, fuzzy matching — no UI import
src/speak/app.py          the textual app
tests/test_core.py        headless
tests/test_app.py         driven through textual's pilot
tests/manual_exits.py     needs a real pty and signals; run by hand
```

`core.py` imports no UI, so the logic is testable without a terminal:

```sh
uv sync
uv run pytest
uv run speak       # without disturbing the installed copy
```

One check needs a real pty and real signals, so it is not a pytest:

```sh
python3 tests/manual_exits.py
```

It ends the app five ways (`^Q`, `^D`, SIGTERM, SIGHUP, SIGINT) and asserts
each turns mouse tracking back off. If any does not, the terminal keeps
reporting mouse motion to a shell that echoes it, and the screen fills with
fragments like `M35;2262;-3M` over the dead app's last frame — which reads as
the app corrupting itself. SIGHUP is the one to watch: it is what a closing
terminal window sends.

## Licence

MIT
