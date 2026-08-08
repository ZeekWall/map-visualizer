"""Writes a new Target() entry into targets.py -- the other half of the
Explore tab's flow (explore.py finds the candidate, this commits it).

Same safety envelope as knobs.write_value() (see knobs.py): back up before
touching the file, splice in the new source text, re-validate by importing
in a clean subprocess, and roll back to the backup on any failure. The
difference from knobs.py is that this *adds* a dict entry rather than
rewriting one existing line, so the surgery is "insert before the closing
brace of TARGETS = {...}" instead of "replace line N".
"""

import ast
import os
import shutil
import subprocess
import sys

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "targets.py")


def slugify(name):
    """Best-effort slug for prefilling the Add form -- lowercase alnum only,
    matching the style of the hand-written keys ("heb", "chickfila")."""
    import re
    return re.sub(r"[^a-z0-9]", "", name.lower())


def render_target(slug, name, osm, atp_spider=None, atp_brands=None,
                  include_instore=False, osm_clauses=None):
    """Render one `"slug": Target(...)` source block, matching the style of
    the hand-written entries in targets.py (see the heb/whataburger
    entries). Fields left at their Target() default are omitted rather than
    spelled out, same as every existing entry does.
    """
    lines = [f'    {slug!r}: Target(']
    head = f'        name={name!r}, osm={osm!r}'
    trailing = []
    if atp_spider:
        trailing.append(f'atp_spider={atp_spider!r}')
    if atp_brands:
        brands_src = "frozenset({" + ", ".join(repr(b) for b in sorted(atp_brands)) + "})"
        trailing.append(f'atp_brands={brands_src}')
    if include_instore:
        trailing.append(f'include_instore={include_instore!r}')

    if not trailing and not osm_clauses:
        lines.append(head + '),')
        return "\n".join(lines) + "\n"

    lines.append(head + ("," if trailing or osm_clauses else ""))
    for i, kw in enumerate(trailing):
        is_last_kw = (i == len(trailing) - 1)
        suffix = "" if (is_last_kw and not osm_clauses) else ","
        lines.append(f'        {kw}{suffix}')

    if osm_clauses:
        lines.append('        osm_clauses=(')
        for clause in osm_clauses:
            lines.append(f'            {clause!r},')
        lines.append('        )),')
    else:
        lines[-1] = lines[-1].rstrip(",") + "),"

    return "\n".join(lines) + "\n"


def _validate_shape(name, osm, atp_spider, atp_brands, include_instore, osm_clauses):
    """`import targets` succeeding only proves the file parses -- it doesn't
    catch a malformed osm tuple, since config.retarget()'s
    `PLACE_MAIN_TYPE, PLACE_TYPE = t.osm` unpacking only runs when the
    target is actually selected, not on import. Check the shapes
    config.retarget() and places.py actually rely on before ever touching
    disk (mirrors knobs._validate_kind's reasoning)."""
    if not name or not isinstance(name, str):
        raise ValueError(f"name must be a non-empty string, got {name!r}")
    if not (isinstance(osm, tuple) and len(osm) == 2
            and all(isinstance(v, str) and v for v in osm)):
        raise ValueError(f"osm must be a 2-tuple of non-empty strings, got {osm!r}")
    if atp_spider is not None and not isinstance(atp_spider, str):
        raise ValueError(f"atp_spider must be a string or None, got {atp_spider!r}")
    if atp_brands is not None and not (
            isinstance(atp_brands, (set, frozenset))
            and all(isinstance(b, str) for b in atp_brands)):
        raise ValueError(f"atp_brands must be a set of strings or None, got {atp_brands!r}")
    if not isinstance(include_instore, bool):
        raise ValueError(f"include_instore must be a bool, got {include_instore!r}")
    if osm_clauses is not None and not (
            isinstance(osm_clauses, (tuple, list))
            and all(isinstance(c, str) for c in osm_clauses)):
        raise ValueError(f"osm_clauses must be a sequence of strings or None, got {osm_clauses!r}")


class TargetExistsError(ValueError):
    """Raised by add_target when `slug` is already in targets.TARGETS and
    overwrite=False. A subclass of ValueError (not a new exception family)
    so existing `except ValueError` callers keep working; callers that want
    to offer a "the caller wants to overwrite it" confirmation before
    retrying with overwrite=True can catch this specifically instead of
    treating every failure the same way.
    """


def _find_targets_dict(tree):
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "TARGETS"
                and isinstance(node.value, ast.Dict)):
            return node
    raise ValueError("could not find a top-level TARGETS = {...} assignment in targets.py")


def _entry_line_range(dict_node, slug):
    """1-indexed (start, end) inclusive source line range for the dict entry
    keyed `slug` -- the key's own line through its value's closing line, so
    replacing lines[start-1:end] with a freshly rendered block swaps the
    whole `"slug": Target(...)),` entry in place. Returns None if not found.
    """
    for key, value in zip(dict_node.keys, dict_node.values):
        if isinstance(key, ast.Constant) and key.value == slug:
            return key.lineno, value.end_lineno
    return None


def existing_slugs(path=CONFIG_PATH):
    with open(path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    node = _find_targets_dict(tree)
    return {k.value for k in node.value.keys if isinstance(k, ast.Constant)}


def add_target(slug, name, osm, atp_spider=None, atp_brands=None,
               include_instore=False, osm_clauses=None, path=CONFIG_PATH,
               overwrite=False):
    """Insert a new Target() entry into targets.py's TARGETS dict, or
    replace the existing one in place when `overwrite=True`.

    Raises TargetExistsError (a ValueError) if `slug` is already present
    and overwrite=False; raises ValueError if the rendered source fails to
    re-import afterward (in which case the file is rolled back to its
    pre-edit state -- nothing is left half-written either way).

    Returns True on success.
    """
    _validate_shape(name, osm, atp_spider, atp_brands, include_instore, osm_clauses)

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src, filename=path)
    node = _find_targets_dict(tree)

    slugs = {k.value for k in node.value.keys if isinstance(k, ast.Constant)}
    existing_range = _entry_line_range(node.value, slug) if slug in slugs else None
    if existing_range is not None and not overwrite:
        raise TargetExistsError(f"{slug!r} already exists in targets.TARGETS")

    lines = src.splitlines(keepends=True)
    block = render_target(slug, name, osm, atp_spider=atp_spider, atp_brands=atp_brands,
                          include_instore=include_instore, osm_clauses=osm_clauses)

    backup_path = path + ".bak"
    shutil.copyfile(path, backup_path)
    try:
        if existing_range is not None:
            start, end = existing_range  # 1-indexed, inclusive
            new_lines = lines[:start - 1] + [block] + lines[end:]
        else:
            close_lineno = node.value.end_lineno  # line holding the dict's closing "}"
            new_lines = lines[:close_lineno - 1] + [block] + lines[close_lineno - 1:]
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)

        result = subprocess.run(
            [sys.executable, "-c", f"import targets; targets.TARGETS[{slug!r}]"],
            cwd=os.path.dirname(os.path.abspath(path)) or ".",
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            shutil.copyfile(backup_path, path)
            raise ValueError(
                f"invalid target {slug!r} -- targets.py failed to import, "
                f"change rolled back:\n{result.stderr.strip()}")
    finally:
        if os.path.exists(backup_path):
            os.remove(backup_path)

    return True
