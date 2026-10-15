"""Passport storage helpers (contract .claude/specs/2-passports.md, sections 2 and 6).

Every function takes the request's connection. Writers run inside the caller's
write transaction and never open one. Nothing here raises ApiError: endpoints
in server.py turn results into responses. Named columns only, never SELECT *.
"""

PERSONAL_NAME = "My passport"
MAX_OWNED = 50
MAX_PEOPLE = 20

# Fixed strings built from fixed strings; no input.
PERSON_COLUMNS = "users.id, users.username, users.display_name, users.disabled"
FIND_PERSON_SQL = "SELECT " + PERSON_COLUMNS + " FROM users WHERE id = ?"
ACTIVE_USERS_SQL = ("SELECT " + PERSON_COLUMNS + " FROM users WHERE disabled = 0 AND id != ? "
                    "ORDER BY display_name COLLATE NOCASE, username")
MEMBERS_SQL = ("SELECT passport_members.role, " + PERSON_COLUMNS + " FROM passport_members "
               "JOIN users ON users.id = passport_members.user_id "
               "WHERE passport_members.passport_id = ? ORDER BY passport_members.id")

# The caller's membership and the passport in one read (contract §3 step 3).
MEMBERSHIP_SQL = (
    "SELECT passports.id, passports.name, passports.owner_id, passports.is_personal, passports.created_at, "
    "passport_members.role FROM passports "
    "JOIN passport_members ON passport_members.passport_id = passports.id "
    "WHERE passports.id = ? AND passport_members.user_id = ?")

# Lowest-id participant other than the leaving owner; active only, or any status.
ACTIVE_SUCCESSOR_SQL = (
    "SELECT passport_members.user_id FROM passport_members JOIN users ON users.id = passport_members.user_id "
    "WHERE passport_members.passport_id = ? AND passport_members.role = 'participant' "
    "AND passport_members.user_id != ? AND users.disabled = 0 ORDER BY passport_members.id LIMIT 1")
ANY_SUCCESSOR_SQL = (
    "SELECT user_id FROM passport_members "
    "WHERE passport_id = ? AND role = 'participant' AND user_id != ? ORDER BY id LIMIT 1")


def person_json(row):
    return {"id": row["id"], "username": row["username"], "display_name": row["display_name"],
            "inactive": bool(row["disabled"])}


def find_person(conn, user_id):
    return conn.execute(FIND_PERSON_SQL, (user_id,)).fetchone()


def find_active_user(conn, user_id, username):
    """The id of the enabled user with this id and username (case-insensitive), or None."""
    row = conn.execute("SELECT id FROM users WHERE id = ? AND username = ? AND disabled = 0",
                       (user_id, username)).fetchone()
    return row["id"] if row else None


def active_users_except(conn, user_id):
    return conn.execute(ACTIVE_USERS_SQL, (user_id,)).fetchall()


def create_personal(conn, user_id, now):
    """The new account's personal passport and owner row, in the account-creation transaction."""
    cur = conn.execute("INSERT INTO passports (name, owner_id, is_personal, created_at) VALUES (?, ?, 1, ?)",
                       (PERSONAL_NAME, user_id, now))
    add_member(conn, cur.lastrowid, user_id, "participant", now)


def create(conn, name, owner_id, participants, viewers, now):
    cur = conn.execute("INSERT INTO passports (name, owner_id, is_personal, created_at) VALUES (?, ?, 0, ?)",
                       (name, owner_id, now))
    passport_id = cur.lastrowid
    add_member(conn, passport_id, owner_id, "participant", now)
    for user_id in participants:
        add_member(conn, passport_id, user_id, "participant", now)
    for user_id in viewers:
        add_member(conn, passport_id, user_id, "viewer", now)
    return passport_id


def add_member(conn, passport_id, user_id, role, now):
    conn.execute("INSERT INTO passport_members (passport_id, user_id, role, created_at) VALUES (?, ?, ?, ?)",
                 (passport_id, user_id, role, now))


def membership(conn, passport_id, user_id):
    return conn.execute(MEMBERSHIP_SQL, (passport_id, user_id)).fetchone()


def is_member(conn, passport_id, user_id):
    return conn.execute("SELECT 1 FROM passport_members WHERE passport_id = ? AND user_id = ?",
                        (passport_id, user_id)).fetchone() is not None


def owned_count(conn, user_id):
    return conn.execute("SELECT COUNT(*) FROM passports WHERE owner_id = ?", (user_id,)).fetchone()[0]


def member_count(conn, passport_id):
    return conn.execute("SELECT COUNT(*) FROM passport_members WHERE passport_id = ?",
                        (passport_id,)).fetchone()[0]


def clear_default(conn, user_id, passport_id):
    """After a membership row is deleted: the user's default no longer points at that passport."""
    conn.execute("UPDATE users SET default_passport_id = NULL WHERE id = ? AND default_passport_id = ?",
                 (user_id, passport_id))


def effective_default(conn, user_id):
    """The stored default while the user is still a member of it, otherwise their personal passport."""
    row = conn.execute(
        "SELECT COALESCE("
        "(SELECT users.default_passport_id FROM users JOIN passport_members "
        "ON passport_members.passport_id = users.default_passport_id AND passport_members.user_id = users.id "
        "WHERE users.id = ?), "
        "(SELECT id FROM passports WHERE owner_id = ? AND is_personal = 1))", (user_id, user_id)).fetchone()
    return row[0]


def caller_role(passport, user_id):
    return "owner" if passport["owner_id"] == user_id else passport["role"]


def summaries(conn, user_id):
    """Every passport the user belongs to: their personal passport first, then by name, then id."""
    default_id = effective_default(conn, user_id)
    rows = conn.execute(
        "SELECT passports.id, passports.name, passports.owner_id, passports.is_personal, passport_members.role, "
        "users.username, users.display_name, users.disabled FROM passport_members "
        "JOIN passports ON passports.id = passport_members.passport_id "
        "JOIN users ON users.id = passports.owner_id "
        "WHERE passport_members.user_id = ? "
        "ORDER BY (passports.is_personal = 1 AND passports.owner_id = ?) DESC, passports.name COLLATE NOCASE, "
        "passports.id", (user_id, user_id)).fetchall()
    return default_id, [
        {"id": row["id"], "name": row["name"], "is_personal": bool(row["is_personal"]),
         "role": caller_role(row, user_id), "is_default": row["id"] == default_id,
         "owner": {"id": row["owner_id"], "username": row["username"], "display_name": row["display_name"],
                   "inactive": bool(row["disabled"])}}
        for row in rows]


def detail(conn, passport, user_id):
    """The passport detail object for a membership row from membership()."""
    members = conn.execute(MEMBERS_SQL, (passport["id"],)).fetchall()
    participants, viewers, owner = [], [], None
    for row in members:
        if row["role"] == "participant":
            participants.append({**person_json(row), "is_owner": row["id"] == passport["owner_id"]})
            if row["id"] == passport["owner_id"]:
                owner = person_json(row)  # the owner always has a participant row (§2 invariant)
        else:
            viewers.append(person_json(row))
    return {"passport": {"id": passport["id"], "name": passport["name"],
                         "is_personal": bool(passport["is_personal"]),
                         "role": caller_role(passport, user_id),
                         "is_default": passport["id"] == effective_default(conn, user_id),
                         "created_at": passport["created_at"],
                         "owner": owner},
            "participants": participants, "viewers": viewers}


# --- Cascades (contract §6). Never apply the owned or people limits. ---

def owned_shared(conn, user_id):
    """Non-personal passports the user owns, by name then id (the delete preview's order)."""
    return conn.execute(
        "SELECT id, name FROM passports WHERE owner_id = ? AND is_personal = 0 ORDER BY name COLLATE NOCASE, id",
        (user_id,)).fetchall()


def successor(conn, passport_id, user_id, any_status):
    row = conn.execute(ACTIVE_SUCCESSOR_SQL, (passport_id, user_id)).fetchone()
    if row is None and any_status:
        row = conn.execute(ANY_SUCCESSOR_SQL, (passport_id, user_id)).fetchone()
    return row[0] if row else None


def transfer(conn, passport_id, new_owner_id):
    conn.execute("UPDATE passports SET owner_id = ? WHERE id = ?", (new_owner_id, passport_id))


def handover_on_disable(conn, user_id):
    """Admin disable: shared passports go to the longest-standing active participant, if any."""
    for passport in owned_shared(conn, user_id):
        new_owner = successor(conn, passport["id"], user_id, any_status=False)
        if new_owner is not None:
            transfer(conn, passport["id"], new_owner)


def deletion_plan(conn, user_id):
    """[(passport row, new owner id or None = deleted)], the same choice remove_user_data makes."""
    return [(passport, successor(conn, passport["id"], user_id, any_status=True))
            for passport in owned_shared(conn, user_id)]


def remove_user_data(conn, user_id):
    """Before deleting a user: hand over or delete their shared passports, delete their personal one.

    The user row delete that follows cascades their remaining memberships; owner_id
    ON DELETE RESTRICT fails the transaction if a passport they own was missed.
    """
    for passport, new_owner in deletion_plan(conn, user_id):
        if new_owner is None:
            conn.execute("DELETE FROM passports WHERE id = ?", (passport["id"],))
        else:
            transfer(conn, passport["id"], new_owner)
    conn.execute("DELETE FROM passports WHERE owner_id = ? AND is_personal = 1", (user_id,))


def other_membership_count(conn, user_id):
    """Passports the user takes part in or views but doesn't own."""
    return conn.execute(
        "SELECT COUNT(*) FROM passport_members JOIN passports ON passports.id = passport_members.passport_id "
        "WHERE passport_members.user_id = ? AND passports.owner_id != ?", (user_id, user_id)).fetchone()[0]
