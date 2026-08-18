import os
import string
import random
import json
from datetime import date, datetime

import stripe
from flask import Flask, request, jsonify, render_template
from sqlalchemy import (
    create_engine, MetaData, Table, Column, Integer, String, Float,
    DateTime, ForeignKey, select, insert, update, delete, func, inspect, text
)

# Stripe keys/price/webhook secret are set as environment variables directly
# in Render's dashboard (Environment tab) -- never hardcoded here.
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_PRICE_ID = os.environ.get("STRIPE_PRICE_ID")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")
FREE_GROUPS_PER_EMAIL = 1

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
    Column("owner_email", String),
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

# Tracks paid status per email. No passwords/login -- email is just a
# lightweight identifier used to enforce the free-group limit and to unlock
# unlimited groups once Stripe confirms payment.
accounts = Table(
    "accounts", metadata,
    Column("id", Integer, primary_key=True),
    Column("email", String, unique=True, nullable=False),
    Column("stripe_customer_id", String),
    Column("is_paid", Integer, server_default="0"),
    Column("created_at", DateTime, server_default=func.now()),
)

metadata.create_all(engine)  # safe to call every startup; only creates what's missing


def run_migrations():
    """metadata.create_all only creates missing TABLES, not missing columns on
    tables that already existed before this deploy. This adds any new columns
    that older, already-deployed databases won't have yet."""
    inspector = inspect(engine)
    existing_columns = {c["name"] for c in inspector.get_columns("groups")}
    if "owner_email" not in existing_columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE groups ADD COLUMN owner_email VARCHAR"))


run_migrations()

app = Flask(__name__)


def get_account(conn, email):
    return conn.execute(select(accounts).where(accounts.c.email == email)).mappings().first()


def is_email_paid(conn, email):
    acct = get_account(conn, email)
    return bool(acct and acct["is_paid"])


def upsert_account(conn, email, stripe_customer_id=None, is_paid=None):
    existing = get_account(conn, email)
    if existing:
        values = {}
        if stripe_customer_id is not None:
            values["stripe_customer_id"] = stripe_customer_id
        if is_paid is not None:
            values["is_paid"] = 1 if is_paid else 0
        if values:
            conn.execute(update(accounts).where(accounts.c.email == email).values(**values))
    else:
        conn.execute(insert(accounts).values(
            email=email,
            stripe_customer_id=stripe_customer_id,
            is_paid=1 if is_paid else 0,
        ))


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
    email = (data.get("email") or "").strip().lower()
    if not name:
        return jsonify({"error": "Group name is required"}), 400
    if not email:
        return jsonify({"error": "Email is required"}), 400

    with engine.begin() as conn:
        existing_count = conn.execute(
            select(func.count()).select_from(groups).where(groups.c.owner_email == email)
        ).scalar() or 0

        if existing_count >= FREE_GROUPS_PER_EMAIL and not is_email_paid(conn, email):
            return jsonify({
                "error": "upgrade_required",
                "message": "You've already used your free group with this email. "
                            "Upgrade to SplitSub Unlimited ($2.99/mo) to create more.",
            }), 402

        code = gen_code()
        while conn.execute(select(groups.c.id).where(groups.c.code == code)).first():
            code = gen_code()
        result = conn.execute(insert(groups).values(name=name, code=code, owner_email=email))
        group_id = result.inserted_primary_key[0]

    return jsonify({"id": group_id, "name": name, "code": code})


@app.route("/api/account/<path:email>", methods=["GET"])
def get_account_status(email):
    email = email.strip().lower()
    with engine.connect() as conn:
        is_paid = is_email_paid(conn, email)
        group_count = conn.execute(
            select(func.count()).select_from(groups).where(groups.c.owner_email == email)
        ).scalar() or 0
    return jsonify({"email": email, "is_paid": is_paid, "group_count": group_count})


@app.route("/api/billing/checkout", methods=["POST"])
def create_checkout_session():
    if not stripe.api_key or not STRIPE_PRICE_ID:
        return jsonify({"error": "Payments aren't configured yet. Please try again later."}), 503

    data = request.get_json(force=True)
    email = (data.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "Email is required"}), 400

    base_url = request.host_url.rstrip("/")
    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            customer_email=email,
            line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
            success_url=f"{base_url}/?upgraded=1",
            cancel_url=f"{base_url}/?upgrade_cancelled=1",
            metadata={"email": email},
        )
    except Exception as e:
        return jsonify({"error": f"Could not start checkout: {e}"}), 400

    return jsonify({"url": session.url})


@app.route("/api/stripe/webhook", methods=["POST"])
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature")

    try:
        if STRIPE_WEBHOOK_SECRET:
            event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
        else:
            # No webhook secret configured yet (e.g. still testing locally) --
            # accept the payload as-is rather than rejecting it outright.
            event = json.loads(payload)
    except (ValueError, stripe.error.SignatureVerificationError):
        return jsonify({"error": "Invalid webhook payload/signature"}), 400

    event_type = event["type"]
    obj = event["data"]["object"]

    with engine.begin() as conn:
        if event_type == "checkout.session.completed":
            email = (
                (obj.get("customer_details") or {}).get("email")
                or obj.get("customer_email")
                or (obj.get("metadata") or {}).get("email")
            )
            customer_id = obj.get("customer")
            if email:
                upsert_account(conn, email.strip().lower(), stripe_customer_id=customer_id, is_paid=True)

        elif event_type in ("customer.subscription.deleted",):
            customer_id = obj.get("customer")
            if customer_id:
                acct = conn.execute(
                    select(accounts).where(accounts.c.stripe_customer_id == customer_id)
                ).mappings().first()
                if acct:
                    conn.execute(
                        update(accounts).where(accounts.c.id == acct["id"]).values(is_paid=0)
                    )

        elif event_type == "customer.subscription.updated":
            customer_id = obj.get("customer")
            status = obj.get("status")
            if customer_id:
                acct = conn.execute(
                    select(accounts).where(accounts.c.stripe_customer_id == customer_id)
                ).mappings().first()
                if acct:
                    conn.execute(
                        update(accounts).where(accounts.c.id == acct["id"])
                        .values(is_paid=1 if status in ("active", "trialing") else 0)
                    )

    return jsonify({"received": True})


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


@app.route("/api/groups/<code>/subscriptions/<int:sub_id>", methods=["PUT"])
def update_subscription(code, sub_id):
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

        existing = conn.execute(
            select(subscriptions).where(
                subscriptions.c.id == sub_id, subscriptions.c.group_id == group["id"]
            )
        ).mappings().first()
        if not existing:
            return jsonify({"error": "Subscription not found"}), 404

        conn.execute(
            update(subscriptions)
            .where(subscriptions.c.id == sub_id)
            .values(name=name, monthly_cost=float(cost), owner_member_id=owner_id, billing_day=billing_day)
        )
        conn.execute(delete(splits).where(splits.c.subscription_id == sub_id))
        for s in shares:
            conn.execute(insert(splits).values(
                subscription_id=sub_id,
                member_id=s["member_id"],
                share_percent=float(s["share_percent"]),
            ))

    return jsonify({"id": sub_id})


@app.route("/api/groups/<code>/subscriptions/<int:sub_id>", methods=["DELETE"])
def delete_subscription(code, sub_id):
    with engine.begin() as conn:
        group = conn.execute(select(groups).where(groups.c.code == code)).mappings().first()
        if not group:
            return jsonify({"error": "Group not found"}), 404

        existing = conn.execute(
            select(subscriptions).where(
                subscriptions.c.id == sub_id, subscriptions.c.group_id == group["id"]
            )
        ).mappings().first()
        if not existing:
            return jsonify({"error": "Subscription not found"}), 404

        conn.execute(delete(splits).where(splits.c.subscription_id == sub_id))
        conn.execute(delete(subscriptions).where(subscriptions.c.id == sub_id))

    return jsonify({"deleted": sub_id})


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
