"""Meals and per-country progress.

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

DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
DATE_MSG = "Choose a date between 1900 and today"
DISH_LENGTH_MSG = '"What you ate" can be up to 100 characters'
DISH_CHARS_MSG = '"What you ate" contains characters that aren\'t allowed'
PLACE_LENGTH_MSG = '"Where" can be up to 100 characters'
PLACE_CHARS_MSG = '"Where" contains characters that aren\'t allowed'
RATING_MSG = "Stars must be 1 to 5"
NOTES_LENGTH_MSG = "Notes can be up to 2,000 characters"
NOTES_CHARS_MSG = "Notes contain characters that aren't allowed"


# --- Validation (§5), in field order ---

def clean_date(value, now):
    """An ISO date string from 1900-01-01 to today (UTC, from the app clock) plus one day."""
    if type(value) is not str or DATE_RE.fullmatch(value) is None:
        raise auth.Invalid(DATE_MSG)
    try:
        date = datetime.date.fromisoformat(value)
    except ValueError:
        raise auth.Invalid(DATE_MSG) from None
    today = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).date()
    if not FIRST_DATE <= date <= today + datetime.timedelta(days=1):
        raise auth.Invalid(DATE_MSG)
    return date.isoformat()


def clean_text(value, limit, length_msg, chars_msg, allowed=(auth.ZERO_WIDTH_JOINER,)):
    """Optional free text: missing or null is "", else NFC, trimmed, display-name character rules."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise auth.Invalid(length_msg)  # as auth.clean_display_name does for non-strings
    text = unicodedata.normalize("NFC", value).strip()
    if len(text) > limit:
        raise auth.Invalid(length_msg)
    for ch in text:
        if ch not in allowed and unicodedata.category(ch) in auth.BLOCKED_CATEGORIES:
            raise auth.Invalid(chars_msg)
    return text


def clean_rating(value):
    if value is None:
        return None
    if type(value) is not int or not 1 <= value <= 5:
        raise auth.Invalid(RATING_MSG)
    return value


def clean_notes(value):
    if isinstance(value, str):
        value = value.replace("\r\n", "\n").replace("\r", "\n")
    return clean_text(value, MAX_NOTES, NOTES_LENGTH_MSG, NOTES_CHARS_MSG,
                      allowed=(auth.ZERO_WIDTH_JOINER, "\n"))


def clean_meal(body, now):
    """The five meal fields from a request body; nothing else is read."""
    return {
        "eaten_on": clean_date(body.get("eaten_on"), now),
        "dish": clean_text(body.get("dish"), MAX_TEXT, DISH_LENGTH_MSG, DISH_CHARS_MSG),
        "place": clean_text(body.get("place"), MAX_TEXT, PLACE_LENGTH_MSG, PLACE_CHARS_MSG),
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
PROGRESS_SQL = ("SELECT country_code, user_id, MIN(eaten_on) AS first_on, COUNT(*) AS meals, "
                "SUM(rating) AS rating_sum, COUNT(rating) AS ratings FROM meals WHERE passport_id = ? "
                "GROUP BY country_code, user_id")


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
    """The 409 message for the first limit the caller has reached, or None (counts only their meals)."""
    checks = (
        ("SELECT COUNT(*) FROM meals WHERE passport_id = ? AND user_id = ? AND country_code = ?",
         (passport_id, user_id, code), MAX_PER_COUNTRY, "You can log at most 100 meals for one country"),
        ("SELECT COUNT(*) FROM meals WHERE passport_id = ? AND user_id = ?",
         (passport_id, user_id), MAX_PER_PASSPORT, "You can log at most 5,000 meals in one passport"),
        ("SELECT COUNT(*) FROM meals WHERE user_id = ?",
         (user_id,), MAX_PER_ACCOUNT, "You can log at most 20,000 meals"),
    )
    for sql, params, limit, message in checks:
        if conn.execute(sql, params).fetchone()[0] >= limit:
            return message
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


def country_list(conn, passport, caller_id):
    """The passport's progress and all 197 summaries, in a fixed number of queries."""
    is_viewer = passport["role"] == "viewer"
    active_ids = {row["id"] for row in conn.execute(PARTICIPANTS_SQL, (passport["id"],)) if not row["disabled"]}
    per_country = {}
    for row in conn.execute(PROGRESS_SQL, (passport["id"],)):
        entry = per_country.setdefault(row["country_code"], {"first_on": {}, "meals": 0, "sum": 0, "count": 0})
        entry["first_on"][row["user_id"]] = row["first_on"]
        entry["meals"] += row["meals"]
        entry["sum"] += row["rating_sum"] or 0
        entry["count"] += row["ratings"]
    summaries, complete, mine = [], 0, 0
    empty = {"first_on": {}, "meals": 0, "sum": 0, "count": 0}
    for country in countries.COUNTRIES:
        entry = per_country.get(country.code, empty)
        status, completed_on = country_state(entry["first_on"], active_ids)
        done = caller_id in entry["first_on"]
        complete += status == "complete"
        mine += done
        summaries.append({"code": country.code, "status": status, "done_by_me": None if is_viewer else done,
                          "meal_count": entry["meals"], "avg_rating": average(entry["sum"], entry["count"]),
                          "completed_on": completed_on,
                          "done_count": sum(user_id in entry["first_on"] for user_id in active_ids)})
    return {"progress": {"total": len(countries.COUNTRIES), "complete": complete,
                         "mine": None if is_viewer else mine, "active_participants": len(active_ids)},
            "countries": summaries}


def country_detail(conn, passport, country, caller_id):
    participants = conn.execute(PARTICIPANTS_SQL, (passport["id"],)).fetchall()
    meal_rows = conn.execute(COUNTRY_MEALS_SQL, (passport["id"], country.code)).fetchall()
    first_on = {}
    for row in meal_rows:  # newest first, so the last one seen per person is their earliest
        first_on[row["user_id"]] = row["eaten_on"]
    active_ids = {row["id"] for row in participants if not row["disabled"]}
    status, completed_on = country_state(first_on, active_ids)
    ratings = [row["rating"] for row in meal_rows if row["rating"] is not None]
    return {"country": {"code": country.code, "name": country.name, "region": country.region},
            "status": status, "completed_on": completed_on, "avg_rating": average(sum(ratings), len(ratings)),
            "participants": [{**passports.person_json(row), "is_owner": row["id"] == passport["owner_id"],
                              "done": row["id"] in first_on, "completed_on": first_on.get(row["id"])}
                             for row in participants],
            "meals": [meal_json(row, caller_id) for row in meal_rows]}
