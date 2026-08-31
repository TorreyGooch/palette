"""Source files must be clean UTF-8.

A PowerShell read/write round-trip re-encoded UTF-8 as the ANSI codepage,
double-encoding every em dash and box-drawing character in cut.py and
pull.py. Because the clip title is built from a literal, the damage reached
the UI: staged clips were titled with mojibake. Cheap to check, and easy to
reintroduce from any editor or script that guesses an encoding.
"""
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "Library", "node_modules"}

# UTF-8 decoded as cp1252 and written back out leaves a telltale lead
# character: 0xE2 ahead of the e2-80-xx punctuation range (em/en dash, smart
# quotes, ellipsis) and 0xC3 ahead of accented latin-1. Built from code
# points rather than spelled out, so this file does not match its own check.
_E2 = chr(0xE2)
_C3 = chr(0xC3)
MOJIBAKE = (
    _E2 + chr(0x20AC),   # e2 80 -> lead + euro sign
    _E2 + chr(0x0080),   # e2 80 where cp1252 has no glyph
    _C3 + chr(0xA9),     # c3 a9
    _C3 + chr(0xA8),     # c3 a8
)

# Both YAML spellings: the workflow files are `.yml`, and a list that
# covered only `.yaml` quietly exempted the whole of .github/ from this.
PATTERNS = ("*.py", "*.md", "*.bat", "*.txt", "*.yaml", "*.yml",
            "*.html", "*.js", "*.css")


def source_files():
    for pattern in PATTERNS:
        for path in REPO.rglob(pattern):
            if SKIP_DIRS & set(path.parts):
                continue
            yield path


@pytest.mark.parametrize("path", sorted(source_files(), key=str),
                         ids=lambda p: str(p.relative_to(REPO)))
def test_file_is_clean_utf8(path):
    raw = path.read_bytes()

    assert not raw.startswith(b"\xef\xbb\xbf"), (
        f"{path.relative_to(REPO)} starts with a UTF-8 BOM, usually a sign an "
        f"editor or script rewrote it with a guessed encoding")

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        pytest.fail(f"{path.relative_to(REPO)} is not valid UTF-8: {e}")

    found = [ascii(m) for m in MOJIBAKE if m in text]
    assert not found, (
        f"{path.relative_to(REPO)} contains double-encoded UTF-8 {found}. "
        f"Restore it from git rather than editing in place: the damage is "
        f"lossy and cannot be transformed back.")
