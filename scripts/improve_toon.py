"""Token-Oriented Object Notation (TOON), specification 4.4. Standard library only.

TOON is the format the skill's helper CLIs print for the agent: an array of uniform
objects declares its length and field list once and then spends one delimited row per
element, so a backlog, a request queue, or a graph query costs far fewer tokens than
JSON while keeping nesting, types, and explicit counts. The data model is JSON's; storage
stays JSON, and this module is the translation layer at the model boundary.

    dumps(value, indent=2, delimiter=",") -> str      encoder (spec 13.1)
    loads(text, indent=2, strict=True) -> value       decoder (spec 13.2)
    uniform(rows) -> rows                             give an array of objects one key set so
                                                      it encodes in tabular form
    truncate(value, width) -> value                   bound long text cells for list views

Host-type normalization (spec 3): NaN and infinities become null; sets and tuples become
arrays (sets sorted by their encoded form); datetimes become ISO 8601 strings; Decimals
that a float cannot hold exactly become quoted decimal strings; other objects become
null. Decoded numbers are Python int or float; a token too large for a float decodes as a
string (lossless-first). Decoded objects are plain dicts, which keep `__proto__` and
similar keys as ordinary entries.

The implementation is verified against the official conformance fixtures in
tests/fixtures/toon (MIT, github.com/toon-format/spec).
"""

import datetime
import decimal
import json
import math
import os
import re
import sys

SPEC_VERSION = "4.4"
DELIMITERS = {",": ",", "comma": ",", "\t": "\t", "tab": "\t", "|": "|", "pipe": "|"}
UNQUOTED_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
NUMERIC_LIKE = re.compile(r"^[+-]?[0-9]+(?:\.[0-9]+)?(?:e[+-]?[0-9]+)?$", re.IGNORECASE)
JSON_NUMBER = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?$")
LENGTH = re.compile(r"^(0|[1-9][0-9]*)$")
ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t"}
UNESCAPES = {"\\": "\\", '"': '"', "n": "\n", "r": "\r", "t": "\t"}
MAX_DEPTH = 500


class ToonError(ValueError):
    """Invalid input to the encoder or the decoder; the message is safe to show."""


class ToonDecodeError(ToonError):
    def __init__(self, message, line=None):
        super().__init__(f"line {line}: {message}" if line is not None else message)
        self.line = line


def delimiter_symbol(delimiter):
    try:
        return DELIMITERS[delimiter]
    except KeyError:
        raise ToonError("delimiter must be comma, tab, or pipe") from None


# ---------------------------------------------------------------- normalization

def normalize(value, depth=0):
    """Map host values onto the JSON data model (spec 3)."""
    if depth > MAX_DEPTH:
        raise ToonError("value nests too deeply to encode")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ToonError("string contains an unpaired surrogate and cannot be encoded") from exc
        return value
    if isinstance(value, decimal.Decimal):
        if not value.is_finite():
            return None
        if value == value.to_integral_value():
            return int(value)
        approximate = float(value)
        return approximate if decimal.Decimal(repr(approximate)) == value else format(value, "f")
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    # Plain loops, not comprehensions: before Python 3.12 a comprehension is a frame of its own,
    # which doubled the stack per level and overflowed before MAX_DEPTH was reached.
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            result[str(key)] = normalize(item, depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        items = []
        for item in value:
            items.append(normalize(item, depth + 1))
        return items
    if isinstance(value, (set, frozenset)):
        items = [normalize(item, depth + 1) for item in value]
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True, default=str))
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    return None


def is_primitive(value):
    return value is None or isinstance(value, (bool, int, float, str))


# ---------------------------------------------------------------- primitives

def format_number(number):
    """Canonical decimal form (spec 2)."""
    if isinstance(number, int):
        return str(number)
    if number == 0:
        return "0"
    if number.is_integer() and abs(number) < 1e21:
        return str(int(number))
    exact = decimal.Decimal(repr(number))
    magnitude = abs(number)
    if 1e-6 <= magnitude < 1e21:
        text = format(exact, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text
    sign, digits, exponent = exact.normalize().as_tuple()
    mantissa = "".join(str(digit) for digit in digits)
    power = exponent + len(mantissa) - 1
    if len(mantissa) > 1:
        mantissa = mantissa[0] + "." + mantissa[1:]
    return f"{'-' if sign else ''}{mantissa}e{'+' if power >= 0 else '-'}{abs(power)}"


def escape(text):
    out = []
    for char in text:
        if char in ESCAPES:
            out.append(ESCAPES[char])
        elif ord(char) < 0x20:
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(char)
    return "".join(out)


def needs_quotes(text, delimiter, root=False):
    """Spec 7.2."""
    if text == "" or text[0] in " \t" or text[-1] in " \t":
        return True
    if text in ("true", "false", "null") or NUMERIC_LIKE.fullmatch(text):
        return True
    if any(char in text for char in ':"\\[]{}') or delimiter in text:
        return True
    if any(ord(char) < 0x20 for char in text) or text[0] in "-#":
        return True
    return root and text.startswith("\ufeff")


def encode_string(text, delimiter, root=False):
    return f'"{escape(text)}"' if needs_quotes(text, delimiter, root) else text


def encode_key(key):
    """Spec 7.3."""
    return key if UNQUOTED_KEY.fullmatch(key) else f'"{escape(key)}"'


def encode_primitive(value, delimiter, root=False):
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return format_number(value)
    return encode_string(value, delimiter, root)


# ---------------------------------------------------------------- shape detection

def tabular_fields(rows):
    """The field tree for an array of uniform objects (spec 9.3), or None when the array
    must use list form. Each entry is (name, subtree or None)."""
    if not rows:
        return None
    for row in rows:
        if not isinstance(row, dict) or not row:
            return None
    keys = set(rows[0])
    for row in rows[1:]:
        if set(row) != keys:
            return None
    fields = []
    for key in rows[0]:
        column = [row[key] for row in rows]
        if all(is_primitive(cell) for cell in column):
            fields.append((key, None))
        elif all(isinstance(cell, dict) and cell for cell in column):
            nested = tabular_fields(column)
            if nested is None:
                return None
            fields.append((key, nested))
        else:
            return None
    return fields


def keyed_fields(value):
    """Spec 9.5: an object of at least two uniform non-empty objects."""
    if not isinstance(value, dict) or len(value) < 2:
        return None
    return tabular_fields(list(value.values()))


def leaf_count(fields):
    return sum(1 if nested is None else leaf_count(nested) for _, nested in fields)


# ---------------------------------------------------------------- encoder

class Encoder:
    def __init__(self, indent=2, delimiter=","):
        if not isinstance(indent, int) or isinstance(indent, bool) or indent < 1:
            raise ToonError("indent must be a positive integer")
        self.indent = indent
        self.delimiter = delimiter_symbol(delimiter)
        self.symbol = "" if self.delimiter == "," else self.delimiter

    def pad(self, depth):
        return " " * (self.indent * depth)

    def primitive(self, value, root=False):
        return encode_primitive(value, self.delimiter, root)

    def fields_text(self, fields):
        parts = []
        for name, nested in fields:
            parts.append(encode_key(name) + (f"{{{self.fields_text(nested)}}}" if nested else ""))
        return self.delimiter.join(parts)

    def cells(self, obj, fields):
        out = []
        for name, nested in fields:
            if nested is None:
                out.append(self.primitive(obj[name]))
            else:
                out.extend(self.cells(obj[name], nested))
        return out

    def encode(self, value):
        value = normalize(value)
        if is_primitive(value):
            return self.primitive(value, root=True)
        lines = []
        if isinstance(value, list):
            if not value:
                return "[]"
            self.array(lines, "", value, 1)
        else:
            if not value:
                return ""
            fields = keyed_fields(value)
            if fields:
                self.keyed(lines, "", value, fields, 1)
            else:
                self.object_fields(lines, value, 0)
        return "\n".join(lines)

    def object_fields(self, lines, obj, depth):
        for key, value in obj.items():
            self.entry(lines, encode_key(key), value, depth, marker=False)

    def entry(self, lines, key, value, depth, marker):
        """One `key: ...` field at `depth`; with `marker` it is the first field of a list
        item and rides on the hyphen line, so a scope it opens lives two levels deeper."""
        head = self.pad(depth) + ("- " if marker else "")
        inner = depth + (2 if marker else 1)
        if is_primitive(value):
            lines.append(f"{head}{key}: {self.primitive(value)}")
        elif isinstance(value, list):
            if not value:
                lines.append(f"{head}{key}: []")
            else:
                self.array(lines, head + key, value, inner)
        elif not value:
            lines.append(f"{head}{key}:")
        else:
            fields = keyed_fields(value)
            if fields:
                self.keyed(lines, head + key, value, fields, inner)
            else:
                lines.append(f"{head}{key}:")
                self.object_fields(lines, value, inner)

    def array(self, lines, head, items, inner):
        """`head` is the padded text before the bracket (a key, or nothing at the root)."""
        count = len(items)
        if all(is_primitive(item) for item in items):
            lines.append(f"{head}[{count}{self.symbol}]: " + self.delimiter.join(self.primitive(item) for item in items))
            return
        fields = tabular_fields(items)
        if fields:
            lines.append(f"{head}[{count}{self.symbol}]{{{self.fields_text(fields)}}}:")
            for item in items:
                lines.append(self.pad(inner) + self.delimiter.join(self.cells(item, fields)))
            return
        lines.append(f"{head}[{count}{self.symbol}]:")
        for item in items:
            self.list_item(lines, item, inner)

    def list_item(self, lines, item, depth):
        head = self.pad(depth) + "- "
        if is_primitive(item):
            lines.append(head + self.primitive(item))
        elif isinstance(item, list):
            count = len(item)
            if not item:
                lines.append(f"{head}[0{self.symbol}]:")
            elif all(is_primitive(sub) for sub in item):
                lines.append(f"{head}[{count}{self.symbol}]: " + self.delimiter.join(self.primitive(sub) for sub in item))
            else:
                # A keyless header is never tabular (spec 9.4): items use list form.
                lines.append(f"{head}[{count}{self.symbol}]:")
                for sub in item:
                    self.list_item(lines, sub, depth + 1)
        elif not item:
            lines.append(self.pad(depth) + "-")
        else:
            keys = list(item)
            self.entry(lines, encode_key(keys[0]), item[keys[0]], depth, marker=True)
            for key in keys[1:]:
                self.entry(lines, encode_key(key), item[key], depth + 1, marker=False)

    def keyed(self, lines, head, obj, fields, inner):
        lines.append(f"{head}[{len(obj)}:{self.symbol}]{{{self.fields_text(fields)}}}:")
        for key, value in obj.items():
            lines.append(self.pad(inner) + encode_key(key) + ": " + self.delimiter.join(self.cells(value, fields)))


def dumps(value, indent=2, delimiter=","):
    """Encode a JSON-shaped value as TOON."""
    try:
        return Encoder(indent, delimiter).encode(value)
    except RecursionError:
        # The encoder uses a few frames per level; a value just under MAX_DEPTH can still
        # exhaust a small interpreter stack. Report it as the documented error.
        raise ToonError("value nests too deeply to encode") from None


# ---------------------------------------------------------------- decoder helpers

def closing_quote(text, start):
    """Index of the quote closing the span that opened at `start`, or -1 (escape aware)."""
    index = start + 1
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == '"':
            return index
        index += 1
    return -1


def first_unquoted(text, chars, start=0):
    """Index of the first character in `chars` outside quoted spans, or -1."""
    index = start
    while index < len(text):
        char = text[index]
        if char == '"':
            end = closing_quote(text, index)
            if end < 0:
                return -1
            index = end + 1
            continue
        if char in chars:
            return index
        index += 1
    return -1


def unescape(text, line=None):
    out = []
    index = 0
    while index < len(text):
        char = text[index]
        if char != "\\":
            out.append(char)
            index += 1
            continue
        if index + 1 >= len(text):
            raise ToonDecodeError("unterminated escape sequence", line)
        code = text[index + 1]
        if code in UNESCAPES:
            out.append(UNESCAPES[code])
            index += 2
            continue
        if code == "u":
            digits = text[index + 2:index + 6]
            if len(digits) != 4 or not all(digit in "0123456789abcdefABCDEF" for digit in digits):
                raise ToonDecodeError("\\u must be followed by four hex digits", line)
            point = int(digits, 16)
            if 0xD800 <= point <= 0xDFFF:
                raise ToonDecodeError("surrogate code points cannot be escaped", line)
            out.append(chr(point))
            index += 6
            continue
        raise ToonDecodeError(f"invalid escape sequence \\{code}", line)
    return "".join(out)


def decode_quoted(token, line=None):
    """A complete quoted token (spec 7.4): the closing quote must be the last character."""
    end = closing_quote(token, 0)
    if end < 0:
        raise ToonDecodeError("unterminated quoted string", line)
    if end != len(token) - 1:
        raise ToonDecodeError("unexpected characters after a closing quote", line)
    return unescape(token[1:end], line)


def decode_primitive(token, line=None):
    """Spec 4 and Appendix B.4."""
    if token.startswith('"'):
        return decode_quoted(token, line)
    if token == "true":
        return True
    if token == "false":
        return False
    if token == "null":
        return None
    if JSON_NUMBER.fullmatch(token):
        if "." in token or "e" in token or "E" in token:
            number = float(token)
            return token if math.isinf(number) else number
        return int(token)
    return token


def decode_key(token, line=None):
    token = token.strip(" ")
    if token.startswith('"'):
        return decode_quoted(token, line)
    return token


def split_cells(text, delimiter):
    """Spec 11.2 and Appendix B.3: split on the unquoted delimiter, trim spaces, keep empties."""
    if text.strip(" ") == "":
        return []
    cells, current, index = [], [], 0
    while index < len(text):
        char = text[index]
        if char == '"':
            end = closing_quote(text, index)
            if end < 0:
                current.append(text[index:])
                break
            current.append(text[index:end + 1])
            index = end + 1
            continue
        if char == delimiter:
            cells.append("".join(current).strip(" "))
            current = []
        else:
            current.append(char)
        index += 1
    cells.append("".join(current).strip(" "))
    return cells


class Header:
    __slots__ = ("key", "length", "keyed", "delimiter", "fields", "rest")

    def __init__(self, key, length, keyed, delimiter, fields, rest):
        self.key, self.length, self.keyed, self.delimiter, self.fields, self.rest = key, length, keyed, delimiter, fields, rest


def is_header_line(content):
    """Spec 5.2 class 4: an unquoted colon whose first unquoted `[` precedes it."""
    colon = first_unquoted(content, ":")
    if colon < 0:
        return False
    bracket = first_unquoted(content, "[")
    return 0 <= bracket < colon


def parse_fields(text, delimiter, line, strict):
    """Field entries of a `{...}` list (without the braces), recursively."""
    if text == "":
        raise ToonDecodeError("empty field list", line)
    for other in (",", "\t", "|"):
        if other != delimiter and first_unquoted(text, other) >= 0 and other_outside_groups(text, other):
            raise ToonDecodeError("field list uses a delimiter the header did not declare", line)
    entries, depth, current, index = [], 0, [], 0
    while index < len(text):
        char = text[index]
        if char == '"':
            end = closing_quote(text, index)
            if end < 0:
                raise ToonDecodeError("unterminated quoted field name", line)
            current.append(text[index:end + 1])
            index = end + 1
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth < 0:
                raise ToonDecodeError("unmatched brace in field list", line)
        if char == delimiter and depth == 0:
            entries.append("".join(current))
            current = []
        else:
            current.append(char)
        index += 1
    if depth != 0:
        raise ToonDecodeError("unmatched brace in field list", line)
    entries.append("".join(current))
    fields, seen = [], set()
    for entry in entries:
        entry = entry.strip(" ")
        if entry == "":
            raise ToonDecodeError("empty field entry", line)
        brace = first_unquoted(entry, "{")
        if brace < 0:
            name, nested = entry, None
        else:
            if not entry.endswith("}"):
                raise ToonDecodeError("nested field group must end the entry", line)
            name, nested = entry[:brace], parse_fields(entry[brace + 1:-1], delimiter, line, strict)
            if name == "":
                raise ToonDecodeError("nested field group without a field name", line)
            if name[-1] in " \t":
                raise ToonDecodeError("whitespace before a nested field group", line)
        if name.startswith('"'):
            name = decode_key(name, line)
        elif name.strip(" ") == "":
            raise ToonDecodeError("empty field entry", line)
        if name in seen and strict:
            raise ToonDecodeError(f"duplicate field name {name!r}", line)
        seen.add(name)
        fields.append((name, nested))
    return fields


def other_outside_groups(text, char):
    """True when `char` occurs unquoted anywhere in the field text (groups included)."""
    return first_unquoted(text, char) >= 0


def parse_header(content, line, strict):
    """Spec 6."""
    if content.startswith('"'):
        end = closing_quote(content, 0)
        if end < 0:
            raise ToonDecodeError("unterminated quoted key", line)
        key, index = unescape(content[1:end], line), end + 1
        if index >= len(content) or content[index] != "[":
            raise ToonDecodeError("expected [ after the quoted key", line)
    else:
        index = first_unquoted(content, "[")
        key = content[:index]
        if key and key[-1] in " \t":
            raise ToonDecodeError("whitespace between a key and its bracket segment", line)
        key = key if key else None
    close = content.find("]", index)
    if close < 0:
        raise ToonDecodeError("unclosed bracket segment", line)
    segment = content[index + 1:close]
    keyed = False
    delimiter = ","
    match = re.match(r"^(0|[1-9][0-9]*)", segment)
    if not match:
        raise ToonDecodeError(f"invalid array length in [{segment}]", line)
    length, remainder = int(match.group(1)), segment[match.end():]
    if remainder.startswith(":"):
        keyed, remainder = True, remainder[1:]
    if remainder in ("\t", "|"):
        delimiter, remainder = remainder, ""
    if remainder != "":
        raise ToonDecodeError(f"malformed bracket segment [{segment}]", line)
    index = close + 1
    if index >= len(content) or content[index] not in "{:":
        raise ToonDecodeError("expected { or : after the bracket segment", line)
    fields = None
    if content[index] == "{":
        depth, scan = 0, index
        while scan < len(content):
            char = content[scan]
            if char == '"':
                end = closing_quote(content, scan)
                if end < 0:
                    raise ToonDecodeError("unterminated quoted field name", line)
                scan = end + 1
                continue
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    break
            scan += 1
        if depth != 0:
            raise ToonDecodeError("unmatched brace in field list", line)
        fields = parse_fields(content[index + 1:scan], delimiter, line, strict)
        index = scan + 1
        if index >= len(content) or content[index] != ":":
            raise ToonDecodeError("expected : after the field list", line)
    elif keyed:
        raise ToonDecodeError("a keyed header requires a field list", line)
    rest = content[index + 1:].strip(" ")
    if fields is not None and rest != "":
        raise ToonDecodeError("content after a header with a field list", line)
    return Header(key, length, keyed, delimiter, fields, rest)


class Line:
    __slots__ = ("number", "depth", "text", "blank")

    def __init__(self, number, depth, text, blank):
        self.number, self.depth, self.text, self.blank = number, depth, text, blank


# ---------------------------------------------------------------- decoder

class Decoder:
    def __init__(self, indent=2, strict=True):
        if not isinstance(indent, int) or isinstance(indent, bool) or indent < 1:
            raise ToonError("indent must be a positive integer")
        self.indent, self.strict = indent, strict
        self.lines = []
        self.position = 0
        self.spans = []

    # ---- preparation (spec 12)

    def prepare(self, text):
        if isinstance(text, (bytes, bytearray)):
            try:
                text = bytes(text).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ToonDecodeError(f"input is not valid UTF-8: {exc.reason}") from exc
        if text.startswith("\ufeff"):
            text = text[1:]
        lines = []
        for number, raw in enumerate(text.split("\n"), 1):
            if raw.endswith("\r"):
                raw = raw[:-1]
            raw = raw.rstrip(" ")
            stripped = raw.lstrip(" ")
            if stripped.startswith("#"):
                continue
            if raw == "":
                lines.append(Line(number, 0, "", True))
                continue
            index = 0
            while index < len(raw) and raw[index] in " \t":
                index += 1
            indentation, content = raw[:index], raw[index:]
            if content == "" and not self.strict:
                lines.append(Line(number, 0, "", True))
                continue
            if self.strict:
                if "\t" in indentation:
                    raise ToonDecodeError("tabs are not allowed in indentation", number)
                if len(indentation) % self.indent:
                    raise ToonDecodeError(f"indentation is not a multiple of {self.indent} spaces", number)
                depth = len(indentation) // self.indent
            else:
                depth = indentation.count("\t") + indentation.count(" ") // self.indent
            lines.append(Line(number, depth, content, False))
        self.lines = lines

    # ---- cursor helpers

    def current(self):
        return self.lines[self.position] if self.position < len(self.lines) else None

    def peek_content(self):
        """The next non-blank line (without consuming it), or None."""
        index = self.position
        while index < len(self.lines):
            if not self.lines[index].blank:
                return self.lines[index]
            index += 1
        return None

    def skip_blank(self, line):
        """Consume a blank line; inside an open header span that is an error in strict mode."""
        following = self.peek_content()
        open_spans = [depth for depth in self.spans if depth is not None]
        inside = following is not None and open_spans and following.depth >= min(open_spans)
        if inside and self.strict:
            raise ToonDecodeError("blank line inside an array", line.number)
        self.position += 1

    # ---- root (spec 5)

    def decode(self, text):
        self.prepare(text)
        self.position, self.spans = 0, []
        first = self.peek_content()
        if first is None:
            return {}
        if first.depth != 0:
            raise ToonDecodeError("the first line must not be indented", first.number)
        self.position = self.lines.index(first)
        content = first.text
        if is_header_line(content):
            header = parse_header(content, first.number, self.strict)
            if header.key is None:
                self.position += 1
                value = self.array_body(header, first, 1)
                self.expect_end()
                return value
            return self.object(0, -1)
        if content == "[]":
            self.position += 1
            self.expect_end()
            return []
        if first_unquoted(content, ":") >= 0:
            return self.object(0, -1)
        if any(not line.blank for line in self.lines[self.position + 1:]):
            raise ToonDecodeError("a scalar line is valid only as the whole document", first.number)
        self.position += 1
        return decode_primitive(content, first.number)

    def expect_end(self):
        trailing = self.peek_content()
        if trailing is not None:
            raise ToonDecodeError("content after the end of the root array", trailing.number)

    # ---- objects (spec 8)

    def object(self, depth, end_depth, initial=None):
        """Fields at `depth`; the scope ends at the first line at or above `end_depth`."""
        obj = initial if initial is not None else {}
        while True:
            line = self.current()
            if line is None:
                break
            if line.blank:
                self.skip_blank(line)
                continue
            if line.depth <= end_depth:
                break
            if line.depth < depth:
                raise ToonDecodeError("line is shallower than its scope", line.number)
            if line.depth > depth:
                raise ToonDecodeError("line is indented deeper than its scope", line.number)
            self.position += 1
            self.field(obj, line.text, line, depth, depth + 1)
        return obj

    def field(self, obj, content, line, depth, content_depth):
        """One field line of an object; a scope it opens has its content at `content_depth`."""
        if is_header_line(content):
            header = parse_header(content, line.number, self.strict)
            if header.key is None:
                raise ToonDecodeError("a keyless header is valid only at the root or as a list item", line.number)
            self.store(obj, header.key, self.array_body(header, line, content_depth), line)
            return
        colon = first_unquoted(content, ":")
        if colon < 0:
            raise ToonDecodeError("expected key: value", line.number)
        key = decode_key(content[:colon], line.number)
        rest = content[colon + 1:].strip(" ")
        if rest == "":
            self.store(obj, key, self.nested_object(depth, content_depth, line), line)
        elif rest == "[]":
            self.store(obj, key, [], line)
        else:
            self.store(obj, key, decode_primitive(rest, line.number), line)

    def store(self, obj, key, value, line):
        if key in obj and self.strict:
            raise ToonDecodeError(f"duplicate key {key!r}", line.number)
        obj[key] = value

    def nested_object(self, depth, content_depth, line):
        following = self.peek_content()
        if following is None or following.depth <= depth:
            return {}
        if following.depth > content_depth:
            if self.strict:
                raise ToonDecodeError("indentation jumps more than one level", following.number)
            return self.object(following.depth, content_depth - 1)
        return self.object(content_depth, content_depth - 1)

    # ---- arrays (spec 9)

    def array_body(self, header, line, content_depth):
        if header.keyed:
            return self.keyed_rows(header, content_depth)
        if header.fields is not None:
            return self.tabular_rows(header, content_depth)
        if header.rest != "":
            values = [decode_primitive(cell, line.number) for cell in split_cells(header.rest, header.delimiter)]
            if self.strict and len(values) != header.length:
                raise ToonDecodeError(f"expected {header.length} values, found {len(values)}", line.number)
            return values
        return self.list_items(header, content_depth)

    def scope_lines(self, content_depth, kind):
        """Yield the content lines of a header scope whose items stand at `content_depth`,
        applying the depth-jump recovery of spec 14.4 in non-strict mode."""
        actual = None
        end_depth = content_depth - 1
        self.spans.append(None)  # The header span opens with its first item, row, or entry.
        try:
            while True:
                line = self.current()
                if line is None:
                    return
                if line.blank:
                    self.skip_blank(line)
                    continue
                if line.depth <= end_depth:
                    return
                if actual is None:
                    if line.depth > content_depth:
                        if self.strict:
                            raise ToonDecodeError("indentation jumps more than one level", line.number)
                        actual = line.depth
                    else:
                        actual = content_depth
                    self.spans[-1] = actual
                if line.depth < actual:
                    raise ToonDecodeError(f"{kind} line is shallower than its scope", line.number)
                if line.depth > actual:
                    raise ToonDecodeError(f"{kind} line is indented deeper than its scope", line.number)
                yield line
        finally:
            self.spans.pop()

    def list_items(self, header, content_depth):
        items = []
        for line in self.scope_lines(content_depth, "list item"):
            content = line.text
            if content == "-":
                remainder = ""
            elif content.startswith("- "):
                remainder = content[2:].lstrip(" ")
            else:
                break  # The scope ends; the enclosing scope decides whether the line is valid.
            self.position += 1
            item_depth = self.spans[-1] if self.spans[-1] is not None else content_depth
            if content == "-":
                items.append({})
            elif remainder == "[]":
                items.append([])
            elif remainder.startswith("[") and is_header_line(remainder):
                inner = parse_header(remainder, line.number, self.strict)
                if inner.fields is not None:
                    raise ToonDecodeError("a keyless header with a field list is valid only at the root", line.number)
                items.append(self.array_body(inner, line, item_depth + 1))
            elif is_header_line(remainder) or first_unquoted(remainder, ":") >= 0:
                obj = {}
                self.field(obj, remainder, line, item_depth + 1, item_depth + 2)
                items.append(self.object(item_depth + 1, item_depth, obj))
            else:
                items.append(decode_primitive(remainder, line.number))
        if self.strict and len(items) != header.length:
            raise ToonDecodeError(f"expected {header.length} list items, found {len(items)}", header_line_number(self))
        return items

    def tabular_rows(self, header, content_depth):
        rows = []
        width = leaf_count(header.fields)
        for line in self.scope_lines(content_depth, "row"):
            content = line.text
            delimiter_at = first_unquoted(content, header.delimiter)
            colon_at = first_unquoted(content, ":")
            if colon_at >= 0 and (delimiter_at < 0 or colon_at < delimiter_at):
                break  # Not a row: the enclosing scope decides what the line is.
            self.position += 1
            cells = split_cells(content, header.delimiter)
            if len(cells) != width:
                raise ToonDecodeError(f"row has {len(cells)} cells, expected {width}", line.number)
            rows.append(self.row_object(header.fields, iter(cells), line.number))
        if self.strict and len(rows) != header.length:
            raise ToonDecodeError(f"expected {header.length} rows, found {len(rows)}", header_line_number(self))
        return rows

    def row_object(self, fields, cells, number):
        obj = {}
        for name, nested in fields:
            obj[name] = decode_primitive(next(cells), number) if nested is None else self.row_object(nested, cells, number)
        return obj

    def keyed_rows(self, header, content_depth):
        obj = {}
        width = leaf_count(header.fields)
        count = 0
        for line in self.scope_lines(content_depth, "entry row"):
            content = line.text
            colon = first_unquoted(content, ":")
            if colon < 0:
                raise ToonDecodeError("expected an entry row with a key", line.number)
            self.position += 1
            key = decode_key(content[:colon], line.number)
            cells = split_cells(content[colon + 1:], header.delimiter)
            if len(cells) != width:
                raise ToonDecodeError(f"entry row has {len(cells)} cells, expected {width}", line.number)
            self.store(obj, key, self.row_object(header.fields, iter(cells), line.number), line)
            count += 1
        if self.strict and count != header.length:
            raise ToonDecodeError(f"expected {header.length} entry rows, found {count}", header_line_number(self))
        return obj


def header_line_number(decoder):
    index = decoder.position - 1
    while index >= 0 and decoder.lines[index].blank:
        index -= 1
    return decoder.lines[index].number if index >= 0 else None


def loads(text, indent=2, strict=True):
    """Decode TOON text (or UTF-8 bytes) into Python values."""
    return Decoder(indent, strict).decode(text)


# ---------------------------------------------------------------- helpers for CLI views

def uniform(rows, keys=None, fill=None):
    """Give every object in `rows` the same key order and set (missing keys get `fill`), so the
    array takes tabular form. `keys` fixes the column order; otherwise it is the union in
    first-seen order. Non-object rows are returned unchanged."""
    if not rows or not all(isinstance(row, dict) for row in rows):
        return rows
    if keys is None:
        keys = []
        for row in rows:
            for key in row:
                if key not in keys:
                    keys.append(key)
    return [{key: row.get(key, fill) for key in keys} for row in rows]


def truncate(value, width, marker=None, keep=()):
    """Bound every string in `value` to `width` characters, marking the cut. The default marker
    says how much was cut (`…[+42]`), which real text ending in "..." can never be mistaken
    for; an explicit `marker` string is used as given. Strings under a key in `keep`
    (identifiers, paths, SHAs) are never cut: a shortened id cannot be used."""
    if width is None or width <= 0:
        return value
    if isinstance(value, str):
        return value if len(value) <= width else cut_text(value, width, marker)
    if isinstance(value, dict):
        return {key: item if key in keep else truncate(item, width, marker, keep) for key, item in value.items()}
    if isinstance(value, list):
        return [truncate(item, width, marker, keep) for item in value]
    return value


def cut_text(text, width, marker=None):
    if marker is not None:
        return text[:max(0, width - len(marker))] + marker
    kept = width
    for _ in range(3):  # The count's own digits shorten what fits; settle in a pass or two.
        suffix = f"\u2026[+{len(text) - kept}]"
        kept = max(0, width - len(suffix))
    return text[:kept] + f"\u2026[+{len(text) - kept}]"


def flatten(row, separator="."):
    """One level of nested objects becomes `parent.child` columns, so rows that carry a small
    object (a file's `stats`) can still share one table header."""
    flat = {}
    for key, value in row.items():
        clash = isinstance(value, dict) and any(f"{key}{separator}{child}" in row for child in value)
        if isinstance(value, dict) and value and not clash and all(is_primitive(item) for item in value.values()):
            for child, item in value.items():
                flat[f"{key}{separator}{child}"] = item
        else:
            flat[key] = value
    return flat


def compact_rows(rows, delimiter=","):
    """The shortest TOON form of an array of objects: as given, made uniform (one header,
    missing cells null), or made uniform after flattening nested objects. Padding mixed rows
    with nulls can cost more than it saves, so each candidate is measured, never assumed."""
    if not rows or not all(isinstance(row, dict) for row in rows):
        return rows
    candidates = [rows, uniform(rows)]
    flattened = [flatten(row) for row in rows]
    if flattened != rows:
        candidates.append(uniform(flattened))
    return min(candidates, key=lambda candidate: (len(dumps({"x": candidate}, delimiter=delimiter)),
                                                  candidates.index(candidate)))


FORMATS = ("toon", "json")


def default_format(environ=None, warn=None):
    """`IMPROVE_FORMAT`, normalized (`JSON`, ` json ` work); an unknown value warns and means toon."""
    environ = os.environ if environ is None else environ
    value = str(environ.get("IMPROVE_FORMAT", "toon")).strip().lower() or "toon"
    if value not in FORMATS:
        message = f"IMPROVE_FORMAT={value!r} is not one of {', '.join(FORMATS)}; using toon"
        (warn or (lambda text: print(text, file=sys.stderr)))(message)
        return "toon"
    return value


def output_options(parser, suppress=False, delimiter=True, text=True, width=None):
    """The output switches every helper shares. Added to the top-level parser with real
    defaults and to each subcommand with `suppress`, so `helper --format json stats` and
    `helper stats --format json` mean the same thing (agents put flags at the end)."""
    import argparse
    blank = argparse.SUPPRESS

    def default(value):
        return blank if suppress else value

    parser.add_argument("--format", choices=FORMATS, type=str.lower, default=default(None) if not suppress else blank,
                        help="structured output format (default toon; IMPROVE_FORMAT overrides)")
    if delimiter:
        parser.add_argument("--delimiter", choices=("comma", "tab", "pipe"), default=default("comma"),
                            help="TOON cell delimiter")
    if text:
        parser.add_argument("--width", type=int, default=default(width),
                            help="bound long text cells to this many characters (0 for no bound)")
        parser.add_argument("--full", action="store_true", default=default(False), help="every field, untruncated")


def resolve_format(args):
    """The format chosen on the command line, else IMPROVE_FORMAT, else toon."""
    chosen = getattr(args, "format", None)
    args.format = chosen if chosen in FORMATS else default_format()
    return args.format


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="Convert between JSON and TOON (specification 4.4).")
    parser.add_argument("action", choices=("encode", "decode"), help="encode JSON from stdin to TOON, or decode TOON to JSON")
    parser.add_argument("--delimiter", default="comma", choices=sorted(DELIMITERS))
    parser.add_argument("--indent", type=int, default=2)
    parser.add_argument("--lenient", action="store_true", help="decode with strict=false (spec 14.4 recoveries)")
    args = parser.parse_args(argv)
    data = sys.stdin.read()
    try:
        if args.action == "encode":
            print(dumps(json.loads(data), indent=args.indent, delimiter=args.delimiter))
        else:
            print(json.dumps(loads(data, indent=args.indent, strict=not args.lenient), ensure_ascii=False))
        return 0
    except (ToonError, ValueError) as exc:
        print(f"improve-toon: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
