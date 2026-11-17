"""Prints a fingerprint of the files that decide a build step, so build-engine.cmd can
skip work that would produce the same result.

    python buildhash.py engine   -> engine sources, run_engine.py, requirements, build script
    python buildhash.py tests    -> the above plus tests/
    python buildhash.py req      -> requirements.txt only (the venv's packages)
"""
import hashlib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def files(kind: str) -> list[str]:
    out = [os.path.join(HERE, "requirements.txt")]
    if kind == "req":
        return out
    out += [os.path.join(HERE, "run_engine.py"), os.path.join(HERE, "build-engine.cmd"),
            os.path.join(HERE, "version_info.txt")]
    roots = ["pdftailor_engine"] + (["tests"] if kind == "tests" else [])
    for root in roots:
        for d, dirs, names in os.walk(os.path.join(HERE, root)):
            dirs[:] = sorted(x for x in dirs if x != "__pycache__")
            out += [os.path.join(d, n) for n in sorted(names) if not n.endswith((".pyc", ".pyo"))]
    return out


def main() -> int:
    kind = sys.argv[1] if len(sys.argv) > 1 else "engine"
    h = hashlib.sha256(kind.encode())
    h.update(sys.version.encode())  # a new Python means a new build
    for p in files(kind):
        h.update(os.path.relpath(p, HERE).replace("\\", "/").encode())
        try:
            with open(p, "rb") as f:
                h.update(f.read().replace(b"\r\n", b"\n"))
        except OSError:
            h.update(b"<missing>")
    print(h.hexdigest())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
