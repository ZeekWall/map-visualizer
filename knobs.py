"""Derives an editable knob list straight from config.py's source, instead of
hand-maintaining a schema that would drift from the file it describes.

scan() walks config.py's AST and picks up every top-level literal assignment
(single-name or a tuple-unpacking line like `OUT_W, OUT_H = 1440, 2560`).
That rule does exactly the right thing on its own: `retarget` is a
FunctionDef so it's skipped entirely, and the globals it fills in
(PLACE_NAME, REGION_EXTENT, HOOK_LINES, ...) only ever get assigned inside
that function body via `global`, never as a module-level Assign -- so they
never show up as knobs. TARGET/REGION/PLACE_SOURCE/REGION_PAD_DEG and every
knob in the "how it looks" section are ordinary top-level literals and are
picked up automatically. The knob list can't drift from config.py because
it IS config.py, re-read.

write_value() edits exactly one source line (found via the AST node's
lineno), preserving everything else on it -- sibling values on a
tuple-unpacking line, and the trailing inline comment -- then validates the
result by importing config.py in a clean subprocess before committing.
Rejected edits leave config.py untouched.
"""

import ast
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field

CONFIG_PATH = "config.py"

# Matches a section header comment, e.g.
#   # ---------------------------------------------------------------- video
#   # ================================================================== target
_SECTION_RE = re.compile(r"^#\s*[-=]{4,}\s*(.+?)\s*$")


@dataclass
class Knob:
    name: str
    value: object
    kind: str                  # bool | int | float | str | none | color | list | other
    lineno: int
    line_names: list = field(default_factory=list)  # all names sharing this source line, in order
    comment: str = ""          # trailing inline comment, if any
    help: str = None           # preceding explanatory comment block, if any
    section: str = None        # nearest section header above it


def _infer_kind(value):
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if value is None:
        return "none"
    if isinstance(value, str):
        return "str"
    if (isinstance(value, tuple) and len(value) == 3
            and all(isinstance(v, int) and 0 <= v <= 255 for v in value)):
        return "color"
    if isinstance(value, (list, tuple)):
        return "list"
    return "other"


def _preceding_context(lines, lineno):
    """Walks upward from the line above `lineno` (1-indexed) through
    contiguous blank/comment lines. Returns (section, help_text): the
    nearest `# ---- header` found, and any other comment lines between the
    header (or code above it) and the knob, joined as free-text help."""
    section = None
    help_lines = []
    i = lineno - 2  # 0-indexed line just above the knob's own line
    while i >= 0:
        s = lines[i].strip()
        if s == "":
            i -= 1
            continue
        if s.startswith("#"):
            m = _SECTION_RE.match(s)
            if m:
                section = m.group(1)
                break
            help_lines.append(s.lstrip("#").strip())
            i -= 1
            continue
        break
    help_lines.reverse()
    return section, " ".join(help_lines) if help_lines else None


def scan(path=CONFIG_PATH):
    """Every editable knob in config.py, in source order."""
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    lines = src.splitlines()
    tree = ast.parse(src, path)

    knobs = []
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        tgt = node.targets[0]
        if isinstance(tgt, ast.Name):
            names = [tgt.id]
        elif isinstance(tgt, ast.Tuple) and all(isinstance(e, ast.Name) for e in tgt.elts):
            names = [e.id for e in tgt.elts]
        else:
            continue

        try:
            value = ast.literal_eval(node.value)
        except (ValueError, SyntaxError):
            continue  # not a plain literal (e.g. an f-string) -- not editable

        if len(names) == 1:
            values = [value]
        else:
            if not isinstance(value, tuple) or len(value) != len(names):
                continue
            values = list(value)

        line = lines[node.lineno - 1]
        m = re.search(r"(\s*#.*)$", line)
        comment = m.group(1).lstrip().lstrip("#").strip() if m else ""
        section, help_text = _preceding_context(lines, node.lineno)

        for nm, val in zip(names, values):
            knobs.append(Knob(
                name=nm, value=val, kind=_infer_kind(val), lineno=node.lineno,
                line_names=names, comment=comment, help=help_text, section=section,
            ))
    return knobs


def grouped(path=CONFIG_PATH):
    """scan() results grouped by section, preserving source order. Returns a
    list of (section_name_or_None, [Knob, ...]).

    Only the first knob directly under a `# ---- header` actually carries
    that section (_preceding_context only finds a header when it's the
    nearest comment above -- later knobs in the same section have a real
    code line, not a header, immediately above them and so report
    section=None). Those join whatever bucket is already open rather than
    starting a new one; only a *new, non-None* section closes the previous
    bucket.
    """
    out = []
    bucket = None
    last_section = object()  # sentinel, never equal to a real section or None
    for k in scan(path):
        if k.section is not None and k.section != last_section:
            bucket = []
            out.append((k.section, bucket))
            last_section = k.section
        elif bucket is None:
            bucket = []
            out.append((None, bucket))
        bucket.append(k)
    return out


def _validate_kind(kind, value):
    """`import config` succeeding only proves the file still parses -- it
    doesn't catch a type mismatch, since config.py has no runtime
    assertions (BG is just a tuple some other module happens to expect a
    3-tuple of 0-255 ints; assigning it a string imports fine and breaks
    silently, later, inside basemap.py). Check the shape the scanner
    inferred from the *current* value instead, before ever touching disk."""
    if kind == "bool" and not isinstance(value, bool):
        raise ValueError(f"expected a bool, got {value!r}")
    if kind == "int" and (isinstance(value, bool) or not isinstance(value, int)):
        raise ValueError(f"expected an int, got {value!r}")
    if kind == "float" and (isinstance(value, bool) or not isinstance(value, (int, float))):
        raise ValueError(f"expected a number, got {value!r}")
    if kind == "str" and not isinstance(value, str):
        raise ValueError(f"expected a string, got {value!r}")
    if kind == "color":
        if not (isinstance(value, tuple) and len(value) == 3
                and all(isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 255
                       for v in value)):
            raise ValueError(f"expected a 3-tuple of 0-255 ints, got {value!r}")


def write_value(name, new_value, path=CONFIG_PATH):
    """Update one knob in place. Rewrites only its source line -- sibling
    values on a shared tuple-unpacking line and the trailing comment are
    preserved verbatim. Validates the new value's shape against the knob's
    inferred kind, then backs up to `<path>.bak`, writes, and validates
    again by importing config.py in a clean subprocess; on either failure
    the file is left untouched (nothing is written at all for a kind
    mismatch; the backup is restored for an import failure) and a
    ValueError is raised.
    """
    knobs = scan(path)
    match = next((k for k in knobs if k.name == name), None)
    if match is None:
        raise ValueError(f"unknown knob {name!r}")
    _validate_kind(match.kind, new_value)

    with open(path, "r", encoding="utf-8") as f:
        raw_lines = f.readlines()  # keep original line endings

    old_line = raw_lines[match.lineno - 1]
    if len(match.line_names) == 1:
        target_src = match.line_names[0]
        value_src = repr(new_value)
    else:
        sibling_values = []
        for nm in match.line_names:
            sibling_values.append(new_value if nm == name
                                  else next(k.value for k in knobs if k.name == nm))
        target_src = ", ".join(match.line_names)
        value_src = ", ".join(repr(v) for v in sibling_values)

    m = re.search(r"(\s*#.*)$", old_line.rstrip("\n"))
    comment_src = m.group(1) if m else ""
    eol = "\n" if old_line.endswith("\n") else ""
    new_line = f"{target_src} = {value_src}{comment_src}{eol}"

    backup_path = path + ".bak"
    shutil.copyfile(path, backup_path)
    try:
        new_lines = list(raw_lines)
        new_lines[match.lineno - 1] = new_line
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)

        result = subprocess.run(
            [sys.executable, "-c", "import config"],
            cwd=os.path.dirname(os.path.abspath(path)) or ".",
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            shutil.copyfile(backup_path, path)
            raise ValueError(
                f"invalid value for {name} ({value_src}) -- config.py failed "
                f"to import, change rolled back:\n{result.stderr.strip()}")
    finally:
        if os.path.exists(backup_path):
            os.remove(backup_path)

    return True
