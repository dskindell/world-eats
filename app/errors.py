"""Error codes and their English text (contract 10, §5 and §12).

MESSAGES is the catalogue: one stable code per distinct message. The frontend's
`error.<code>` table entries must equal these templates exactly (tests/test_strings.py).

Templates use `{name}` placeholders, filled from integer server constants only
(never request data). Numbers written with a thousands separator stay literal
in the template, with no param (2,000, 5,000, 20,000). Templates contain no
literal braces.
"""

import re

CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,39}")
PARAM_RE = re.compile(r"[a-z][a-zA-Z0-9]*")
# The same one-pass grammar as the frontend's t(): inserted values are never re-scanned.
PLACEHOLDER_RE = re.compile(r"\{\{|\}\}|\{([a-z][a-zA-Z0-9]*)\}")
RESERVED_KEYS = frozenset({"error", "code", "params"})

MESSAGES = {
    # Request handling
    "bad_request": "Bad request",
    "headers_too_large": "Request headers too large",
    "http_version": "HTTP version not supported",
    "unknown_host": "Unknown host",
    "method_not_allowed": "Method not allowed",
    "length_required": "Content-Length required",
    "invalid_content_length": "Invalid Content-Length",
    "cross_origin": "Cross-origin request blocked",
    "content_type_json": "Content-Type must be application/json",
    "too_large": "Request too large",
    "invalid_json": "Invalid JSON",
    "not_found": "Not found",
    "internal_error": "Internal error",
    "busy": "Server busy, try again",
    # Sessions, setup, login and account settings
    "not_logged_in": "Not logged in",
    "password_change_required": "You must change your password first",
    "admins_only": "Admins only",
    "setup_complete": "Setup is already complete",
    "setup_required": "Setup is required",
    "too_many_attempts": "Too many failed attempts. Try again in a few minutes.",
    "wrong_login": "Wrong username or password",
    "nothing_to_update": "Nothing to update",
    "current_password_wrong": "Current password is wrong",
    "password_unchanged": "New password must be different from the current one",
    "password_wrong": "Password is wrong",
    "only_admin_delete": "You're the only admin, so this account can't be deleted",
    "username_invalid": "Username must be {min}-{max} characters: letters, digits, . _ -",
    "display_name_length": "Display name must be {min}-{max} characters",
    "display_name_chars": "Display name contains characters that aren't allowed",
    "password_length": "Password must be {min}-{max} characters",
    # Admin screen
    "user_not_found": "User not found",
    "is_admin_invalid": "is_admin must be true or false",
    "username_taken": "That username is taken",
    "reset_own_password": "Change your own password in Account settings",
    "disable_self": "You can't disable your own account",
    "last_admin_disable": "This is the last admin, so it can't be disabled",
    "only_admin_demote": "You're the only admin, so you can't remove your own admin rights",
    "delete_self": "Delete your own account in Account settings",
    "confirm_username": "Type the username to confirm",
    "last_admin_delete": "This is the last admin, so it can't be deleted",
    # Passports
    "passport_name_length": "Passport name must be {min}-{max} characters",
    "passport_name_chars": "Passport name contains characters that aren't allowed",
    "choose_people": "Choose people from the list",
    "choose_passport": "Choose a passport you belong to",
    "passport_not_found": "Passport not found",
    "owner_only": "Only the owner can do that",
    "person_repeated": "Each person can only be added once",
    "passport_limit": "You can own at most {max} passports",
    "people_limit": "A passport can have at most {max} people",
    "personal_passport_delete": "Your personal passport can't be deleted",
    "confirm_passport_name": "Type the passport name to confirm",
    "role_invalid": "Role must be participant or viewer",
    "personal_passport_participants": "A personal passport can't have participants",
    "already_member": "Already in this passport",
    "owner_remove": "The owner can't be removed",
    "not_member": "Not in this passport",
    "owner_leave": "The owner can't leave. Delete the passport instead.",
    # Countries and meals
    "country_not_found": "Country not found",
    "meal_not_found": "Meal not found",
    "participants_only": "Only participants can log meals",
    "own_meals_only": "You can only change your own meals",
    "date_range": "Choose a date between {year} and today",
    "dish_length": '"What you ate" can be up to {max} characters',
    "dish_chars": '"What you ate" contains characters that aren\'t allowed',
    "place_length": '"Where" can be up to {max} characters',
    "place_chars": '"Where" contains characters that aren\'t allowed',
    "rating_range": "Stars must be {min} to {max}",
    "notes_length": "Notes can be up to 2,000 characters",  # literal: meals.MAX_NOTES
    "notes_chars": "Notes contain characters that aren't allowed",
    "meal_limit_country": "You can log at most {max} meals for one country",
    "meal_limit_passport": "You can log at most 5,000 meals in one passport",  # literal: meals.MAX_PER_PASSPORT
    "meal_limit_account": "You can log at most 20,000 meals",  # literal: meals.MAX_PER_ACCOUNT
    # Search
    "search_empty": "Type something to search",
    "search_length": "Search can be up to {max} characters",
    "search_term_length": "Search words can be up to {max} characters",
    # Next stops
    "next_stop_list_invalid": "List must be mine or ours",
    "next_stop_on_invalid": "on must be true or false",
    "personal_no_shared": "Personal passports have no shared next stops",
    "next_stop_done": "You've already eaten here",
    "next_stop_complete": "Everyone has already eaten here",
    # Profile photos
    "content_type_jpeg": "Content-Type must be image/jpeg",
    "invalid_image": "That photo couldn't be read. Try another photo.",
    "photo_not_found": "Photo not found",
    "too_many_uploads": "Too many photo changes. Try again later.",
}


def placeholders(template):
    return [m.group(1) for m in PLACEHOLDER_RE.finditer(template) if m.group(1)]


def check(code, params):
    """Raises (a programming error, so a 500) unless code and params fit the catalogue exactly."""
    if code not in MESSAGES:
        raise ValueError(f"unknown error code {code!r}")
    params = params or {}
    if set(params) != set(placeholders(MESSAGES[code])):
        raise ValueError(f"params for {code!r} don't match its template")
    for name, value in params.items():
        if type(value) is not int or not PARAM_RE.fullmatch(name):
            raise ValueError(f"param {name!r} of {code!r} must be an int")
    return params


def render(code, params=None):
    """The English text for `code`: one pass, plain base-10 integers."""
    params = check(code, params)

    def substitute(match):
        token = match.group(0)
        if token == "{{":
            return "{"
        if token == "}}":
            return "}"
        return str(params[match.group(1)])

    return PLACEHOLDER_RE.sub(substitute, MESSAGES[code])


def body(code, params=None, extra=None):
    """The JSON error body: error, code, params (only when non-empty), then any extra fields."""
    params = check(code, params)
    extra = extra or {}
    if RESERVED_KEYS & set(extra):
        raise ValueError("extra fields can't replace error, code or params")
    result = {"error": render(code, params), "code": code}
    if params:
        result["params"] = dict(params)
    result.update(extra)
    return result
