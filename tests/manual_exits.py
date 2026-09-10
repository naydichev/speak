"""For each way of ending the app, did it turn mouse tracking back off?

Not a pytest: it needs a real pty and real signals. Run it from the project
root after touching main() or anything about startup:

    python3 tests/manual_exits.py

A dirty exit is not cosmetic. The terminal keeps reporting mouse motion to a
shell that echoes it, so the screen fills with fragments like `M35;2262;-3M`
over the dead app's last frame — which reads as the app corrupting itself.
SIGHUP is the one to watch: it is what a closing terminal window sends.
"""
import fcntl, os, pty, select, signal, struct, termios, time

def run(label, finish):
    pid, fd = pty.fork()
    if pid == 0:
        os.environ.update(TERM="xterm-256color")
        os.execv(".venv/bin/speak", [".venv/bin/speak"])   # from the project root

    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    out = b""

    def pump(sec):
        nonlocal out
        end = time.time() + sec
        while time.time() < end:
            if select.select([fd], [], [], 0.05)[0]:
                try: out += os.read(fd, 65536)
                except OSError: return

    pump(3.0)
    on = [s for s in (b"?1000h", b"?1003h", b"?1006h") if s in out]
    mark = len(out)

    finish(pid, fd)
    pump(2.0)
    tail = out[mark:]
    off = [s for s in (b"?1000l", b"?1003l", b"?1006l") if s in tail]

    try: os.close(fd)
    except OSError: pass
    try: os.kill(pid, signal.SIGKILL); os.waitpid(pid, 0)
    except (ProcessLookupError, ChildProcessError): pass

    ok = bool(off) or not on
    print(f"  {label:26} enabled {len(on)}  disabled {len(off)}   {'clean' if ok else 'DIRTY'}")
    return ok

results = [
    run("^Q",            lambda pid, fd: os.write(fd, b"\x11")),
    run("^D",            lambda pid, fd: os.write(fd, b"\x04")),
    run("SIGTERM (kill)", lambda pid, fd: os.kill(pid, signal.SIGTERM)),
    run("SIGHUP (close window)", lambda pid, fd: os.kill(pid, signal.SIGHUP)),
    run("SIGINT", lambda pid, fd: os.kill(pid, signal.SIGINT)),
]
raise SystemExit(0 if all(results) else 1)
