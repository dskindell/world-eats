"""Search (contract .claude/specs/4-browse.md, sections 3 and 4).

A change to fold() needs a new migration that recomputes meals.search_text for
every meal (as migration 5 does); stored text folded the old way would no longer
match queries folded the new way.

fold() is the one normalisation for country names, aliases, meals and queries.
Validators raise auth.Invalid (a 400) and never touch the database. Query
helpers take the request's connection and run inside the caller's read
transaction. Nothing here logs, and no error message is built from the query.
"""

import time
import unicodedata
from contextlib import contextmanager

import auth
import countries

MAX_QUERY = 200
MAX_TERMS = 10
MAX_TERM = 50
DISH_CAP, PLACE_CAP, NOTES_CAP = 200, 200, 4000
EXCERPT = 80
PROGRESS_STEPS = 10000

EMPTY_MSG = "Type something to search"
LENGTH_MSG = "Search can be up to 200 characters"
TERM_MSG = "Search words can be up to 50 characters"

# Letters with no accent to drop; applied after casefold, so upper-case forms are covered.
LETTERS = str.maketrans({"ø": "o", "ł": "l", "đ": "d", "ð": "d", "þ": "th", "æ": "ae", "œ": "oe", "ı": "i"})


def fold(text):
    """NFKD, drop combining marks, casefold, map LETTERS, collapse whitespace."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if unicodedata.combining(ch) == 0)
    return " ".join(stripped.casefold().translate(LETTERS).split())


def meal_search_text(dish, place, notes):
    """The meals.search_text value, capped per field in code points (§3)."""
    return fold(dish)[:DISH_CAP] + "\n" + fold(place)[:PLACE_CAP] + "\n" + fold(notes)[:NOTES_CAP]


# Folded once at import: (code, (folded name, folded aliases...)) in COUNTRIES order.
FOLDED_NAMES = tuple((c.code, tuple(fold(n) for n in (c.name, *c.aliases))) for c in countries.COUNTRIES)


def clean_query(value):
    """The search terms from body["q"], validated in the contract's order; no database."""
    if not isinstance(value, str) or auth.has_surrogate(value):
        raise auth.Invalid(EMPTY_MSG)
    if len(value) > MAX_QUERY:
        raise auth.Invalid(LENGTH_MSG)
    folded = fold(value)
    if len(folded) > MAX_QUERY:
        raise auth.Invalid(LENGTH_MSG)
    terms = list(dict.fromkeys(t for t in folded.split(" ") if t))[:MAX_TERMS]
    if any(len(t) > MAX_TERM for t in terms):
        raise auth.Invalid(TERM_MSG)
    if not terms:
        raise auth.Invalid(EMPTY_MSG)
    return terms


class Budget:
    """A progress handler that interrupts the connection's queries once `seconds` have passed."""

    def __init__(self, seconds, clock=time.monotonic):
        self.clock = clock
        self.deadline = clock() + seconds
        self.expired = False

    def __call__(self):
        if self.clock() >= self.deadline:
            self.expired = True
            return 1
        return 0


@contextmanager
def budget(conn, limit):
    """Installs `limit` on the connection for the block and always removes it afterwards."""
    conn.set_progress_handler(limit, PROGRESS_STEPS)
    try:
        yield limit
    finally:
        conn.set_progress_handler(None, PROGRESS_STEPS)


# Fixed strings; only "?" placeholder lists are added to them.
MEAL_CODES_SQL = "SELECT DISTINCT country_code FROM meals WHERE passport_id = ? AND instr(search_text, ?) > 0"
TERM_COLUMN = "instr(search_text, ?) > 0"
CANDIDATES_SQL = "SELECT id, country_code, eaten_on, {columns} FROM meals WHERE passport_id = ? AND country_code IN ({codes})"
CHOSEN_SQL = "SELECT id, dish, place, notes FROM meals WHERE passport_id = ? AND id IN ({ids})"


def placeholders(count):
    return ", ".join("?" * count)


def name_codes(term):
    return {code for code, names in FOLDED_NAMES if any(term in name for name in names)}


def find(conn, passport_id, terms):
    """The matches for already validated, folded terms in passport P, in COUNTRIES order."""
    by_name = [name_codes(term) for term in terms]
    matched = None
    for term, names in zip(terms, by_name):
        codes = names | {row[0] for row in conn.execute(MEAL_CODES_SQL, (passport_id, term))}
        matched = codes if matched is None else matched & codes
        if not matched:
            return []
    # Per country, the indexes of the terms that didn't match its name or aliases.
    hint_terms = {code: [i for i, names in enumerate(by_name) if code not in names] for code in matched}
    hints = meal_hints(conn, passport_id, terms, {code: idx for code, idx in hint_terms.items() if idx})
    return [{"code": code, "meal": hints.get(code)} for code, _ in FOLDED_NAMES if code in matched]


def meal_hints(conn, passport_id, terms, hint_terms):
    """{code: meal hint} for the countries in hint_terms, in two queries (contract §4, review M2)."""
    if not hint_terms:
        return {}
    codes = list(hint_terms)
    statement = CANDIDATES_SQL.format(columns=", ".join([TERM_COLUMN] * len(terms)), codes=placeholders(len(codes)))
    best = {}  # code: (hinted terms contained, eaten_on, id)
    for row in conn.execute(statement, (*terms, passport_id, *codes)):
        code = row[1]
        score = sum(row[3 + i] for i in hint_terms[code])
        if score:
            key = (score, row[2], row[0])
            if key > best.get(code, (0, "", 0)):
                best[code] = key
    if not best:
        return {}
    chosen = {key[2]: code for code, key in best.items()}
    statement = CHOSEN_SQL.format(ids=placeholders(len(chosen)))
    hints = {}
    for row in conn.execute(statement, (passport_id, *chosen)):
        code = chosen[row["id"]]
        hints[code] = hint_json(row, [terms[i] for i in hint_terms[code]])
    return hints


def hint_json(row, hinted):
    dish, place, notes = row["dish"], row["place"], row["notes"]
    folded = (fold(dish)[:DISH_CAP], fold(place)[:PLACE_CAP])
    excerpt = None
    if not any(term in text for term in hinted for text in folded):
        excerpt = notes[:EXCERPT].replace("\n", " ") + ("…" if len(notes) > EXCERPT else "")
    return {"dish": dish, "place": place, "notes_excerpt": excerpt}
