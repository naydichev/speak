"""Does a SIGTERM leave the terminal with mouse tracking still on?

Not a pytest: it needs a real pty and a real signal. Run it by hand from the
project root after touching main() or anything about startup:

    python3 tests/manual_sigterm.py

A dirty exit is not cosmetic — the shell then echoes raw mouse reports as
text over the dead screen, which looks like the app corrupting itself.
"""
import fcntl, os, pty, select, signal, struct, sys, termios, time

pid, fd = pty.fork()
if pid == 0:
    os.environ.update(TERM="xterm-256color")
    os.execv(".venv/bin/speak", [".venv/bin/speak"])   # from the project root

fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
out = b""

def pump(sec):
    global out
    end = time.time() + sec
    while time.time() < end:
        if select.select([fd], [], [], 0.05)[0]:
            try: out += os.read(fd, 65536)
            except OSError: return

pump(3.0)
enabled = [s for s in (b"?1000h", b"?1002h", b"?1003h", b"?1006h") if s in out]
mark = len(out)

os.kill(pid, signal.SIGTERM)
pump(2.0)
tail = out[mark:]
disabled = [s for s in (b"?1000l", b"?1002l", b"?1003l", b"?1006l") if s in tail]

os.close(fd)
try: os.waitpid(pid, 0)
except ChildProcessError: pass

print(f"  enabled at start:       {[s.decode() for s in enabled]}")
print(f"  disabled after SIGTERM: {[s.decode() for s in disabled]}")

if enabled and not disabled:
    raise SystemExit("FAIL: terminal left dirty — a kill will wreck the shell")

print("ok")
