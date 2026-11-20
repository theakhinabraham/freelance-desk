-- =====================================================================
-- FreelanceDesk database schema (SQLite)
--
-- Conventions
--   * Money is stored as INTEGER paise (1 rupee = 100 paise). Never REAL,
--     because floating point cannot represent 0.10 exactly.
--   * Dates are ISO-8601 TEXT:  'YYYY-MM-DD'  or  'YYYY-MM-DD HH:MM:SS'
--     (Indian Standard Time). ISO strings sort in chronological order, so
--     plain < and > comparisons on them are correct.
--   * Foreign keys are only enforced when every connection runs
--     PRAGMA foreign_keys = ON  (done in app/db.py).
--   * Every CREATE uses IF NOT EXISTS so this file can be run repeatedly.
--
-- Ownership chain (used to scope every query to one user):
--   users -> clients -> projects -> time_logs
--                               -> invoices -> payments
-- =====================================================================


-- ---------------------------------------------------------------------
-- users: one row per freelancer account
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    name          TEXT    NOT NULL CHECK (length(trim(name)) > 0),
    -- COLLATE NOCASE makes the UNIQUE check case-insensitive,
    -- so 'Asha@x.com' and 'asha@x.com' cannot both register.
    email         TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT    NOT NULL,
    -- 'now' is UTC in SQLite; +330 minutes converts it to IST (UTC+5:30).
    created_at    TEXT    NOT NULL DEFAULT (datetime('now', '+330 minutes'))
);


-- ---------------------------------------------------------------------
-- clients: belongs to exactly one user
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS clients (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    name       TEXT    NOT NULL CHECK (length(trim(name)) > 0),
    email      TEXT,
    phone      TEXT,
    notes      TEXT,
    created_at TEXT    NOT NULL DEFAULT (datetime('now', '+330 minutes'))
);

-- Every page lists "my clients": WHERE user_id = ?. Without this index
-- SQLite would scan the whole clients table for each request.
CREATE INDEX IF NOT EXISTS idx_clients_user ON clients(user_id);


-- ---------------------------------------------------------------------
-- projects: belongs to one client
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS projects (
    id                INTEGER PRIMARY KEY,
    -- RESTRICT: a client that still has projects cannot be deleted.
    client_id         INTEGER NOT NULL REFERENCES clients(id) ON DELETE RESTRICT,
    title             TEXT    NOT NULL CHECK (length(trim(title)) > 0),
    billing_type      TEXT    NOT NULL CHECK (billing_type IN ('hourly', 'fixed')),
    hourly_rate_paise INTEGER CHECK (hourly_rate_paise IS NULL OR hourly_rate_paise > 0),
    fixed_price_paise INTEGER CHECK (fixed_price_paise IS NULL OR fixed_price_paise > 0),
    estimated_hours   INTEGER CHECK (estimated_hours IS NULL OR estimated_hours > 0),
    status            TEXT    NOT NULL DEFAULT 'active'
                      CHECK (status IN ('active', 'completed', 'on_hold', 'cancelled')),
    start_date        TEXT,
    deadline          TEXT,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now', '+330 minutes')),

    -- Table-level CHECK: the price column that matches the billing type
    -- must be filled in. An hourly project needs a rate; a fixed one a price.
    CHECK (
        (billing_type = 'hourly' AND hourly_rate_paise IS NOT NULL)
     OR (billing_type = 'fixed'  AND fixed_price_paise IS NOT NULL)
    ),
    -- A deadline cannot be before the start date (skipped if either is NULL).
    CHECK (start_date IS NULL OR deadline IS NULL OR deadline >= start_date)
);

-- Joining clients -> projects and listing a client's projects both
-- filter on client_id. (SQLite does NOT index foreign keys automatically.)
CREATE INDEX IF NOT EXISTS idx_projects_client ON projects(client_id);


-- ---------------------------------------------------------------------
-- invoices: belongs to one project
-- (created before time_logs because time_logs references it)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS invoices (
    id             INTEGER PRIMARY KEY,
    -- RESTRICT: financial records must never disappear with a project.
    project_id     INTEGER NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    -- Format FD-YYYY-NNNN. GLOB is SQLite's pattern match; [0-9] is one digit.
    invoice_number TEXT    NOT NULL UNIQUE
                   CHECK (invoice_number GLOB 'FD-[0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9]'),
    issue_date     TEXT    NOT NULL,
    due_date       TEXT    NOT NULL,
    amount_paise   INTEGER NOT NULL CHECK (amount_paise > 0),
    status         TEXT    NOT NULL DEFAULT 'draft'
                   CHECK (status IN ('draft', 'sent', 'paid', 'overdue', 'cancelled')),
    CHECK (due_date >= issue_date)
);

-- Listing a project's invoices, and the projects -> invoices join.
CREATE INDEX IF NOT EXISTS idx_invoices_project ON invoices(project_id);

-- Composite index for the overdue check:
--   WHERE status = 'sent' AND due_date < today
-- The equality column (status) comes first and the range column
-- (due_date) second, so SQLite can jump straight to the 'sent' rows and
-- read only those past the due date.
CREATE INDEX IF NOT EXISTS idx_invoices_status_due ON invoices(status, due_date);


-- ---------------------------------------------------------------------
-- time_logs: belongs to one project; optionally billed on one invoice
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS time_logs (
    id               INTEGER PRIMARY KEY,
    -- CASCADE: a time log has no meaning without its project.
    project_id       INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    -- NULL = not billed yet. Set when the log is included in an invoice.
    -- SET NULL: if a draft invoice is deleted, its hours become unbilled again.
    invoice_id       INTEGER REFERENCES invoices(id) ON DELETE SET NULL,
    started_at       TEXT    NOT NULL,
    ended_at         TEXT,               -- NULL while the timer is running
    duration_minutes INTEGER,            -- filled in when the timer stops
    description      TEXT,

    -- The timer must end after it started.
    CHECK (ended_at IS NULL OR ended_at > started_at),
    -- A running timer has no duration yet; a finished one must have one.
    -- Note the explicit IS NOT NULL: a CHECK only fails when its result is
    -- FALSE. "NULL >= 0" evaluates to NULL (unknown), which would PASS.
    CHECK (
        (ended_at IS NULL     AND duration_minutes IS NULL)
     OR (ended_at IS NOT NULL AND duration_minutes IS NOT NULL AND duration_minutes >= 0)
    )
);

-- Summing hours per project (effective rate, unbilled hours, invoices).
CREATE INDEX IF NOT EXISTS idx_time_logs_project ON time_logs(project_id);

-- Finding the logs billed on an invoice, and the ON DELETE SET NULL
-- action (SQLite must find the child rows when an invoice is deleted).
CREATE INDEX IF NOT EXISTS idx_time_logs_invoice ON time_logs(invoice_id);

-- "Hours today" and "hours this week" filter on a started_at range.
CREATE INDEX IF NOT EXISTS idx_time_logs_started ON time_logs(started_at);

-- PARTIAL index: only rows WHERE ended_at IS NULL are stored in it.
-- At most one row per user is ever running, so the index stays tiny and
-- the "is a timer running?" check (and the triggers below) is instant.
CREATE INDEX IF NOT EXISTS idx_time_logs_running ON time_logs(project_id)
    WHERE ended_at IS NULL;


-- ---------------------------------------------------------------------
-- Rule: at most ONE running timer per user.
--
-- A partial UNIQUE index cannot enforce this, because time_logs has no
-- user_id column (the user is two joins away: project -> client -> user).
-- Adding user_id to time_logs would duplicate data and break 3NF, so a
-- trigger enforces the rule instead.
--
-- Both triggers find the user who owns NEW.project_id, then abort if that
-- user already has another log with ended_at IS NULL.
-- ---------------------------------------------------------------------
CREATE TRIGGER IF NOT EXISTS trg_one_running_timer_insert
BEFORE INSERT ON time_logs
WHEN NEW.ended_at IS NULL
BEGIN
    SELECT RAISE(ABORT, 'A timer is already running for this user')
    WHERE EXISTS (
        SELECT 1
        FROM time_logs t
        JOIN projects  p ON p.id = t.project_id
        JOIN clients   c ON c.id = p.client_id
        WHERE t.ended_at IS NULL
          AND c.user_id = (SELECT c2.user_id
                           FROM projects p2
                           JOIN clients  c2 ON c2.id = p2.client_id
                           WHERE p2.id = NEW.project_id)
    );
END;

-- Same rule when an existing log is changed back to "running"
-- (ended_at set to NULL) or moved to another project while running.
CREATE TRIGGER IF NOT EXISTS trg_one_running_timer_update
BEFORE UPDATE OF ended_at, project_id ON time_logs
WHEN NEW.ended_at IS NULL
BEGIN
    SELECT RAISE(ABORT, 'A timer is already running for this user')
    WHERE EXISTS (
        SELECT 1
        FROM time_logs t
        JOIN projects  p ON p.id = t.project_id
        JOIN clients   c ON c.id = p.client_id
        WHERE t.ended_at IS NULL
          AND t.id <> NEW.id               -- ignore the row being updated
          AND c.user_id = (SELECT c2.user_id
                           FROM projects p2
                           JOIN clients  c2 ON c2.id = p2.client_id
                           WHERE p2.id = NEW.project_id)
    );
END;


-- ---------------------------------------------------------------------
-- payments: belongs to one invoice; an invoice may have many (partial)
-- payments. "Is it fully paid?" is SUM(amount_paise) >= invoice amount.
-- Overpayment is blocked in the application inside a transaction.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS payments (
    id           INTEGER PRIMARY KEY,
    invoice_id   INTEGER NOT NULL REFERENCES invoices(id) ON DELETE RESTRICT,
    amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
    paid_on      TEXT    NOT NULL,
    method       TEXT    NOT NULL CHECK (method IN ('upi', 'bank', 'cash', 'other')),
    reference    TEXT
);

-- Summing payments per invoice (balance due, status update).
CREATE INDEX IF NOT EXISTS idx_payments_invoice ON payments(invoice_id);

-- "Earned this month" and "income by month" filter and group on paid_on.
CREATE INDEX IF NOT EXISTS idx_payments_paid_on ON payments(paid_on);
