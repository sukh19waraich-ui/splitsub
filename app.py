import os
import string
import random
from datetime import date, datetime

from flask import Flask, request, jsonify, render_template
from sqlalchemy import (
    create_engine, MetaData, Table, Column, Integer, String, Float,
    DateTime, ForeignKey, select, insert, func
)

BASE_DIR = os.path.dirname(__file__)

# Render (and most hosts) provide DATABASE_URL for a real Postgres database.
# Locally, with no DATABASE_URL set, we fall back to a SQLite file so the app
# still runs with zero setup. Same code, same queries, either way.
db_url = os.environ.get("DATABASE_URL", f"sqlite:///{os.path.join(BASE_DIR, 'splitsub.db')}")
if db_url.startswith("postgres://"):
    # SQLAlchemy 1.4+/2.x requires the "postgresql://" scheme; some hosts
    # (Render included) still hand out the older "postgres://" form.
    db_url = db_url.replace("postgres://", "postgresql://", 1)

engine = create_engine(db_url, pool_pre_ping=True)
metadata = MetaData()

groups = Table(
    "groups", metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String, nullable=False),
    Column("code", String, unique=True, nullable=False),
    Column("created_at", DateTime, server_default=func.now()),
)

members = Table(
    "members", metadata,
    Column("id", Integer, primary_key=True),
    Column("group_id", Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False),
    Column("name", String, nullable=False),
    Column("contact", String),
)

subscriptions = Table(
    "subscriptions", metadata,
    Column("id", Integer, primary_key=True),
    Column("group_id", Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False),
    Column("name", String, nullable=False),
    Column("monthly_cost", Float, nullable=False),
    Column("owner_member_id", Integer, ForeignKey("members.id")),
    Column("billing_day", Integer, server_default="1"),
)

splits = Table(
    "splits", metadata,
    Column("id", Integer, primary_key=True),
    Column("subscription_id", Integer, ForeignKey("subscriptions.id", ondelete="CASCADE"), nullable=False),
    Column("member_id", Integer, ForeignKey("members.id", ondelete="CASCADE"), nullable=False),
    Column("share_percent", Float, nullable=False),
)

metadata.create_all(engine)  # safe to call every startup; only creates what's missing

app = Flask(__name__)


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

    with engine.begin() as conn:
        code = gen_code()
        while conn.execute(select(groups.c.id).where(groups.c.code == code)).first():
            code = gen_code()
        result = conn.execute(insert(groups).values(name=name, code=code))
        group_id = result.inserted_primary_key[0]

    return jsonify({"id": group_id, "name": name, "code": code})


@app.route("/api/groups/<code>", methods=["GET"])
def get_group(code):
    with engine.connect() as conn:
        group = conn.execute(select(groups).where(groups.c.code == code)).mappings().first()
        if not group:
            return jsonify({"error": "Group not found"}), 404

        member_rows = conn.execute(
            select(members).where(members.c.group_id == group["id"]).order_by(members.c.id)
        ).mappings().all()
        sub_rows = conn.execute(
            select(subscriptions).where(subscriptions.c.group_id == group["id"]).order_by(subscriptions.c.id)
        ).mappings().all()

        sub_list = []
        owed = {m["id"]: 0.0 for m in member_rows}
        for s in sub_rows:
            split_rows = conn.execute(
                select(splits).where(splits.c.subscription_id == s["id"])
            ).mappings().all()
            split_list = []
            for sp in split_rows:
                amount = round(s["monthly_cost"] * sp["share_percent"] / 100.0, 2)
                owed[sp["member_id"]] = owed.get(sp["member_id"], 0.0) + amount
                split_list.append({
                    "member_id": sp["member_id"],
                    "share_percent": sp["share_percent"],
                    "amount": amount,
                })
            sub_list.append({
                "id": s["id"],
                "name": s["name"],
                "monthly_cost": s["monthly_cost"],
                "owner_member_id": s["owner_member_id"],
                "billing_day": s["billing_day"],
                "splits": split_list,
            })

    return jsonify({
        "id": group["id"],
        "name": group["name"],
        "code": group["code"],
        "members": [dict(m) for m in member_rows],
        "subscriptions": sub_list,
        "owed_totals": owed,
    })


@app.route("/api/groups/<code>/members", methods=["POST"])
def add_member(code):
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    contact = (data.get("contact") or "").strip()
    if not name:
        return jsonify({"error": "Member name is required"}), 400

    with engine.begin() as conn:
        group = conn.execute(select(groups).where(groups.c.code == code)).mappings().first()
        if not group:
            return jsonify({"error": "Group not found"}), 404
        result = conn.execute(
            insert(members).values(group_id=group["id"], name=name, contact=contact)
        )
        member_id = result.inserted_primary_key[0]

    return jsonify({"id": member_id, "name": name, "contact": contact})


@app.route("/api/groups/<code>/subscriptions", methods=["POST"])
def add_subscription(code):
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    cost = data.get("monthly_cost")
    owner_id = data.get("owner_member_id")
    billing_day = int(data.get("billing_day") or 1)
    shares = data.get("shares")

    if not name or cost is None or not shares:
        return jsonify({"error": "name, monthly_cost, and shares are required"}), 400

    total_pct = sum(float(s["share_percent"]) for s in shares)
    if abs(total_pct - 100.0) > 0.5:
        return jsonify({"error": f"Shares must add up to 100% (got {total_pct}%)"}), 400

    with engine.begin() as conn:
        group = conn.execute(select(groups).where(groups.c.code == code)).mappings().first()
        if not group:
            return jsonify({"error": "Group not found"}), 404

        result = conn.execute(insert(subscriptions).values(
            group_id=group["id"], name=name, monthly_cost=float(cost),
            owner_member_id=owner_id, billing_day=billing_day,
        ))
        sub_id = result.inserted_primary_key[0]

        for s in shares:
            conn.execute(insert(splits).values(
                subscription_id=sub_id,
                member_id=s["member_id"],
                share_percent=float(s["share_percent"]),
            ))

    return jsonify({"id": sub_id})


@app.route("/api/groups/<code>/reminder", methods=["GET"])
def reminder(code):
    with engine.connect() as conn:
        group = conn.execute(select(groups).where(groups.c.code == code)).mappings().first()
        if not group:
            return jsonify({"error": "Group not found"}), 404

        member_rows = conn.execute(
            select(members).where(members.c.group_id == group["id"])
        ).mappings().all()
        members_by_id = {m["id"]: dict(m) for m in member_rows}

        sub_rows = conn.execute(
            select(subscriptions).where(subscriptions.c.group_id == group["id"])
        ).mappings().all()

        per_member = {}
        for s in sub_rows:
            split_rows = conn.execute(
                select(splits).where(splits.c.subscription_id == s["id"])
            ).mappings().all()
            owner_name = members_by_id.get(s["owner_member_id"], {}).get("name", "the group")
            for sp in split_rows:
                if sp["member_id"] == s["owner_member_id"]:
                    continue
                amount = round(s["monthly_cost"] * sp["share_percent"] / 100.0, 2)
                per_member.setdefault(sp["member_id"], []).append(
                    (s["name"], amount, owner_name, s["billing_day"])
                )

    messages = []
    for member_id, items in per_member.items():
        m = members_by_id[member_id]
        lines = [
            f"  - {name}: ${amount:.2f} (due to {owner} by day {day})"
            for name, amount, owner, day in items
        ]
        total = sum(amount for _, amount, _, _ in items)
        text = (
            f"Hey {m['name']}! Your shared subscriptions for this month:\n"
            + "\n".join(lines)
            + f"\n  Total: ${total:.2f}\n\n"
            "Pay whenever works, e-transfer/Venmo/PayPal all fine \U0001F642"
        )
        messages.append({"member_id": member_id, "name": m["name"], "contact": m.get("contact"), "message": text})

    return jsonify({"month": date.today().strftime("%B %Y"), "messages": messages})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5055))
    app.run(host="0.0.0.0", port=port, debug=True)
