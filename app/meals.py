"""Meals, per-country progress and next stops (change 11b).

Validators raise auth.Invalid (a 400) and never touch the database. Storage
helpers take the request's connection and run inside the caller's transaction;
nothing here raises ApiError. Named columns only, never SELECT *.
"""

import datetime
import re
import unicodedata

import auth
import countries
import passports
import search

MAX_PER_COUNTRY = 100
MAX_PER_PASSPORT = 5000
MAX_PER_ACCOUNT = 20000
MAX_TEXT = 100
MAX_NOTES = 2000
FIRST_DATE = datetime.date(1900, 1, 1)

MIN_RATING, MAX_RATING = 1, 5

DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
DATE_PARAMS = {"year": FIRST_DATE.year}
TEXT_PARAMS = {"max": MAX_TEXT}
RATING_PARAMS = {"min": MIN_RATING, "max": MAX_RATING}


# --- Validation (§5), in field order ---

def clean_date(value, now):
    """An ISO date string from 1900-01-01 to today (UTC, from the app clock) plus one day."""
    if type(value) is not str or DATE_RE.fullmatch(value) is None:
        raise auth.Invalid("date_range", DATE_PARAMS)
    try:
        date = datetime.date.fromisoformat(value)
    except ValueError:
        raise auth.Invalid("date_range", DATE_PARAMS) from None
    today = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).date()
    if not FIRST_DATE <= date <= today + datetime.timedelta(days=1):
        raise auth.Invalid("date_range", DATE_PARAMS)
    return date.isoformat()


def clean_text(value, limit, length_error, chars_code, allowed=(auth.ZERO_WIDTH_JOINER,)):
    """Optional free text: missing or null is "", else NFC, trimmed, display-name character rules.

    length_error is the (code, params) for a value that's too long.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        raise auth.Invalid(*length_error)  # as auth.clean_display_name does for non-strings
    text = unicodedata.normalize("NFC", value).strip()
    if len(text) > limit:
        raise auth.Invalid(*length_error)
    for ch in text:
        if ch not in allowed and unicodedata.category(ch) in auth.BLOCKED_CATEGORIES:
            raise auth.Invalid(chars_code)
    return text


def clean_rating(value):
    if value is None:
        return None
    if type(value) is not int or not MIN_RATING <= value <= MAX_RATING:
        raise auth.Invalid("rating_range", RATING_PARAMS)
    return value


def clean_notes(value):
    if isinstance(value, str):
        value = value.replace("\r\n", "\n").replace("\r", "\n")
    # "2,000" is literal in the notes_length template (no param).
    return clean_text(value, MAX_NOTES, ("notes_length", None), "notes_chars",
                      allowed=(auth.ZERO_WIDTH_JOINER, "\n"))


def clean_meal(body, now):
    """The five meal fields from a request body; nothing else is read."""
    return {
        "eaten_on": clean_date(body.get("eaten_on"), now),
        "dish": clean_text(body.get("dish"), MAX_TEXT, ("dish_length", TEXT_PARAMS), "dish_chars"),
        "place": clean_text(body.get("place"), MAX_TEXT, ("place_length", TEXT_PARAMS), "place_chars"),
        "rating": clean_rating(body.get("rating")),
        "notes": clean_notes(body.get("notes")),
    }


# --- Storage ---

# Fixed strings built from fixed strings; no input.
MEAL_SELECT = ("SELECT meals.id AS meal_id, meals.user_id, meals.eaten_on, meals.dish, meals.place, meals.rating, "
               "meals.notes, " + passports.PERSON_COLUMNS + " FROM meals JOIN users ON users.id = meals.user_id")
MEAL_BY_ID_SQL = MEAL_SELECT + " WHERE meals.id = ?"
COUNTRY_MEALS_SQL = (MEAL_SELECT + " WHERE meals.passport_id = ? AND meals.country_code = ? "
                     "ORDER BY meals.eaten_on DESC, meals.id DESC")
PARTICIPANTS_SQL = ("SELECT " + passports.PERSON_COLUMNS + " FROM passport_members "
                    "JOIN users ON users.id = passport_members.user_id "
                    "WHERE passport_members.passport_id = ? AND passport_members.role = 'participant' "
                    "ORDER BY passport_members.id")
# Per country and person: their completion date, meal count, and rating sum and count.
PROGRESS_SELECT = ("SELECT country_code, user_id, MIN(eaten_on) AS first_on, COUNT(*) AS meals, "
                   "SUM(rating) AS rating_sum, COUNT(rating) AS ratings FROM meals WHERE passport_id = ?")
PROGRESS_GROUP = " GROUP BY country_code, user_id"
PROGRESS_SQL = PROGRESS_SELECT + PROGRESS_GROUP
PROGRESS_ONE_SQL = PROGRESS_SELECT + " AND country_code = ?" + PROGRESS_GROUP
# Change 11b §7.1: own entries count only for enabled participants, in membership order.
OWN_STOPS_SELECT = ("SELECT next_stops.country_code, " + passports.PERSON_COLUMNS + " FROM next_stops "
                    "JOIN passport_members ON passport_members.passport_id = next_stops.passport_id "
                    "AND passport_members.user_id = next_stops.user_id "
                    "JOIN users ON users.id = next_stops.user_id "
                    "WHERE next_stops.passport_id = ? AND passport_members.role = 'participant' "
                    "AND users.disabled = 0")
OWN_STOPS_SQL = OWN_STOPS_SELECT + " ORDER BY passport_members.id"
OWN_STOPS_ONE_SQL = OWN_STOPS_SELECT + " AND next_stops.country_code = ? ORDER BY passport_members.id"
SHARED_STOPS_SQL = "SELECT country_code FROM shared_next_stops WHERE passport_id = ?"
SHARED_STOPS_ONE_SQL = SHARED_STOPS_SQL + " AND country_code = ?"
# The adder only while they're still a participant here (§7.1); otherwise the person columns are NULL.
SHARED_STOP_DETAIL_SQL = (
    "SELECT shared_next_stops.created_at AS added_at, " + passports.PERSON_COLUMNS + " FROM shared_next_stops "
    "LEFT JOIN passport_members ON passport_members.passport_id = shared_next_stops.passport_id "
    "AND passport_members.user_id = shared_next_stops.added_by AND passport_members.role = 'participant' "
    "LEFT JOIN users ON users.id = passport_members.user_id "
    "WHERE shared_next_stops.passport_id = ? AND shared_next_stops.country_code = ?")


def meal_json(row, caller_id):
    return {"id": row["meal_id"], "user": passports.person_json(row), "eaten_on": row["eaten_on"],
            "dish": row["dish"], "place": row["place"], "rating": row["rating"], "notes": row["notes"],
            "mine": row["user_id"] == caller_id}


def find_meal(conn, passport_id, meal_id):
    """The meal's owner and country, only if it's in this passport."""
    return conn.execute("SELECT user_id, country_code FROM meals WHERE id = ? AND passport_id = ?",
                        (meal_id, passport_id)).fetchone()


def meal_by_id(conn, meal_id):
    return conn.execute(MEAL_BY_ID_SQL, (meal_id,)).fetchone()


def limit_reached(conn, passport_id, user_id, code):
    """The 409 (error code, params) for the first limit the caller has reached, or None.

    Counts only their meals. 5,000 and 20,000 are literal in their templates (no params).
    """
    checks = (
        ("SELECT COUNT(*) FROM meals WHERE passport_id = ? AND user_id = ? AND country_code = ?",
         (passport_id, user_id, code), MAX_PER_COUNTRY, ("meal_limit_country", {"max": MAX_PER_COUNTRY})),
        ("SELECT COUNT(*) FROM meals WHERE passport_id = ? AND user_id = ?",
         (passport_id, user_id), MAX_PER_PASSPORT, ("meal_limit_passport", None)),
        ("SELECT COUNT(*) FROM meals WHERE user_id = ?",
         (user_id,), MAX_PER_ACCOUNT, ("meal_limit_account", None)),
    )
    for sql, params, limit, error in checks:
        if conn.execute(sql, params).fetchone()[0] >= limit:
            return error
    return None


def search_text(meal):
    return search.meal_search_text(meal["dish"], meal["place"], meal["notes"])


def insert(conn, passport_id, user_id, code, meal, now):
    cur = conn.execute(
        "INSERT INTO meals (passport_id, user_id, country_code, eaten_on, dish, place, rating, notes, "
        "search_text, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (passport_id, user_id, code, meal["eaten_on"], meal["dish"], meal["place"], meal["rating"],
         meal["notes"], search_text(meal), now, now))
    return cur.lastrowid


def update(conn, passport_id, user_id, meal_id, meal, now):
    """Only the caller's own meal in this passport; returns False if no row matched."""
    cur = conn.execute(
        "UPDATE meals SET eaten_on = ?, dish = ?, place = ?, rating = ?, notes = ?, search_text = ?, "
        "updated_at = ? WHERE id = ? AND passport_id = ? AND user_id = ?",
        (meal["eaten_on"], meal["dish"], meal["place"], meal["rating"], meal["notes"], search_text(meal), now,
         meal_id, passport_id, user_id))
    return cur.rowcount == 1


def delete(conn, passport_id, user_id, meal_id):
    cur = conn.execute("DELETE FROM meals WHERE id = ? AND passport_id = ? AND user_id = ?",
                       (meal_id, passport_id, user_id))
    return cur.rowcount == 1


def has_meal(conn, passport_id, user_id, code):
    return conn.execute("SELECT EXISTS(SELECT 1 FROM meals WHERE passport_id = ? AND user_id = ? "
                        "AND country_code = ?)", (passport_id, user_id, code)).fetchone()[0] == 1


# --- Derived values (§3) ---

def average(rating_sum, ratings):
    """The mean rating rounded half up to one decimal, in exact integer arithmetic (4.25 -> 4.3)."""
    if not ratings:
        return None
    return ((20 * rating_sum + ratings) // (2 * ratings)) / 10


def country_state(first_on, active_ids):
    """(status, fully-complete date) from {user_id: completion date} for one country."""
    if not first_on:
        return "not_started", None
    if active_ids and all(user_id in first_on for user_id in active_ids):
        return "complete", max(first_on[user_id] for user_id in active_ids)
    return "some", None


def active_participant_ids(conn, passport_id):
    return {row["id"] for row in conn.execute(PARTICIPANTS_SQL, (passport_id,)) if not row["disabled"]}


def progress(conn, passport_id, code=None):
    """{country code: {first_on: {user_id: date}, meals, sum, count}}, for one country if code is given."""
    rows = (conn.execute(PROGRESS_SQL, (passport_id,)) if code is None
            else conn.execute(PROGRESS_ONE_SQL, (passport_id, code)))
    per_country = {}
    for row in rows:
        entry = per_country.setdefault(row["country_code"], {"first_on": {}, "meals": 0, "sum": 0, "count": 0})
        entry["first_on"][row["user_id"]] = row["first_on"]
        entry["meals"] += row["meals"]
        entry["sum"] += row["rating_sum"] or 0
        entry["count"] += row["ratings"]
    return per_country


EMPTY_PROGRESS = {"first_on": {}, "meals": 0, "sum": 0, "count": 0}


class SummaryContext:
    """Everything country_summary() needs, read in a fixed number of queries (all countries or one)."""

    def __init__(self, conn, passport, caller_id, code=None):
        passport_id = passport["id"]
        self.caller_id = caller_id
        self.is_viewer = passport["role"] == "viewer"
        self.active_ids = active_participant_ids(conn, passport_id)
        self.per_country = progress(conn, passport_id, code)
        self.own = {}  # country code -> [user ids, membership order]
        rows = (conn.execute(OWN_STOPS_SQL, (passport_id,)) if code is None
                else conn.execute(OWN_STOPS_ONE_SQL, (passport_id, code)))
        for row in rows:
            self.own.setdefault(row["country_code"], []).append(row["id"])
        self.shared = set()
        if not passport["is_personal"]:  # §7.1: never a shared list on a personal passport
            rows = (conn.execute(SHARED_STOPS_SQL, (passport_id,)) if code is None
                    else conn.execute(SHARED_STOPS_ONE_SQL, (passport_id, code)))
            self.shared = {row["country_code"] for row in rows}


def country_summary(ctx, code):
    """One country's summary object, as in GET .../countries (contract 11b §7.4: the only builder)."""
    entry = ctx.per_country.get(code, EMPTY_PROGRESS)
    status, completed_on = country_state(entry["first_on"], ctx.active_ids)
    own = ctx.own.get(code, [])
    return {"code": code, "status": status,
            "done_by_me": None if ctx.is_viewer else ctx.caller_id in entry["first_on"],
            "meal_count": entry["meals"], "avg_rating": average(entry["sum"], entry["count"]),
            "completed_on": completed_on,
            "done_count": sum(user_id in entry["first_on"] for user_id in ctx.active_ids),
            "my_next_stop": None if ctx.is_viewer else ctx.caller_id in own,
            "next_stop_user_ids": list(own),
            "shared_next_stop": code in ctx.shared}


def country_list(conn, passport, caller_id):
    """The passport's progress and all 197 summaries, in a fixed number of queries."""
    ctx = SummaryContext(conn, passport, caller_id)
    summaries = [country_summary(ctx, country.code) for country in countries.COUNTRIES]
    return {"progress": {"total": len(countries.COUNTRIES),
                         "complete": sum(s["status"] == "complete" for s in summaries),
                         "mine": None if ctx.is_viewer else sum(s["done_by_me"] for s in summaries),
                         "active_participants": len(ctx.active_ids)},
            "countries": summaries}


def next_stops_json(conn, passport, code):
    """The detail's next_stops object: people with it on their own list, and the shared entry."""
    people = [passports.person_json(row) for row in conn.execute(OWN_STOPS_ONE_SQL, (passport["id"], code))]
    shared = None
    if not passport["is_personal"]:
        row = conn.execute(SHARED_STOP_DETAIL_SQL, (passport["id"], code)).fetchone()
        if row is not None:
            shared = {"added_by": passports.person_json(row) if row["id"] is not None else None,
                      "created_at": row["added_at"]}
    return {"people": people, "shared": shared}


def country_detail(conn, passport, country, caller_id):
    participants = conn.execute(PARTICIPANTS_SQL, (passport["id"],)).fetchall()
    meal_rows = conn.execute(COUNTRY_MEALS_SQL, (passport["id"], country.code)).fetchall()
    first_on = {}
    for row in meal_rows:  # newest first, so the last one seen per person is their earliest
        first_on[row["user_id"]] = row["eaten_on"]
    active_ids = {row["id"] for row in participants if not row["disabled"]}
    status, completed_on = country_state(first_on, active_ids)
    ratings = [row["rating"] for row in meal_rows if row["rating"] is not None]
    stops = next_stops_json(conn, passport, country.code)
    return {"country": {"code": country.code, "name": country.name, "region": country.region},
            "status": status, "completed_on": completed_on, "avg_rating": average(sum(ratings), len(ratings)),
            "participants": [{**passports.person_json(row), "is_owner": row["id"] == passport["owner_id"],
                              "done": row["id"] in first_on, "completed_on": first_on.get(row["id"])}
                             for row in participants],
            "meals": [meal_json(row, caller_id) for row in meal_rows],
            "my_next_stop": my_next_stop(passport, stops, caller_id),
            "next_stops": stops}


def my_next_stop(passport, stops, caller_id):
    if passport["role"] == "viewer":
        return None
    return any(person["id"] == caller_id for person in stops["people"])


# --- Next stops (contract 11b). Writers run inside the caller's write transaction. ---

def complete_codes(conn, passport_id, code=None):
    """Fully complete countries (all, or just `code`) by country_state, as the summary's status.

    §7.2: with no active participants nothing is complete.
    """
    active_ids = active_participant_ids(conn, passport_id)
    return [country_code for country_code, entry in progress(conn, passport_id, code).items()
            if country_state(entry["first_on"], active_ids)[0] == "complete"]


def is_complete(conn, passport, code):
    return code in complete_codes(conn, passport["id"], code)


def add_own_stop(conn, passport_id, user_id, code, now):
    conn.execute("INSERT OR IGNORE INTO next_stops (passport_id, user_id, country_code, created_at) "
                 "VALUES (?, ?, ?, ?)", (passport_id, user_id, code, now))


def remove_own_stop(conn, passport_id, user_id, code):
    conn.execute("DELETE FROM next_stops WHERE passport_id = ? AND user_id = ? AND country_code = ?",
                 (passport_id, user_id, code))


def add_shared_stop(conn, passport_id, user_id, code, now):
    """An existing entry keeps its original added_by and created_at."""
    conn.execute("INSERT OR IGNORE INTO shared_next_stops (passport_id, country_code, added_by, created_at) "
                 "VALUES (?, ?, ?, ?)", (passport_id, code, user_id, now))


def remove_shared_stop(conn, passport_id, code):
    conn.execute("DELETE FROM shared_next_stops WHERE passport_id = ? AND country_code = ?", (passport_id, code))


def forget_adder(conn, passport_id, user_id):
    """After a membership is deleted: shared entries they added stay, without their name."""
    conn.execute("UPDATE shared_next_stops SET added_by = NULL WHERE passport_id = ? AND added_by = ?",
                 (passport_id, user_id))


def prune_shared(conn, passport_id, code=None):
    """Removes fully complete countries (all of them, or just `code`) from the shared list."""
    conn.executemany("DELETE FROM shared_next_stops WHERE passport_id = ? AND country_code = ?",
                     [(passport_id, country_code) for country_code in complete_codes(conn, passport_id, code)])
