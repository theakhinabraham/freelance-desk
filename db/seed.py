"""Load realistic demo data.

Usage (from the project root):
    python -m db.seed

WARNING: this DELETES the database file and recreates it.

All dates are relative to today, so the dashboard always shows current
data. A fixed random seed makes the data the same on every run on a given day.

Demo login:  akhin@gmail.com  /  demo1234
"""

import os
import random
import sqlite3
from datetime import date, datetime, timedelta

from app import db
from app.auth import hash_password
from app.services.invoices import hourly_amount, next_invoice_number

DEMO_NAME = "Akhin Abraham"
DEMO_EMAIL = "akhin@gmail.com"
DEMO_PASSWORD = "demo1234"

# (name, email, phone, notes)
CLIENTS = [
    ("Sharma Textiles", "accounts@sharmatextiles.example", "+91 98200 11223",
     "Family-run fabric business in Surat. Pays reliably."),
    ("Kaveri Analytics Pvt Ltd", "finance@kaveri-analytics.example", "+91 80 4110 2233",
     "Bengaluru data consultancy. Net-15 terms."),
    ("GreenLeaf Organics", "hello@greenleaf.example", "+91 99000 44556",
     "D2C organic food brand. Scope creep on the last project."),
    ("Pixel Mantra Studio", "studio@pixelmantra.example", "+91 97300 77889",
     "Design agency, sends small fix-it jobs."),
    ("Nair & Sons Logistics", "it@nairlogistics.example", "+91 484 255 6677",
     "Kochi logistics firm. Large fixed-price work."),
]

# Rupee amounts are written as paise: 1_500_00 = Rs 1,500.00.
# window = (first_day_ago, last_day_ago) in which time logs are created.
PROJECTS = [
    dict(client=0, title="E-commerce website", billing="hourly", rate=1_500_00,
         est=60, status="completed", window=(150, 60), logs=12),
    dict(client=0, title="Inventory dashboard", billing="hourly", rate=1_500_00,
         est=40, status="active", window=(55, 1), logs=9),
    dict(client=1, title="Data pipeline automation", billing="hourly", rate=2_500_00,
         est=50, status="active", window=(100, 1), logs=10),
    dict(client=1, title="Monthly reporting retainer", billing="fixed", fixed=80_000_00,
         est=None, status="active", window=(90, 1), logs=6),
    dict(client=2, title="Brand landing page", billing="fixed", fixed=25_000_00,
         est=12, status="completed", window=(120, 75), logs=9),
    dict(client=3, title="Mobile app UI fixes", billing="hourly", rate=1_200_00,
         est=15, status="on_hold", window=(80, 31), logs=5),
    dict(client=4, title="Fleet tracking portal", billing="fixed", fixed=1_20_000_00,
         est=80, status="active", window=(110, 1), logs=14),
    dict(client=4, title="Website maintenance", billing="hourly", rate=800_00,
         est=None, status="completed", window=(140, 45), logs=8),
]

# Invoices: (project index, issue_days_ago, payment terms in days, amount, payments)
#   amount  = None  -> hourly: bill all unbilled hours logged before the issue date
#           = paise -> fixed-price instalment
#   payments = [(percent of amount, paid_days_ago, method, reference)]
#   "draft" in place of the payment list -> a draft invoice (not sent yet)
INVOICES = [
    (0, 58, 15, None, [(50, 50, "bank", "NEFT-SHT-0912"), (50, 35, "upi", "UPI-4471")]),
    (1, 20, 30, None, [(40, 10, "upi", "UPI-5520")]),                 # partial, not due
    (2, 70, 15, None, [(100, 60, "bank", "IMPS-KAV-118")]),
    (2, 35, 15, None, []),                                            # overdue, unpaid
    (3, 60, 15, 40_000_00, [(100, 45, "bank", "NEFT-KAV-201")]),
    (3, 28, 15, 40_000_00, [(100, 12, "bank", "NEFT-KAV-233")]),
    (4, 74, 15, 25_000_00, [(50, 70, "upi", "UPI-GL-01"), (50, 40, "upi", "UPI-GL-02")]),
    (5, 30, 7, None, [(30, 20, "cash", None)]),                       # overdue, partial
    (6, 90, 15, 60_000_00, [(100, 75, "bank", "RTGS-NSL-77")]),
    (6, 5, 15, 30_000_00, "draft"),
    (7, 44, 15, None, [(100, 25, "upi", "UPI-NSL-930")]),
]

DESCRIPTIONS = [
    "Requirements call", "Wireframes", "Database design", "API development",
    "Frontend work", "Bug fixing", "Code review", "Deployment",
    "Client feedback changes", "Testing", "Documentation", "Performance tuning",
]
DURATIONS = [45, 60, 90, 120, 150, 180, 210, 240]  # minutes


def create_user(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "INSERT INTO users (name, email, password_hash) VALUES (?, ?, ?)",
        (DEMO_NAME, DEMO_EMAIL, hash_password(DEMO_PASSWORD)),
    )
    return cur.lastrowid


def create_clients(conn: sqlite3.Connection, user_id: int) -> list[int]:
    ids = []
    for name, email, phone, notes in CLIENTS:
        cur = conn.execute(
            "INSERT INTO clients (user_id, name, email, phone, notes) VALUES (?, ?, ?, ?, ?)",
            (user_id, name, email, phone, notes),
        )
        ids.append(cur.lastrowid)
    return ids


def create_projects(conn: sqlite3.Connection, client_ids: list[int], today: date) -> list[int]:
    ids = []
    for p in PROJECTS:
        first_ago, last_ago = p["window"]
        start = today - timedelta(days=first_ago + 3)
        deadline = today - timedelta(days=last_ago) + timedelta(days=30)
        cur = conn.execute(
            """
            INSERT INTO projects (client_id, title, billing_type, hourly_rate_paise,
                                  fixed_price_paise, estimated_hours, status,
                                  start_date, deadline)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (client_ids[p["client"]], p["title"], p["billing"], p.get("rate"),
             p.get("fixed"), p["est"], p["status"], start.isoformat(), deadline.isoformat()),
        )
        ids.append(cur.lastrowid)
    return ids


def create_time_logs(conn: sqlite3.Connection, project_ids: list[int],
                     today: date, rng: random.Random) -> None:
    """Finished logs on weekdays. Logs on the same day never overlap."""
    next_free = {}  # date -> minutes after midnight when the next log may start
    for p, project_id in zip(PROJECTS, project_ids):
        first_ago, last_ago = p["window"]
        weekdays = [today - timedelta(days=d) for d in range(last_ago, first_ago + 1)
                    if (today - timedelta(days=d)).weekday() < 5]
        for day in sorted(rng.sample(weekdays, k=min(p["logs"], len(weekdays)))):
            start_min = next_free.get(day, 9 * 60 + 30)  # first log at 09:30
            duration = rng.choice(DURATIONS)
            next_free[day] = start_min + duration + 30   # 30-minute break
            started = datetime.combine(day, datetime.min.time()) + timedelta(minutes=start_min)
            insert_finished_log(conn, project_id, started, duration, rng.choice(DESCRIPTIONS))

    # Today: one finished log and one running timer, so the "today"
    # numbers and the stop button have something to show.
    now = db.now_ist()
    insert_finished_log(conn, project_ids[1], now - timedelta(minutes=140), 90,
                        "Stand-up and chart fixes")
    conn.execute(
        "INSERT INTO time_logs (project_id, started_at, description) VALUES (?, ?, ?)",
        (project_ids[2], db.to_timestamp(now - timedelta(minutes=40)), "Scheduler refactor"),
    )


def insert_finished_log(conn, project_id, started: datetime, minutes: int, description: str):
    conn.execute(
        """
        INSERT INTO time_logs (project_id, started_at, ended_at, duration_minutes, description)
        VALUES (?, ?, ?, ?, ?)
        """,
        (project_id, db.to_timestamp(started),
         db.to_timestamp(started + timedelta(minutes=minutes)), minutes, description),
    )


def create_invoices(conn: sqlite3.Connection, project_ids: list[int], today: date) -> None:
    # Oldest first, so invoice numbers follow issue dates.
    for p_idx, issue_ago, terms, amount, payments in sorted(INVOICES, key=lambda i: -i[1]):
        project_id = project_ids[p_idx]
        issue = today - timedelta(days=issue_ago)
        due = issue + timedelta(days=terms)

        if amount is None:
            # Hourly: bill the unbilled, finished hours logged before the issue date.
            minutes = conn.execute(
                """
                SELECT COALESCE(SUM(duration_minutes), 0)
                FROM time_logs
                WHERE project_id = ? AND invoice_id IS NULL
                  AND ended_at IS NOT NULL AND started_at < ?
                """,
                (project_id, issue.isoformat()),
            ).fetchone()[0]
            rate = PROJECTS[p_idx]["rate"]
            amount = hourly_amount(minutes, rate)  # integer paise, rounded
            if amount == 0:
                continue

        if payments == "draft":
            status, payments = "draft", []
        else:
            paid = sum(amount * pct // 100 for pct, *_ in payments)
            if sum(pct for pct, *_ in payments) == 100:
                paid = amount
            if paid >= amount:
                status = "paid"
            elif due < today:
                status = "overdue"
            else:
                status = "sent"

        cur = conn.execute(
            """
            INSERT INTO invoices (project_id, invoice_number, issue_date, due_date,
                                  amount_paise, status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (project_id, next_invoice_number(conn, issue.year), issue.isoformat(),
             due.isoformat(), amount, status),
        )
        invoice_id = cur.lastrowid

        if PROJECTS[p_idx]["billing"] == "hourly":
            # Mark the hours on this invoice as billed.
            conn.execute(
                """
                UPDATE time_logs SET invoice_id = ?
                WHERE project_id = ? AND invoice_id IS NULL
                  AND ended_at IS NOT NULL AND started_at < ?
                """,
                (invoice_id, project_id, issue.isoformat()),
            )

        remaining = amount
        for i, (pct, paid_ago, method, reference) in enumerate(payments):
            is_last_of_full = i == len(payments) - 1 and sum(x[0] for x in payments) == 100
            part = remaining if is_last_of_full else amount * pct // 100
            remaining -= part
            conn.execute(
                """
                INSERT INTO payments (invoice_id, amount_paise, paid_on, method, reference)
                VALUES (?, ?, ?, ?, ?)
                """,
                (invoice_id, part, (today - timedelta(days=paid_ago)).isoformat(),
                 method, reference),
            )


def seed(conn: sqlite3.Connection, today: date | None = None) -> None:
    """Insert all demo data in ONE transaction: either everything or nothing."""
    today = today or db.now_ist().date()
    rng = random.Random(42)
    with db.transaction(conn):
        user_id = create_user(conn)
        client_ids = create_clients(conn, user_id)
        project_ids = create_projects(conn, client_ids, today)
        create_time_logs(conn, project_ids, today, rng)
        create_invoices(conn, project_ids, today)


def main() -> None:
    path = db.get_db_path()
    if os.path.exists(path):
        os.remove(path)
    conn = db.connect(path)
    try:
        db.init_db(conn)
        seed(conn)
        counts = conn.execute(
            """
            SELECT (SELECT COUNT(*) FROM clients),   (SELECT COUNT(*) FROM projects),
                   (SELECT COUNT(*) FROM time_logs), (SELECT COUNT(*) FROM invoices),
                   (SELECT COUNT(*) FROM payments)
            """
        ).fetchone()
    finally:
        conn.close()
    print(f"Seeded {path}")
    print("clients={} projects={} time_logs={} invoices={} payments={}".format(*counts))
    print(f"Login: {DEMO_EMAIL} / {DEMO_PASSWORD}")


if __name__ == "__main__":
    main()
