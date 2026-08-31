"""Surgical edits to RustDesk's TOML config files.

RustDesk's per-peer file holds the saved password as a multi-line array of
bytes. Round-tripping the file through a parser and re-serialising it would
rewrite that array along with everything else -- so this module does not do
that. It finds one key, replaces exactly its value text, and leaves every other
byte of the file alone.

Two guards make that claim checkable rather than aspirational:

* the edited text must still parse as TOML before it is written
* the ``password`` assignment must be byte-identical afterwards, or the write is
  abandoned

**A caveat that will bite otherwise:** RustDesk keeps its configuration in
memory and writes it out as it goes. Editing a file underneath a running
instance can be silently overwritten. ``assert_safe_to_edit`` checks for a live
session window and says so.
"""

import os
import re
import tomllib

from .errors import BridgeError

_SECTION = re.compile(r"^\s*\[([^\]]+)\]\s*$")


def format_value(value):
    """Render a Python value as TOML."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) or isinstance(value, float):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(format_value(item) for item in value) + "]"
    if value is None:
        raise BridgeError("TOML has no null; use an empty string instead.")
    text = str(value)
    escaped = (text.replace("\\", "\\\\").replace('"', '\\"')
               .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"))
    return '"%s"' % escaped


def _value_end(text, start):
    """Index just past the value beginning at ``start``.

    Handles bracketed values spanning many lines, and ignores brackets that
    appear inside quoted strings.
    """
    index = start
    while index < len(text) and text[index] in " \t":
        index += 1
    if index >= len(text):
        return index

    if text[index] not in "[{":
        newline = text.find("\n", index)
        return len(text) if newline == -1 else newline

    opener = text[index]
    closer = "]" if opener == "[" else "}"
    depth = 0
    quote = None
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    raise BridgeError("Unterminated %s value in TOML." % opener)


def _find_key(text, key, section=None):
    """Locate ``key`` within ``section``; return (start, value_start, end) or None."""
    current = None
    offset = 0
    pattern = re.compile(r"^\s*(?:%s|\"%s\"|'%s')\s*=" % (
        re.escape(key), re.escape(key), re.escape(key)))

    for line in text.splitlines(keepends=True):
        header = _SECTION.match(line)
        if header:
            current = header.group(1).strip()
        elif current == section:
            match = pattern.match(line)
            if match:
                value_start = offset + match.end()
                return offset, value_start, _value_end(text, value_start)
        offset += len(line)
    return None


def _section_end(text, section):
    """Offset at which ``section`` ends, or None if it is absent."""
    current = None
    offset = 0
    end_of_section = None
    for line in text.splitlines(keepends=True):
        header = _SECTION.match(line)
        if header:
            if current == section:
                return end_of_section
            current = header.group(1).strip()
        if current == section:
            end_of_section = offset + len(line)
        offset += len(line)
    return end_of_section if current == section else None


def set_key(text, key, value, section=None):
    """Return ``text`` with ``key`` set to ``value``, inserting it if absent."""
    rendered = format_value(value)
    found = _find_key(text, key, section)
    if found:
        _, value_start, value_end = found
        return text[:value_start] + " " + rendered + text[value_end:]

    assignment = "%s = %s\n" % (key, rendered)
    if section is None:
        # Top-level keys must precede the first section header.
        first_header = None
        offset = 0
        for line in text.splitlines(keepends=True):
            if _SECTION.match(line):
                first_header = offset
                break
            offset += len(line)
        if first_header is None:
            return text + ("" if text.endswith("\n") or not text else "\n") + assignment
        return text[:first_header] + assignment + text[first_header:]

    end = _section_end(text, section)
    if end is None:
        prefix = text if text.endswith("\n") or not text else text + "\n"
        return prefix + "\n[%s]\n%s" % (section, assignment)
    return text[:end] + assignment + text[end:]


def _password_region(text):
    """The exact text of the password assignment, for the tamper check."""
    found = _find_key(text, "password", None)
    if not found:
        return None
    start, _, end = found
    return text[start:end]


def edit_file(path, key, value, section=None):
    """Set one key in a TOML file in place, preserving everything else.

    Returns a dict describing the change. Raises rather than writing if the
    result would not parse, or if the stored password moved.
    """
    with open(path, "r", encoding="utf-8") as handle:
        original = handle.read()

    # Compare the parsed value, not the text. RustDesk writes strings with
    # single quotes and this module writes them with double quotes, so a
    # text comparison would call every no-op a change and rewrite a file
    # RustDesk owns for nothing.
    try:
        current = tomllib.loads(original)
    except tomllib.TOMLDecodeError as exc:
        raise BridgeError(
            "Refusing to edit %s: it is not valid TOML to begin with (%s)."
            % (os.path.basename(path), exc)
        )
    container = current.get(section) if section else current
    if isinstance(container, dict) and key in container and container[key] == value:
        return {"path": path, "key": key, "section": section, "changed": False,
                "value": value}

    updated = set_key(original, key, value, section)
    if updated == original:
        return {"path": path, "key": key, "section": section, "changed": False}

    try:
        parsed = tomllib.loads(updated)
    except tomllib.TOMLDecodeError as exc:
        raise BridgeError(
            "Refusing to write %s: the edit produced invalid TOML (%s)."
            % (os.path.basename(path), exc)
        )

    before, after = _password_region(original), _password_region(updated)
    if before != after:
        raise BridgeError(
            "Refusing to write %s: the stored password block changed. The bridge "
            "must never rewrite a credential, so this edit is abandoned."
            % os.path.basename(path)
        )

    temp = path + ".rdbridge.tmp"
    with open(temp, "w", encoding="utf-8", newline="") as handle:
        handle.write(updated)
    os.replace(temp, path)

    container = parsed.get(section) if section else parsed
    return {
        "path": path,
        "key": key,
        "section": section,
        "changed": True,
        "value": (container or {}).get(key),
    }


def assert_safe_to_edit(peer_id=None):
    """Warn when RustDesk is likely to overwrite the file we are about to edit."""
    from . import windows
    try:
        open_sessions = windows.session_windows()
    except OSError:
        return None
    if not open_sessions:
        return None
    titles = ", ".join(repr(w["title"]) for w in open_sessions)
    raise BridgeError(
        "Refusing to edit peer config while %d RustDesk session window(s) are "
        "open (%s). RustDesk holds its config in memory and writes it back on "
        "close, which would silently discard this edit. Close the session first."
        % (len(open_sessions), titles)
    )
