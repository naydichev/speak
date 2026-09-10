"""Regenerate the README screenshots.

    uv run python docs/screenshots.py

Drives the real app through textual's pilot and exports its own SVG, so the
images cannot drift from the interface the way a hand-pasted capture does.
The transcript and saved phrases below are invented; nothing touches the real
config, and no audio is produced.
"""
import asyncio, pathlib, sys

sys.path.insert(0, "src")
from speak import core
from speak.app import Speak

OUT = pathlib.Path("docs")
LINES = [
    "hey, can you hear me?",
    "i'll be about ten minutes",
    "no, the second one",
    "was ist das",
    "thanks, that's really kind of you",
]
SAVED = ["yes please", "no thanks", "one moment", "could you repeat that?"]


class FakeProc:
    returncode = 0
    def communicate(self): return b"", b""
    def kill(self): pass


async def shoot(name, keys=(), pending=False):
    app = Speak(run=lambda argv, **kw: FakeProc())
    app.lines = list(LINES)
    app.cfg.update(voice="Daniel", rate=200,
                   saved=[{"name": s, "text": s} for s in SAVED])

    async with app.run_test(size=(84, 22)) as pilot:
        app.repopulate()
        if pending:                       # make the status bar show a queue
            for line in LINES[:3]:
                app.sp.q.put(line)
        app.show_status()
        await pilot.pause()

        for k in keys:
            await pilot.press(k)
            await pilot.pause()

        await pilot.pause()
        (OUT / name).write_text(app.export_screenshot(title="speak"))
        print(f"  docs/{name}")

    app.sp.stop()


async def main():
    core.CONFIG = "/dev/null"             # never touch the real config
    core.TRANSCRIPT = "/dev/null"
    await shoot("screenshot.svg", pending=True)
    await shoot("screenshot-saved.svg", ["tab"])
    await shoot("screenshot-voices.svg", ["ctrl+v", "d", "a", "n", "i", "e", "l"])

asyncio.run(main())
