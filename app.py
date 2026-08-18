import os
import sqlite3
import string
import random
from datetime import date
from flask import Flask, request, jsonify, g, render_template

DB_PATH = os.path.join(os.path.dirname(__file__), "splitsub.db")
app = Flask(__name__)


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            code TEXT UNIQUE NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            contact TEXT
        );
        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            monthly_cost REAL NOT NULL,
            owner_member_id INTEGER REFERENCES members(id),
            billing_day INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS splits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
            member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
            share_percent REAL NOT NULL
        );
        """
    )
    db.commit()
    db.close()


def gen_code(length=6):
    alphabet = string.ascii_uppercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(length))


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/api/groups", methods=["POST"])
def create_group():
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Group name is required"}), 400
    db = get_db()
    code = gen_code()
    while db.execute("SELECT 1 FROM groups WHERE code=?", (code,)).fetchone():
        code = gen_code()
    cur = db.execute("INSERT INTO groups (name, code) VALUES (?, ?)", (name, code))
    db.commit()
    return jsonify({"id": cur.lastrowid, "name": name, "code": code})


@app.route("/api/groups/<code>", methods=["GET"])
def get_group(code):
    db = get_db()
    group = db.execute("SELECT * FROM groups WHERE code=?", (code,)).fetchone()
    if not group:
        return jsonify({"error": "Group not found"}), 404

    members = db.execute(
        "SELECT * FROM members WHERE group_id=? ORDER BY id", (group["id"],)
    ).fetchall()
    subs = db.execute(
        "SELECT * FROM subscriptions WHERE group_id=? ORDER BY id", (group["id"],)
    ).fetchall()

    sub_list = []
    owed = {m["id"]: 0.0 for m in members}
    for s in subs:
        splits = db.execute(
            "SELECT * FROM splits WHERE subscription_id=?", (s["id"],)
        ).fetchall()
        split_list = []
        for sp in splits:
            amount = round(s["monthly_cost"] * sp["share_percent"] / 100.0, 2)
            owed[sp["member_id"]] = owed.get(sp["member_id"], 0.0) + amount
            split_list.append(
                {
                    "member_id": sp["member_id"],
                    "share_percent": sp["share_percent"],
                    "amount": amount,
                }
            )
        sub_list.append(
            {
                "id": s["id"],
                "name": s["name"],
                "monthly_cost": s["monthly_cost"],
                "owner_member_id": s["owner_member_id"],
                "billing_day": s["billing_day"],
                "splits": split_list,
            }
        )

    return jsonify(
        {
            "id": group["id"],
            "name": group["name"],
            "code": group["code"],
            "members": [dict(m) for m in members],
            "subscriptions": sub_list,
            "owed_totals": owed,
        }
    )


@app.route("/api/groups/<code>/members", methods=["POST"])
def add_member(code):
    db = get_db()
    group = db.execute("SELECT * FROM groups WHERE code=?", (code,)).fetchone()
    if not group:
        return jsonify({"error": "Group not found"}), 404
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    contact = (data.get("contact") or "").strip()
    if not name:
        return jsonify({"error": "Member name is required"}), 400
    cur = db.execute(
        "INSERT INTO members (group_id, name, contact) VALUES (?, ?, ?)",
        (group["id"], name, contact),
    )
    db.commit()
    return jsonify({"id": cur.lastrowid, "name": name, "contact": contact})


@app.route("/api/groups/<code>/subscriptions", methods=["POST"])
def add_subscription(code):
    db = get_db()
    group = db.execute("SELECT * FROM groups WHERE code=?", (code,)).fetchone()
    if not group:
        return jsonify({"error": "Group not found"}), 404
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    cost = data.get("monthly_cost")
    owner_id = data.get("owner_member_id")
    billing_day = int(data.get("billing_day") or 1)
    shares = data.get("shares")  # list of {member_id, share_percent}

    if not name or cost is None or not shares:
        return jsonify({"error": "name, monthly_cost, and shares are required"}), 400

    total_pct = sum(float(s["share_percent"]) for s in shares)
    if abs(total_pct - 100.0) > 0.5:
        return jsonify({"error": f"Shares must add up to 100% (got {total_pct}%)"}), 400

    cur = db.execute(
        "INSERT INTO subscriptions (group_id, name, monthly_cost, owner_member_id, billing_day) VALUES (?, ?, ?, ?, ?)",
        (group["id"], name, float(cost), owner_id, billing_day),
    )
    sub_id = cur.lastrowid
    for s in shares:
        db.execute(
            "INSERT INTO splits (subscription_id, member_id, share_percent) VALUES (?, ?, ?)",
            (sub_id, s["member_id"], float(s["share_percent"])),
        )
    db.commit()
    return jsonify({"id": sub_id})


@app.route("/api/groups/<code>/reminder", methods=["GET"])
def reminder(code):
    db = get_db()
    group = db.execute("SELECT * FROM groups WHERE code=?", (code,)).fetchone()
    if not group:
        return jsonify({"error": "Group not found"}), 404

    members = {
        m["id"]: dict(m)
        for m in db.execute(
            "SELECT * FROM members WHERE group_id=?", (group["id"],)
        ).fetchall()
    }
    subs = db.execute(
        "SELECT * FROM subscriptions WHERE group_id=?", (group["id"],)
    ).fetchall()

    per_member_lines = {}
    for s in subs:
        splits = db.execute(
            "SELECT * FROM splits WHERE subscription_id=?", (s["id"],)
        ).fetchall()
        owner_name = members.get(s["owner_member_id"], {}).get("name", "the group")
        for sp in splits:
            if sp["member_id"] == s["owner_member_id"]:
                continue  # owner doesn't owe themselves
            amount = round(s["monthly_cost"] * sp["share_percent"] / 100.0, 2)
            per_member_lines.setdefault(sp["member_id"], []).append(
                f"  - {s['name']}: ${amount:.2f} (due to {owner_name} by day {s['billing_day']})"
            )

    messages = []
    for member_id, lines in per_member_lines.items():
        m = members[member_id]
        total = sum(float(l.split("$")[1].split(" ")[0]) for l in lines)
        text = (
            f"Hey {m['name']}! Your shared subscriptions for this month:\n"
            + "\n".join(lines)
            + f"\n  Total: ${total:.2f}\n\n"
            "Pay whenever works, e-transfer/Venmo/PayPal all fine \U0001F642"
        )
        messages.append({"member_id": member_id, "name": m["name"], "contact": m.get("contact"), "message": text})

    return jsonify({"month": date.today().strftime("%B %Y"), "messages": messages})


init_db()  # ensure tables exist whether run via `python app.py` or via gunicorn

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5055))
    app.run(host="0.0.0.0", port=port, debug=True)
