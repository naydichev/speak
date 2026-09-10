# Discoveries about `say`

Things measured while building [speak](../README.md), none of them documented
anywhere obvious. Every claim here is a byte comparison of rendered audio, not
an impression — the commit that introduced each one shows the measurement.

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
