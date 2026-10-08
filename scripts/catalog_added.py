#!/usr/bin/env python3
"""Give every catalog tool row without an `added:` date today's date.

    uv run python scripts/catalog_added.py            # write today's date where it is missing
    uv run python scripts/catalog_added.py --check    # list the rows that have none; exit 1 if any

`added` is the UTC day a tool first became available on main (docs/context/architecture/catalog.md).
This helper never touches an existing date. It inserts one line per row as text, next to
`verified:` when the row has one and after `id:` otherwise, so hand-written comments and layout
survive; the YAML is never re-dumped.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import yaml

CATALOG = Path(__file__).resolve().parent.parent / "src" / "treg" / "catalog"


def _rows(text: str) -> list[yaml.MappingNode]:
    """The endpoint mapping nodes of one catalog file, in file order; [] for a non-tool file."""
    root = yaml.compose(text, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))
    if not isinstance(root, yaml.MappingNode):
        return []
    for key, value in root.value:
        if key.value == "endpoints" and isinstance(value, yaml.SequenceNode):
            return [n for n in value.value if isinstance(n, yaml.MappingNode)]
    return []


def row_ids(text: str) -> list[str]:
    out = []
    for node in _rows(text):
        for key, value in node.value:
            if key.value == "id" and isinstance(value, yaml.ScalarNode) and value.value:
                out.append(value.value)
    return out


def insert_added(text: str, dates: dict[str, str]) -> tuple[str, list[str], list[str]]:
    """Insert `added: '<date>'` into every row that has none and an entry in `dates`.

    Returns the new text, the ids that got a date, and the ids that could not be placed (a row
    with no `added`, no entry in `dates`, or a layout the line insert cannot handle safely).
    """
    lines = text.splitlines(keepends=True)
    inserts: list[tuple[int, str]] = []
    placed: list[str] = []
    unplaced: list[str] = []
    for node in _rows(text):
        keys = {k.value: (k, v) for k, v in node.value}
        if "id" not in keys or "added" in keys:
            continue
        row_id = keys["id"][1].value
        when = dates.get(row_id)
        if node.flow_style or when is None:
            unplaced.append(row_id)
            continue
        key, value = keys.get("verified") or keys["id"]
        # A one-line scalar ends on its key's line; anything else is not a shape this inserts into.
        if not isinstance(value, yaml.ScalarNode) or value.end_mark.line != key.start_mark.line:
            unplaced.append(row_id)
            continue
        eol = "\r\n" if lines[key.start_mark.line].endswith("\r\n") else "\n"
        inserts.append((key.start_mark.line + 1, " " * key.start_mark.column + f"added: '{when}'{eol}"))
        placed.append(row_id)
    for at, line in sorted(inserts, reverse=True):
        lines.insert(at, line)
    return "".join(lines), placed, unplaced


def missing(directory: Path = CATALOG) -> dict[Path, list[str]]:
    out = {}
    for path in sorted(directory.glob("*.yaml")):
        _, _, none = insert_added(path.read_text(), {})
        if none:
            out[path] = none
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="only list rows with no added date")
    parser.add_argument("--date", default=datetime.now(UTC).date().isoformat(), help="date to write (default today, UTC)")
    args = parser.parse_args(argv)
    date.fromisoformat(args.date)
    todo = missing()
    if args.check:
        for path, ids in todo.items():
            for row_id in ids:
                print(f"{path.name}: {row_id} has no added date")
        return 1 if todo else 0
    for path, ids in todo.items():
        new, placed, unplaced = insert_added(path.read_text(), dict.fromkeys(ids, args.date))
        path.write_text(new)
        for row_id in placed:
            print(f"{path.name}: {row_id} added {args.date}")
        for row_id in unplaced:
            print(f"{path.name}: {row_id} NOT placed; add `added: '{args.date}'` by hand", file=sys.stderr)
    if not todo:
        print("every tool row already has an added date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
