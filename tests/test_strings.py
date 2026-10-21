"""Change 10: the strings table and its use (contract 10 §8 tests 1-12, §12).

The checkers are plain functions that return a list of problems, so
CheckerTests can run them against small fixtures. The table tests run them
against app/static/strings-en.js, app.js and index.html, and fail (not skip)
when a file is missing.

Exemptions: a line carrying `// i18n-ok <reason>` is exempt from test 9 (hard-coded
text) only; list them with `grep -n 'i18n-ok' app/static/app.js`. Tests 11
(escaping) and 12 (shadowing) honour no exemptions.
"""

import bisect
import html
import json
import re
import unittest
from html.parser import HTMLParser
from pathlib import Path

from tests.helpers import APP_DIR, ServerTestCase, auth, passports, server

import countries  # noqa: E402  (app/ is on sys.path via tests.helpers)
import errors  # noqa: E402

STATIC = APP_DIR / "static"
STRINGS_PATH = STATIC / "strings-en.js"
APP_JS_PATH = STATIC / "app.js"
INDEX_PATH = STATIC / "index.html"

FIRST_LINE = "'use strict';"
SECOND_LINE = "self.WORLD_EATS_STRINGS = {"
LAST_LINE = "};"
KEY_RE = re.compile(r"[a-z][a-z0-9]*(\.[A-Za-z0-9_]+)+")
LANG_RE = re.compile(r"[a-z]{2,3}(-[A-Za-z0-9]{2,8})*")
PLURAL_FORMS = frozenset({"zero", "one", "two", "few", "many", "other"})
TOKEN_RE = re.compile(r"\{\{|\}\}|\{([a-z][a-zA-Z0-9]*)\}")  # the t() / errors.render grammar
DYNAMIC_PREFIXES = ("error.", "country.", "board.short.")
EXEMPT_RE = re.compile(r"//\s*i18n-ok\s+\S")
# A word of 2+ letters followed by more words of any length ("Log a meal", "Not found").
WORDS_RE = re.compile(r"[^\W\d_]{2,}(?: [^\W\d_]+)+")
# Text sinks: (call pattern, index of the text argument).
SINKS = ((re.compile(r"(?<![\w$.])toast\("), 0), (re.compile(r"(?<![\w$.])fail\("), 0),
         (re.compile(r"(?<![\w$.])textEl\("), 1), (re.compile(r"(?<![\w$.])setError\("), 1),
         (re.compile(r"\bnew\s+Option\("), 0))
TEXT_CONTENT_RE = re.compile(r"\.textContent\s*=\s*")
I18N_ATTR_PREFIX = "data-i18n-"
BOARD_LETTERS = " ABCDEFGHIJKLMNOPQRSTUVWXYZ"
BOARD_SHORT = {"CF": "Central African Rep.", "VC": "St Vincent & Grenadines"}


# --- Test 1: the file format (§2) ---

class Duplicate(ValueError):
    pass


def _no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Duplicate(f"duplicate key {key!r}")
        result[key] = value
    return result


def _no_constants(name):
    raise ValueError(f"{name} is not JSON")


def parse_strings(text):
    """(table, problems). The table is None when the wrapper or the JSON is broken."""
    lines = text.split("\n")
    problems = []
    if lines[0] != FIRST_LINE:
        problems.append(f"line 1 must be exactly {FIRST_LINE!r}")
    if len(lines) < 2 or lines[1] != SECOND_LINE:
        problems.append(f"line 2 must be exactly {SECOND_LINE!r}")
    last = max((i for i, line in enumerate(lines) if line.strip()), default=0)
    if last < 2 or lines[last] != LAST_LINE:
        problems.append(f"the last non-empty line must be exactly {LAST_LINE!r} with nothing after it")
    if problems:
        return None, problems
    try:
        table = json.loads("{\n" + "\n".join(lines[2:last]) + "\n}", object_pairs_hook=_no_duplicates,
                           parse_constant=_no_constants)
    except ValueError as e:  # includes Duplicate and JSONDecodeError
        return None, [f"not strict JSON: {e}"]
    for key, value in table.items():
        if not KEY_RE.fullmatch(key):
            problems.append(f"bad key {key!r}")
        if isinstance(value, dict):
            if "other" not in value or not set(value) <= PLURAL_FORMS:
                problems.append(f"{key}: a plural needs 'other' and only {sorted(PLURAL_FORMS)}")
            if not all(isinstance(form, str) for form in value.values()):
                problems.append(f"{key}: plural forms must be strings")
        elif not isinstance(value, str):
            problems.append(f"{key}: a value must be a string or a plural object")
    lang = table.get("meta.lang")
    if lang != "en" or not LANG_RE.fullmatch(lang):
        problems.append(f"meta.lang must be 'en', not {lang!r}")
    return table, problems


def texts(table):
    """(key, text) for every string, including each plural form."""
    for key, value in table.items():
        if isinstance(value, dict):
            for form, text in value.items():
                if isinstance(text, str):
                    yield f"{key}[{form}]", text
        elif isinstance(value, str):
            yield key, value


# --- Test 2: placeholders ---

def check_placeholders(table):
    problems = []
    for key, text in texts(table):
        rest = TOKEN_RE.sub("", text)
        if "{" in rest or "}" in rest:
            problems.append(f"{key}: bad placeholder or unbalanced brace in {text!r}")
    return problems


# --- A small JavaScript lexer for app.js (strings, templates, comments, regex literals) ---

class Literal:
    """A string literal, or one static part of a template literal.

    start: offset of the opening quote (for a template part, the backtick or the
    `}` that ends the previous expression); value: the decoded text; whole: True
    for a quoted string or a template with no expressions.
    """

    def __init__(self, kind, start, end, value, whole):
        self.kind, self.start, self.end, self.value, self.whole = kind, start, end, value, whole


WORD_RE = re.compile(r"[\w$]+")
ESCAPES = {"n": " ", "t": " ", "r": " ", "b": "", "f": "", "v": "", "0": ""}
REGEX_BEFORE = set("(,=:[!&|?{};+-*%<>~^")
REGEX_KEYWORDS = {"return", "typeof", "case", "do", "else", "in", "of", "new", "delete", "void", "throw",
                  "yield", "await", "instanceof"}


class JsSource:
    """`code` is the source with comment, string, template-text and regex contents blanked
    (same length, newlines kept, quotes, backticks, `${` and `}` kept)."""

    def __init__(self, src):
        self.src = src
        self.literals = []
        self.expressions = []  # (start, end) of each ${...} expression
        self.newlines = [i for i, ch in enumerate(src) if ch == "\n"]
        self.code = self._lex()
        self.code_lines = self.code.split("\n")
        self.src_lines = src.split("\n")
        self.by_start = {lit.start: lit for lit in self.literals}

    def line_of(self, offset):
        return bisect.bisect_left(self.newlines, offset) + 1

    def exempt(self, *lines):
        return any(EXEMPT_RE.search(self.src_lines[n - 1]) for n in lines)

    def _lex(self):
        src, n = self.src, len(self.src)
        out = list(src)
        stack = []  # one brace depth per open template expression
        prev = ""  # last significant code character or word
        i = 0

        def blank(a, b):
            for k in range(a, b):
                if out[k] != "\n":
                    out[k] = " "

        while i < n:
            ch = src[i]
            if ch in " \t\r\n":
                i += 1
                continue
            if src.startswith("//", i):
                j = src.find("\n", i)
                j = n if j < 0 else j
                blank(i, j)
                i = j
                continue
            if src.startswith("/*", i):
                j = src.find("*/", i + 2)
                j = n if j < 0 else j + 2
                blank(i, j)
                i = j
                continue
            if ch in "'\"":
                j, value = self._string(i, ch)
                self.literals.append(Literal("string", i, j, value, True))
                blank(i + 1, j - 1)
                prev, i = "x", j
                continue
            if ch == "`" or (ch == "}" and stack and stack[-1] == 0):
                if ch == "}":
                    stack.pop()
                    self.expressions[-1] = (self.expressions[-1][0], i)
                j, value, opens = self._template_part(i)
                self.literals.append(Literal("template", i, j, value, ch == "`" and not opens))
                blank(i + 1, j - (2 if opens else 1))
                if opens:
                    stack.append(0)
                    self.expressions.append((j, None))
                    prev = "("
                else:
                    prev = "x"
                i = j
                continue
            if ch == "/" and (prev == "" or prev in REGEX_BEFORE or prev in REGEX_KEYWORDS):
                j = self._regex(i)
                if j is not None:
                    blank(i + 1, j)
                    prev, i = "x", j
                    continue
            if ch == "{" and stack:
                stack[-1] += 1
            elif ch == "}" and stack:
                stack[-1] -= 1
            m = WORD_RE.match(src, i)
            if m:
                prev, i = m.group(0), m.end()
                continue
            prev = ch
            i += 1
        return "".join(out)

    def _string(self, i, quote):
        src, j, value = self.src, i + 1, []
        while j < len(self.src):
            ch = src[j]
            if ch == "\\":
                text, j = self._escape(j)
                value.append(text)
                continue
            if ch == quote:
                return j + 1, "".join(value)
            if ch == "\n":
                break
            value.append(ch)
            j += 1
        raise ValueError(f"unterminated string at line {self.line_of(i)}")

    def _escape(self, j):
        """The text of the escape at src[j] == '\\' and the offset after it."""
        src = self.src
        nxt = src[j + 1:j + 2]
        for pattern in (r"u([0-9A-Fa-f]{4})", r"u\{([0-9A-Fa-f]{1,6})\}", r"x([0-9A-Fa-f]{2})"):
            m = re.compile(pattern).match(src, j + 1)
            if m:
                return chr(min(int(m.group(1), 16), 0x10FFFF)), m.end()
        if nxt == "\n":
            return "", j + 2  # line continuation
        return ESCAPES.get(nxt, nxt), j + 2

    def _template_part(self, i):
        """From a backtick or an expression's `}`: (end, text, opens an expression)."""
        src, j, value = self.src, i + 1, []
        while j < len(src):
            ch = src[j]
            if ch == "\\":
                text, j = self._escape(j)
                value.append(text)
                continue
            if ch == "`":
                return j + 1, "".join(value), False
            if src.startswith("${", j):
                return j + 2, "".join(value), True
            value.append(ch)
            j += 1
        raise ValueError(f"unterminated template at line {self.line_of(i)}")

    def _regex(self, i):
        src, j, in_class = self.src, i + 1, False
        if src[j:j + 1] in ("/", "*", ""):
            return None
        while j < len(src):
            ch = src[j]
            if ch == "\n":
                return None
            if ch == "\\":
                j += 2
                continue
            if ch == "[":
                in_class = True
            elif ch == "]":
                in_class = False
            elif ch == "/" and not in_class:
                j += 1
                while j < len(src) and src[j].isalpha():
                    j += 1
                return j
            j += 1
        return None


CALL_RE = re.compile(r"(?<![\w$.])(tn?)\(\s*")


def literal_calls(js):
    """(function, key, line) for every t('…') / tn('…') call whose key is a literal."""
    return scan_calls(js)[0]


def scan_calls(js):
    """(literal calls, problems): every t()/tn() call other than the two declarations needs a
    plain literal key (no escapes, no template expressions)."""
    calls, problems = [], []
    for m in CALL_RE.finditer(js.code):
        line = js.line_of(m.start())
        if re.search(r"\bfunction\s+$", js.code[max(0, m.start() - 20):m.start()]):
            continue
        lit = js.by_start.get(m.end())
        after = js.code[lit.end:].lstrip()[:1] if lit is not None else ""
        if lit is not None and lit.whole and "\\" not in js.src[lit.start:lit.end] and after in (",", ")"):
            calls.append((m.group(1), lit.value, line))
        else:
            problems.append(f"app.js:{line}: {m.group(1)}() without a literal key")
    return calls, problems


# --- index.html: data-i18n elements and attributes ---

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class I18nParser(HTMLParser):
    """Collects (kind, key, english, line) per data-i18n* use, and problems."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.uses = []
        self.problems = []
        self.open = None  # [tag, key, text parts, line, depth]
        self.script_srcs = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        line = self.getpos()[0]
        if tag == "script" and "src" in attrs:
            self.script_srcs.append((attrs["src"], "defer" in attrs))
        if self.open is not None:
            if self.open[0] == tag and tag not in VOID:
                self.open[4] += 1
            self.problems.append(f"line {line}: <{tag}> inside a data-i18n element (line {self.open[3]})")
        for name, key in attrs.items():
            if name.startswith(I18N_ATTR_PREFIX):
                target = name[len(I18N_ATTR_PREFIX):]
                value = attrs.get(target)
                if not value:
                    self.problems.append(f"line {line}: {name} but no {target} text")
                else:
                    if not key:
                        self.problems.append(f"line {line}: empty {name}")
                    self.uses.append((name, key or "", value, line))
        if "data-i18n" in attrs:
            if tag in VOID:
                self.problems.append(f"line {line}: data-i18n on a void <{tag}>")
            elif self.open is None:
                if not attrs["data-i18n"]:
                    self.problems.append(f"line {line}: empty data-i18n")
                self.open = [tag, attrs["data-i18n"] or "", [], line, 0]

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if self.open is None or tag != self.open[0]:
            return
        if self.open[4]:
            self.open[4] -= 1
            return
        _, key, parts, line, _ = self.open
        self.uses.append(("data-i18n", key, "".join(parts), line))
        self.open = None

    def handle_data(self, data):
        if self.open is not None:
            self.open[2].append(data)


def parse_index(text):
    parser = I18nParser()
    parser.feed(text)
    parser.close()
    if parser.open is not None:
        parser.problems.append(f"line {parser.open[3]}: data-i18n element never closed")
    return parser


def collapse(text):
    return " ".join(text.split())


# --- Tests 3-8: keys against the table ---

def check_key_types(table, js, index):
    """Test 3: every literal key exists, with the right type."""
    calls, problems = scan_calls(js)
    problems += [f"index.html: {p}" for p in index.problems if "empty data-i18n" in p]
    for func, key, line in calls:
        value = table.get(key)
        if func == "tn" and not isinstance(value, dict):
            problems.append(f"app.js:{line}: tn('{key}') needs a plural object")
        elif func == "t" and not isinstance(value, str):
            problems.append(f"app.js:{line}: t('{key}') needs a string")
    for kind, key, _, line in index.uses:
        if not isinstance(table.get(key), str):
            problems.append(f"index.html:{line}: {kind}=\"{key}\" needs a string")
    return problems


def check_unused(table, js, index):
    """Test 4: every key is used literally, has an allowed dynamic prefix, or is meta.*."""
    used = {lit.value for lit in js.literals if lit.whole} | {key for _, key, _, _ in index.uses}
    return [f"unused key {key}" for key in table
            if key not in used and not key.startswith(DYNAMIC_PREFIXES) and not key.startswith("meta.")]


def prefixed(table, prefix):
    return {key[len(prefix):]: value for key, value in table.items() if key.startswith(prefix)}


def check_html_text(table, index):
    """Test 8: the English in index.html equals the table (entities decoded, whitespace collapsed)."""
    problems = list(index.problems)
    for kind, key, english, line in index.uses:
        value = table.get(key)
        if isinstance(value, str) and collapse(english) != collapse(value):
            problems.append(f"index.html:{line}: {kind}=\"{key}\" is {collapse(english)!r}, table {value!r}")
    return problems


# --- Tests 9, 11, 12: app.js code checks ---

def _argument_starts(code, open_at):
    """Offsets where each depth-0 argument starts, for the call whose '(' is at code[open_at]."""
    starts, depth, k = [open_at + 1], 0, open_at
    while k < len(code):
        ch = code[k]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                break
        elif ch == "," and depth == 1:
            starts.append(k + 1)
        k += 1
    return starts


def _literal_at(js, offset):
    while offset < len(js.code) and js.code[offset] in " \t\n":
        offset += 1
    return js.by_start.get(offset)


def _capitalised(text):
    """Starts like \\p{Lu}\\p{Ll}+ ("Removed", "Log in")."""
    return len(text) >= 2 and text[0].isupper() and text[1].islower()


def check_hard_coded(js):
    """Test 9: no literal with a run of words (first one 2+ letters), and no capitalised literal
    given straight to a text sink; lines with `// i18n-ok <reason>` are exempt."""
    problems = []

    def flag(lit, why=""):
        first, last = js.line_of(lit.start), js.line_of(max(lit.start, lit.end - 1))
        if not js.exempt(first, last):
            problems.append(f"app.js:{first}: {why}{lit.value[:60]!r}")

    flagged = set()
    for lit in js.literals:
        if lit.value == "use strict":
            continue  # the directive, not text
        if WORDS_RE.search(lit.value):
            flagged.add(lit.start)
            flag(lit)
    sink_literals = []
    for pattern, index in SINKS:
        for m in pattern.finditer(js.code):
            starts = _argument_starts(js.code, m.end() - 1)
            if index < len(starts):
                sink_literals.append(_literal_at(js, starts[index]))
    sink_literals += [_literal_at(js, m.end()) for m in TEXT_CONTENT_RE.finditer(js.code)]
    for lit in sink_literals:
        if lit is not None and lit.start not in flagged and _capitalised(lit.value):
            flagged.add(lit.start)
            flag(lit, "text sink: ")
    return sorted(problems, key=lambda p: int(p.split(":")[1]))


def statements(js):
    """(start, end) offsets of rough statements: split at `;` and at line ends after `{` or `}`."""
    code, spans, start = js.code, [], 0
    for k, ch in enumerate(code):
        if ch == ";" or (ch == "\n" and code[start:k].rstrip().endswith(("{", "}"))):
            spans.append((start, k))
            start = k + 1
    spans.append((start, len(code)))
    return spans


PLUS_RE = re.compile(r"(?<!\+)\+(?!\+)")


def check_escaping(js):
    """Test 11 (review M1): t()/tn() results reach innerHTML templates only as ${esc(t(…))}, and are
    never +-concatenated into HTML. No exemptions."""
    problems = []
    for number, line in enumerate(js.src_lines, 1):
        if re.search(r"\$\{\s*tn?\(", line):
            problems.append(f"app.js:{number}: ${{t(…)}} without esc()")
    # Variables that are later assigned to innerHTML (`el.innerHTML = html;`).
    html_vars = set(re.findall(r"\.innerHTML\s*\+?=\s*([A-Za-z_$][\w$]*)\s*(?:;|$)", js.code, re.M))
    def in_expression(offset):
        return any(a <= offset < (b or len(js.code)) for a, b in js.expressions)

    for start, end in statements(js):
        code = js.code[start:end]
        # Calls inside ${…} are checked below (they must be esc()-wrapped there).
        bare = [m for m in CALL_RE.finditer(js.code, start, end) if not in_expression(m.start())]
        if not bare or not PLUS_RE.search(code):
            continue
        markup = "innerHTML" in code or any(
            "<" in lit.value for lit in js.literals if start <= lit.start < end)
        target = re.match(r"\s*(?:(?:const|let|var)\s+)?([A-Za-z_$][\w$]*)\s*\+?=(?!=)", code)
        if markup or (target and target.group(1) in html_vars):
            problems.append(f"app.js:{js.line_of(start + len(code) - len(code.lstrip()))}: "
                            "t()/tn() in a +-concatenated HTML statement")
    for start, end in js.expressions:
        if end is None:
            continue
        for m in CALL_RE.finditer(js.code, start, end):
            if not re.search(r"\besc\(\s*$", js.code[start:m.start()]):
                problems.append(f"app.js:{js.line_of(m.start())}: t()/tn() inside ${{…}} not directly wrapped in esc()")
    return problems


# A method definition: name(params) { after a line start, {, }, ; or a comma.
METHOD_RE = re.compile(r"(?:^|[{},;])[ \t]*(?:(?:async|static|get|set)\s+)*"
                       r"(?!(?:if|for|while|switch|catch|function|return|with)\b)[\w$]+\s*\(", re.M)


def _split_top(text):
    """Splits at depth-0 commas."""
    parts, depth, current = [], 0, []
    for ch in text:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return parts


def _binds_t(target):
    """True if a binding target (`t`, `{ a: t }`, `[t]`, `t = 1`, `...t`) binds t or tn."""
    target = target.strip()
    depth, cut = 0, len(target)
    for k, ch in enumerate(target):  # drop a default value / initialiser at depth 0
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "=" and depth == 0 and target[k + 1:k + 2] not in ("=", ">"):
            cut = k
            break
    target = target[:cut].strip().lstrip(".").strip()
    if target and target[0] in "{[":
        inner = target[1:-1]
        return any(_binds_t(part.split(":", 1)[1] if ":" in part.split("=", 1)[0] else part)
                   for part in _split_top(inner))
    return re.fullmatch(r"tn?", target) is not None


def _balanced(code, open_at):
    """The text inside the parentheses starting at code[open_at] == '(' and the index after ')'."""
    depth = 0
    for k in range(open_at, len(code)):
        if code[k] == "(":
            depth += 1
        elif code[k] == ")":
            depth -= 1
            if depth == 0:
                return code[open_at + 1:k], k + 1
    return code[open_at + 1:], len(code)


def _declarators(code, start):
    """The declarators after `const|let|var` at `start`, up to `;`, a depth-0 newline, or a closing bracket."""
    depth, k = 0, start
    while k < len(code):
        ch = code[k]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth < 0:
                break
        elif ch == ";" and depth == 0:
            break
        elif ch == "\n" and depth == 0 and not code[start:k].rstrip().endswith((",", "=")):
            break
        k += 1
    for part in _split_top(code[start:k]):
        part = re.sub(r"\s+(?:of|in)\s.*$", "", part, flags=re.S)
        yield part


def check_shadowing(js):
    """Test 12 (review M2): no binding named t or tn other than the two function declarations.
    No exemptions."""
    code, problems = js.code, []

    def report(offset, what):
        problems.append(f"app.js:{js.line_of(offset)}: {what}")

    declared = {}
    for m in re.finditer(r"\b(?:const|let|var)\b", code):
        for part in _declarators(code, m.end()):
            if _binds_t(part):
                report(m.start(), f"variable named t/tn: {part.strip()[:40]!r}")
    for m in re.finditer(r"\bfunction\b\s*\*?\s*([\w$]*)\s*\(", code):
        if m.group(1) in ("t", "tn"):
            declared[m.group(1)] = declared.get(m.group(1), 0) + 1
            if declared[m.group(1)] > 1:
                report(m.start(), f"second function {m.group(1)}")
        params, _ = _balanced(code, m.end() - 1)
        if any(_binds_t(p) for p in _split_top(params)):
            report(m.start(), "parameter named t/tn")
    for m in re.finditer(r"=>", code):
        k = m.start() - 1
        while k >= 0 and code[k] in " \t\n":
            k -= 1
        if k >= 0 and code[k] == ")":
            depth, j = 0, k
            while j >= 0:
                depth += code[j] == ")"
                depth -= code[j] == "("
                if depth == 0:
                    break
                j -= 1
            if any(_binds_t(p) for p in _split_top(code[j + 1:k])):
                report(m.start(), "arrow parameter named t/tn")
        else:
            j = k
            while j >= 0 and (code[j].isalnum() or code[j] in "_$"):
                j -= 1
            if code[j + 1:k + 1] in ("t", "tn") and (j < 0 or code[j] != "."):
                report(m.start(), "arrow parameter named t/tn")
    for m in re.finditer(r"\bcatch\s*\(", code):
        params, _ = _balanced(code, m.end() - 1)
        if _binds_t(params):
            report(m.start(), "catch binding named t/tn")
    for m in METHOD_RE.finditer(code):
        params, after = _balanced(code, m.end() - 1)
        if re.match(r"\s*\{", code[after:]) and any(_binds_t(p) for p in _split_top(params)):
            report(m.start(), "method parameter named t/tn")
    return problems


def check_script_order(index):
    """§2: /strings-en.js is loaded with defer, before /app.js."""
    srcs = [src for src, _ in index.script_srcs]
    if "/strings-en.js" not in srcs or "/app.js" not in srcs:
        return ["index.html must load /strings-en.js and /app.js"]
    problems = []
    if srcs.index("/strings-en.js") > srcs.index("/app.js"):
        problems.append("/strings-en.js must come before /app.js")
    if not dict(index.script_srcs)["/strings-en.js"]:
        problems.append("/strings-en.js must be deferred")
    return problems


# --- The real files ---

def read_required(path):
    if not path.exists():
        raise AssertionError(f"{path.relative_to(APP_DIR.parent)} is missing (contract 10 §2: written by the frontend)")
    return path.read_text(encoding="utf-8")


class TableTests(unittest.TestCase):
    """Tests 1-9, 11, 12 against app/static."""

    @classmethod
    def setUpClass(cls):
        cls._table = cls._table_problems = None
        if STRINGS_PATH.exists():
            cls._table, cls._table_problems = parse_strings(STRINGS_PATH.read_text(encoding="utf-8"))

    def table(self):
        read_required(STRINGS_PATH)
        self.assertIsNotNone(self._table, self._table_problems)
        return self._table

    def js(self):
        return JsSource(read_required(APP_JS_PATH))

    def index(self):
        return parse_index(read_required(INDEX_PATH))

    def assertNoProblems(self, problems):
        self.assertEqual(problems, [], f"{len(problems)} problem(s):\n" + "\n".join(problems[:50]))

    def test_1_file_format(self):
        _, problems = parse_strings(read_required(STRINGS_PATH))
        self.assertNoProblems(problems)

    def test_2_placeholders(self):
        self.assertNoProblems(check_placeholders(self.table()))

    def test_3_literal_keys_exist(self):
        self.assertNoProblems(check_key_types(self.table(), self.js(), self.index()))

    def test_4_no_unused_keys(self):
        self.assertNoProblems(check_unused(self.table(), self.js(), self.index()))

    def test_5_error_keys_equal_messages(self):
        self.assertEqual(prefixed(self.table(), "error."), errors.MESSAGES)

    def test_6_country_keys_equal_countries(self):
        self.assertEqual(prefixed(self.table(), "country."), {c.code: c.name for c in countries.COUNTRIES})

    def test_7_board(self):
        table = self.table()
        short = prefixed(table, "board.short.")
        self.assertTrue(set(short) <= set(countries.BY_CODE), sorted(set(short) - set(countries.BY_CODE)))
        self.assertEqual(short, BOARD_SHORT)
        self.assertEqual(table.get("board.letters"), BOARD_LETTERS)

    def test_8_html_text_equals_table(self):
        self.assertNoProblems(check_html_text(self.table(), self.index()))

    def test_hint_numbers_match_server_constants(self):
        # Hints state limits the server enforces; every number in them must come from its constant.
        table = self.table()
        expected = {
            "hint.username": [auth.USERNAME_MIN, auth.USERNAME_MAX],
            "hint.password": [auth.PASSWORD_MIN, auth.PASSWORD_MAX],
            "passports.new_hint": [passports.MAX_PEOPLE - 1],  # everyone but the owner
            "admin.temp_hint": [auth.PASSWORD_MIN, auth.PASSWORD_MAX, server.TEMP_PASSWORD_TTL // 86400],
        }
        self.assertEqual(server.TEMP_PASSWORD_TTL % 86400, 0)
        for key, numbers in expected.items():
            with self.subTest(key=key):
                self.assertIsInstance(table.get(key), str)
                self.assertEqual([int(n) for n in re.findall(r"[0-9]+", table[key])], numbers, table[key])

    def test_8_script_order(self):
        self.assertNoProblems(check_script_order(self.index()))

    def test_9_no_hard_coded_text(self):
        self.assertNoProblems(check_hard_coded(self.js()))

    def test_11_escaping(self):
        self.assertNoProblems(check_escaping(self.js()))

    def test_12_no_shadowing(self):
        self.assertNoProblems(check_shadowing(self.js()))


# --- Test 10: server responses ---

class ResponseTests(ServerTestCase):
    def assert_coded(self, resp):
        self.assertGreaterEqual(resp.status, 400)
        body = resp.json()
        self.assertTrue(errors.CODE_RE.fullmatch(body["code"]), body)
        params = body.get("params")
        if params is not None:
            self.assertTrue(params)
            self.assertTrue(all(type(v) is int for v in params.values()), params)
        self.assertEqual(body["error"], errors.render(body["code"], params))

    def test_10_sample_of_routes(self):
        token = self.setup_admin()
        samples = [
            self.request("GET", "/api/passports"),
            self.request("GET", "/api/passports/999", token=token),
            self.request("GET", "/api/passports/1/countries/ZZ", token=token),
            self.request("POST", "/api/passports", {"name": ""}, token=token),
            self.request("POST", "/api/passports/1/countries/PE/meals", {"eaten_on": "1800-01-01"}, token=token),
            self.request("POST", "/api/passports/1/countries/PE/meals", {"eaten_on": "2026-01-01", "notes": "x" * 2001},
                         token=token),
            self.request("POST", "/api/passports/1/search", {"q": "x" * 201}, token=token),
            self.request("POST", "/api/admin/users", {"username": "x"}, token=token),
            self.request("POST", "/api/login", {"username": "admin", "password": "nope"}),
            self.request("POST", "/api/logout", origin="http://evil.example"),
            self.request("GET", "/nope"),
            self.raw(b"GARBAGE\r\n\r\n"),
            self.raw_request("GET", "/", host="evil.example"),
        ]
        for resp in samples:
            with self.subTest(resp=resp):
                self.assert_coded(resp)


# --- The checkers against fixtures ---

GOOD_TABLE = """'use strict';
self.WORLD_EATS_STRINGS = {
  "meta.lang": "en",
  "app.title": "World Eats",
  "meal.delete.title": "Delete this meal?",
  "meal.count": {"one": "{count} meal", "other": "{count} meals"},
  "pick.empty": "Literal {{braces}} and {country}",
  "login.username": "Username",
  "error.not_found": "Not found",
  "country.PE": "Peru",
  "board.short.CF": "Central African Rep."
};
"""

GOOD_JS = r"""'use strict';
const STRINGS = self.WORLD_EATS_STRINGS;
function t(key, params) { return key; }
function tn(key, count, params) { return key; }
const RE = /[&<>"'] a b/g;
const half = width / 2;  // division, then a regex: /no words here/
el.textContent = t('meal.delete.title');
el.textContent = tn('meal.count', 3);
list.innerHTML = `<li>${esc(t("pick.empty", { country: name }))}</li>`;
console.warn('Missing string for', key); // i18n-ok console only
const n = items.map((item) => item.x);
"""

GOOD_HTML = """<!doctype html>
<html lang="en"><head><title data-i18n="app.title">World Eats</title></head>
<body>
<label for="u" data-i18n="login.username">  Username
</label>
<input id="u" placeholder="Username" data-i18n-placeholder="login.username">
<script src="/strings-en.js" defer></script>
<script src="/app.js" defer></script>
</body></html>
"""


class CheckerTests(unittest.TestCase):
    """The checkers find what they should, on fixtures (not the real files)."""

    def good(self):
        table, problems = parse_strings(GOOD_TABLE)
        self.assertEqual(problems, [])
        return table, JsSource(GOOD_JS), parse_index(GOOD_HTML)

    def test_good_fixture_passes(self):
        table, js, index = self.good()
        self.assertEqual(check_placeholders(table), [])
        self.assertEqual(check_key_types(table, js, index), [])
        self.assertEqual(check_unused(table, js, index), [])
        self.assertEqual(check_html_text(table, index), [])
        self.assertEqual(check_hard_coded(js), [])
        self.assertEqual(check_escaping(js), [])
        self.assertEqual(check_shadowing(js), [])
        self.assertEqual(check_script_order(index), [])
        self.assertEqual(sorted(literal_calls(js)), [("t", "meal.delete.title", 7), ("t", "pick.empty", 9),
                                                      ("tn", "meal.count", 8)])

    def test_file_format_problems(self):
        good = GOOD_TABLE
        bad = {
            "first line": good.replace("'use strict';", '"use strict";'),
            "second line": good.replace("self.WORLD_EATS_STRINGS = {", "window.WORLD_EATS_STRINGS = {"),
            "last line": good.replace("};\n", "}\n"),
            "after the end": good + "console.log(1);\n",
            "bom": "﻿" + good,
            "duplicate": good.replace('"app.title": "World Eats",', '"app.title": "World Eats", "app.title": "X",'),
            "trailing comma": good.replace('"Central African Rep."', '"Central African Rep.",'),
            "comment": good.replace('"app.title"', '// c\n  "app.title"'),
            "single quotes": good.replace('"World Eats"', "'World Eats'"),
            "nan": good.replace('"World Eats"', "NaN"),
            "number": good.replace('"World Eats"', "5"),
            "bad key": good.replace('"app.title"', '"App.title"'),
            "no dot": good.replace('"app.title"', '"title"'),
            "plural without other": good.replace('"other": "{count} meals"', '"many": "{count} meals"'),
            "plural bad form": good.replace('"one":', '"single":'),
            "nested": good.replace('"one": "{count} meal"', '"one": {"x": "y"}'),
            "lang": good.replace('"meta.lang": "en"', '"meta.lang": "fr"'),
            "no lang": good.replace('"meta.lang": "en",', ''),
        }
        for name, text in bad.items():
            with self.subTest(name=name):
                _, problems = parse_strings(text)
                self.assertNotEqual(problems, [])
        _, problems = parse_strings(good + "\n\n")
        self.assertEqual(problems, [], "trailing blank lines are fine")

    def test_placeholder_problems(self):
        for text in ("{Country}", "{country", "country}", "{1}", "{a-b}", "{ x }", "{{x}", "}"):
            with self.subTest(text=text):
                self.assertNotEqual(check_placeholders({"a.b": text}), [])
                self.assertNotEqual(check_placeholders({"a.b": {"other": text}}), [])
        self.assertEqual(check_placeholders({"a.b": "{{x}} {aB1} }}{{"}), [])

    def test_key_type_problems(self):
        table, _, index = self.good()
        js = JsSource("x(t('missing.key')); y(tn('meal.delete.title', 2)); z(t('meal.count'));")
        self.assertEqual(len(check_key_types(table, js, index)), 3)
        index = parse_index('<p data-i18n="nope.key">x</p><input title="x" data-i18n-title="meal.count">')
        self.assertEqual(len(check_key_types(table, JsSource(""), index)), 2)

    def test_unused_key(self):
        table, js, index = self.good()
        table = {**table, "extra.key": "x", "error.anything": "x", "meta.other": "x"}
        self.assertEqual(check_unused(table, js, index), ["unused key extra.key"])
        # A key in a lookup object counts as a literal use.
        self.assertEqual(check_unused(table, JsSource(GOOD_JS + "const K = { a: 'extra.key' };"), index), [])

    def test_html_problems(self):
        table, _, _ = self.good()
        cases = {
            "text differs": '<p data-i18n="app.title">World  Eat</p>',
            "child element": '<p data-i18n="app.title">World <b>Eats</b></p>',
            "attr differs": '<input placeholder="User" data-i18n-placeholder="login.username">',
            "attr missing": '<input data-i18n-placeholder="login.username">',
            "void": '<input data-i18n="login.username">',
        }
        for name, text in cases.items():
            with self.subTest(name=name):
                self.assertNotEqual(check_html_text(table, parse_index(text)), [])
        table = {**table, "x.y": "Loading… − & <b>"}
        index = parse_index('<p data-i18n="x.y">Loading&hellip; &minus; &amp; &lt;b&gt;</p>')
        self.assertEqual(check_html_text(table, index), [], "entities are decoded")
        self.assertEqual(index.uses, [("data-i18n", "x.y", html.unescape("Loading&hellip; &minus; &amp; &lt;b&gt;"), 1)])

    def test_hard_coded_text(self):
        flagged = ["el.textContent = 'Log in';", 'x = "Not logged in";', "x = `Hello there ${name}`;",
                   "x = `${a} items left`;", "x = 'Caf\u00e9 cr\u00e8me';", "x = `\n  two words\n`;",
                   "x = 'Log a meal';", "x = 'Log a b';", "x = 'Go to a b c';",
                   # Capitalised literals straight into a text sink.
                   "toast('Removed');", "fail('Nope', 'code');", "textEl('p', 'Removed', 'muted');",
                   "setError(box, 'Wrong');", "o = new Option('Peru', 'PE');", "el.textContent = 'Done';",
                   "el.textContent = `Done`;", "toast(\n  'Saved',\n  'ok');"]
        for src in flagged:
            with self.subTest(src=src):
                self.assertEqual(len(check_hard_coded(JsSource(src))), 1)
        allowed = ["x = 'a b';", "x = 'x a b';", "x = 'one-word';", "// Some words in a comment",
                   "/* two words */", "x = /two words/;", "x = 'Two words'; // i18n-ok selector",
                   "x = t('meal.delete.title');", "x = `${t('a.b')} ${t('c.d')}`;", "x = 'A1 b2';",
                   "y = 1;", "toast('Removed'); // i18n-ok fixture",
                   # Non-text literals in sinks, and capitalised literals in non-text positions.
                   "textEl('Span', t('a.b'), 'Muted');", "toast(t('a.b'), 'Error');", "toast('OK');",
                   "el.textContent = '';", "el.textContent = 'x';", "o = new Option(name, 'PE');",
                   "setError('Box', t('a.b'));", "x = 'Removed';", "fail(t('a.b'), 'Not_found');",
                   "el.textContent = t('a.b');", "mytoast('Removed');", "a.toast('Removed');"]
        for src in allowed:
            with self.subTest(src=src):
                self.assertEqual(check_hard_coded(JsSource(src)), [])
        self.assertEqual(len(check_hard_coded(JsSource("x = 'Two words'; // i18n-ok"))), 1, "needs a reason")
        # Regex literal vs division.
        self.assertEqual(check_hard_coded(JsSource("a = b / 2; c = 'x' / 'y'; d = (e) / f;")), [])
        self.assertEqual(len(check_hard_coded(JsSource("a = b / 2; c = 'Two words';"))), 1)

    def test_escaping_problems(self):
        flagged = ["el.innerHTML = `<b>${t('a.b')}</b>`;", "el.innerHTML = `<b>${ tn('a.b', 2)}</b>`;",
                   "el.innerHTML = `<b>${foo(t('a.b'))}</b>`;", "el.innerHTML = '<b>' + t('a.b') + '</b>';",
                   "el.innerHTML = html + t('a.b');",
                   "el.innerHTML = `<b>${x ? t('a.b') : ''}</b>`;",
                   # No exemptions for test 11.
                   "el.innerHTML = '<b>' + t('a.b'); // i18n-ok reviewed",
                   "el.innerHTML = `<b>${t('a.b')}</b>`; // i18n-ok HTML markup",
                   # Statement-level, across lines.
                   "el.innerHTML = '<b>' +\n  t('a.b') +\n  '</b>';",
                   "const row = '<li>'\n  + esc(t('a.b'));",
                   # Accumulated into a variable that goes to innerHTML.
                   "let html = '';\nhtml += t('a.b') + x;\nel.innerHTML = html;",
                   "html = html + t('a.b');\nbox.innerHTML = html;"]
        for src in flagged:
            with self.subTest(src=src):
                self.assertNotEqual(check_escaping(JsSource(src)), [])
        allowed = ["el.innerHTML = `<b>${esc(t('a.b'))}</b>`;", "el.innerHTML = `${x ? esc(t('a.b')) : ''}`;",
                   "el.textContent = t('a.b') + ' ' + t('c.d');", "const s = 'x' + y; // <b>",
                   "if (count < 2) x = 1;\nconst s = t('a.b') + y;",
                   "for (let i = 0; i < n; i++) total += i;\nlabel.textContent = t('a.b') + n;",
                   "let html = '';\nhtml += `<b>${esc(t('a.b'))}</b>`;\nel.innerHTML = html;",
                   "const text = t('a.b') + x;\nel.innerHTML = other;"]
        for src in allowed:
            with self.subTest(src=src):
                self.assertEqual(check_escaping(JsSource(src)), [])

    def test_shadowing_problems(self):
        flagged = ["const t = 1;", "let tn = 2;", "var t;", "for (const t of tiles) {}", "const { t } = x;",
                   "const [a, t] = x;", "const { key: t } = x;", "let a = 1, t = 2;", "function f(t) {}",
                   "function f(a, { t }) {}", "function f(a = 1, tn = 2) {}", "x.map((t) => t.x);",
                   "x.map(t => t.x);", "x.map((a, t) => a);", "try {} catch (t) {}",
                   "class A { set v(t) { this.x = t; } }", "const o = { f(t) { return t; } };",
                   "function t() {}\nfunction t() {}", "async (t) => 1;",
                   "const t = 1; // i18n-ok no exemptions for test 12"]
        for src in flagged:
            with self.subTest(src=src):
                self.assertNotEqual(check_shadowing(JsSource(src)), [])
        allowed = ["function t(key, params) {}\nfunction tn(key, count, params) {}", "const tile = 1;",
                   "const token = t('a.b');", "x.map((tile) => t('a.b'));", "const f = (a = t('a.b')) => a;",
                   "const { key: tile } = x;", "const { t: label } = x;", "const a = f(x, t, y);",
                   "if (t('a.b')) { go(); }", "x.forEach(function (tile) {});", "const tt = 1, nt = 2;"]
        for src in allowed:
            with self.subTest(src=src):
                self.assertEqual(check_shadowing(JsSource(src)), [])

    def test_non_literal_keys(self):
        table, _, index = self.good()
        for src in ("x(t(key));", "x(tn(name, 2));", "x(t(`meal.${kind}`));", "x(t('meal.delete\\u002etitle'));",
                    "x(t(\n  'meal.' + kind));", "x(t());"):
            with self.subTest(src=src):
                problems = check_key_types(table, JsSource(src), index)
                self.assertEqual(len(problems), 1, problems)
                self.assertIn("without a literal key", problems[0])
        js = JsSource("function t(key, params) {}\nfunction tn(key, count, params) {}\nx(t(`meal.delete.title`));")
        self.assertEqual(check_key_types(table, js, index), [])

    def test_empty_i18n_attributes(self):
        table, js, _ = self.good()
        for text in ('<p data-i18n="">x</p>', '<input placeholder="x" data-i18n-placeholder="">',
                     '<p data-i18n>x</p>'):
            with self.subTest(text=text):
                index = parse_index(text)
                self.assertTrue(any("empty data-i18n" in p for p in check_key_types(table, js, index)))
                self.assertNotEqual(check_html_text(table, index), [])

    def test_script_order_problems(self):
        for text in ('<script src="/app.js" defer></script><script src="/strings-en.js" defer></script>',
                     '<script src="/strings-en.js"></script><script src="/app.js" defer></script>',
                     '<script src="/app.js" defer></script>'):
            with self.subTest(text=text):
                self.assertNotEqual(check_script_order(parse_index(text)), [])

    def test_lexer(self):
        js = JsSource("a = 'it\\'s \\u0041'; b = `x${ {k: `y${z}`}.k }w`; c = /[/]x/g; // 'no'\n/* \"no\" */ d")
        self.assertEqual([(lit.kind, lit.value) for lit in js.literals],
                         [("string", "it's A"), ("template", "x"), ("template", "y"), ("template", ""),
                          ("template", "w")])
        self.assertEqual(len(js.code), len(js.src))
        self.assertNotIn("no", js.code)
        self.assertEqual(js.code.count("\n"), 1)

    def test_real_file_missing_fails(self):
        missing = STATIC / "no-such-file.js"
        with self.assertRaises(AssertionError) as ctx:
            read_required(missing)
        self.assertIn("missing", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
