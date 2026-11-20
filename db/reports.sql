-- =====================================================================
-- FreelanceDesk reports
--
-- Each query starts with a line   -- name: <query_name>
-- The app loads the queries from this file by name (app/reports.py), so
-- what you read here is exactly what the app runs.
--
-- Parameters use NUMBERED placeholders: ?1, ?2 ... A numbered placeholder
-- can appear several times but is bound once. Every query takes:
--     ?1 = the logged-in user's id   (every report is scoped to one user)
-- and some take a second parameter, documented above the query.
--
-- To try a query in the sqlite3 shell:
--     sqlite3 freelancedesk.db
--     .parameter set ?1 1
--     .parameter set ?2 '2026-11-19'
--     <paste the query>
--
-- Money is INTEGER paise. A rate in "paise per hour" is
--     paise * 60 / minutes
-- computed with integer arithmetic; adding  minutes / 2  before dividing
-- rounds to the nearest paisa instead of always rounding down.
-- =====================================================================


-- ---------------------------------------------------------------------
-- REPORT 1a: Effective hourly rate per PROJECT, lowest first.
--
--   effective rate = total payments received / total hours logged
--
-- Why two CTEs (WITH ... AS) instead of one big JOIN?
-- A project has MANY payments and MANY time logs. Joining both to the
-- project in one query would pair every payment with every log
-- (3 payments x 10 logs = 30 rows), and the SUMs would be 10x and 3x too
-- big. This is the "fan-out" problem. So each side is added up separately
-- first (one row per project), and then the two are joined.
--
-- NULLIF(minutes, 0) turns 0 into NULL, and x / NULL is NULL, so a project
-- with no hours gets no rate instead of a division-by-zero error.
-- "ORDER BY rate IS NULL" puts those projects last.
-- ---------------------------------------------------------------------
-- name: effective_rate_by_project
WITH received AS (
    SELECT i.project_id, SUM(pay.amount_paise) AS received_paise
    FROM payments pay
    JOIN invoices i ON i.id = pay.invoice_id
    GROUP BY i.project_id
),
logged AS (
    SELECT project_id, SUM(duration_minutes) AS minutes
    FROM time_logs
    WHERE ended_at IS NOT NULL                 -- a running timer has no duration yet
    GROUP BY project_id
)
SELECT p.id                          AS project_id,
       p.title                       AS project_title,
       c.id                          AS client_id,
       c.name                        AS client_name,
       p.billing_type,
       p.hourly_rate_paise           AS quoted_rate_paise,
       COALESCE(r.received_paise, 0) AS received_paise,
       COALESCE(l.minutes, 0)        AS minutes,
       (COALESCE(r.received_paise, 0) * 60 + l.minutes / 2)
           / NULLIF(l.minutes, 0)    AS effective_rate_paise
FROM projects p
JOIN clients c        ON c.id = p.client_id
LEFT JOIN received r  ON r.project_id = p.id   -- LEFT: keep projects with no payments
LEFT JOIN logged   l  ON l.project_id = p.id   -- LEFT: keep projects with no hours
WHERE c.user_id = ?1
ORDER BY effective_rate_paise IS NULL, effective_rate_paise, p.title;


-- ---------------------------------------------------------------------
-- REPORT 1b: Effective hourly rate per CLIENT, lowest first.
-- Same idea as 1a, but both sides are grouped by client (through projects).
-- ---------------------------------------------------------------------
-- name: effective_rate_by_client
WITH received AS (
    SELECT p.client_id, SUM(pay.amount_paise) AS received_paise
    FROM payments pay
    JOIN invoices i ON i.id = pay.invoice_id
    JOIN projects p ON p.id = i.project_id
    GROUP BY p.client_id
),
logged AS (
    SELECT p.client_id, SUM(t.duration_minutes) AS minutes
    FROM time_logs t
    JOIN projects p ON p.id = t.project_id
    WHERE t.ended_at IS NOT NULL
    GROUP BY p.client_id
)
SELECT c.id                          AS client_id,
       c.name                        AS client_name,
       COALESCE(r.received_paise, 0) AS received_paise,
       COALESCE(l.minutes, 0)        AS minutes,
       (COALESCE(r.received_paise, 0) * 60 + l.minutes / 2)
           / NULLIF(l.minutes, 0)    AS effective_rate_paise
FROM clients c
LEFT JOIN received r ON r.client_id = c.id
LEFT JOIN logged   l ON l.client_id = c.id
WHERE c.user_id = ?1
ORDER BY effective_rate_paise IS NULL, effective_rate_paise, c.name;


-- ---------------------------------------------------------------------
-- REPORT 2: Overdue invoices with days overdue.
--   ?2 = today ('YYYY-MM-DD')
--
-- julianday() turns a date into a day number, so subtracting two of them
-- gives the number of days between the dates.
-- Status 'sent' is included as well as 'overdue', so the report is
-- correct even if refresh_overdue() has not run yet today.
-- The balance is a correlated subquery: it runs once for each invoice row.
-- ---------------------------------------------------------------------
-- name: overdue_invoices
SELECT i.id                                            AS invoice_id,
       i.invoice_number,
       c.name                                          AS client_name,
       p.title                                         AS project_title,
       i.due_date,
       CAST(julianday(?2) - julianday(i.due_date) AS INTEGER) AS days_overdue,
       i.amount_paise
         - COALESCE((SELECT SUM(pay.amount_paise)
                     FROM payments pay
                     WHERE pay.invoice_id = i.id), 0)  AS balance_paise
FROM invoices i
JOIN projects p ON p.id = i.project_id
JOIN clients  c ON c.id = p.client_id
WHERE c.user_id = ?1
  AND i.status IN ('sent', 'overdue')
  AND i.due_date < ?2
ORDER BY days_overdue DESC;


-- ---------------------------------------------------------------------
-- REPORT 3: Income for each of the last 12 months, INCLUDING months with
-- no income.
--   ?2 = today ('YYYY-MM-DD')
--
-- A plain GROUP BY on payments would simply skip months with no payments,
-- and the chart would have gaps. So a RECURSIVE CTE first builds the list
-- of the 12 month-start dates:
--   * the anchor row is the month 11 months before this one;
--   * each recursive step adds one month, stopping at the current month.
-- Then the payments are LEFT JOINed onto that list, and empty months get 0.
-- ---------------------------------------------------------------------
-- name: monthly_income
WITH RECURSIVE months(month_start) AS (
    SELECT date(?2, 'start of month', '-11 months')
    UNION ALL
    SELECT date(month_start, '+1 month')
    FROM months
    WHERE month_start < date(?2, 'start of month')
),
my_payments AS (
    SELECT pay.paid_on, pay.amount_paise
    FROM payments pay
    JOIN invoices i ON i.id = pay.invoice_id
    JOIN projects p ON p.id = i.project_id
    JOIN clients  c ON c.id = p.client_id
    WHERE c.user_id = ?1
)
SELECT strftime('%Y-%m', m.month_start)  AS month,
       COALESCE(SUM(x.amount_paise), 0)  AS income_paise
FROM months m
LEFT JOIN my_payments x
       ON x.paid_on >= m.month_start
      AND x.paid_on <  date(m.month_start, '+1 month')
GROUP BY m.month_start
ORDER BY m.month_start;


-- ---------------------------------------------------------------------
-- REPORT 4: Unbilled hours per hourly project, and what they are worth.
--
-- WHERE filters rows BEFORE grouping (only finished, unbilled logs).
-- HAVING filters groups AFTER grouping (only projects with some unbilled
-- time). HAVING is needed because WHERE cannot use SUM().
-- ---------------------------------------------------------------------
-- name: unbilled_hours
SELECT p.id                                       AS project_id,
       p.title                                    AS project_title,
       c.name                                     AS client_name,
       COUNT(t.id)                                AS entries,
       SUM(t.duration_minutes)                    AS minutes,
       p.hourly_rate_paise,
       (SUM(t.duration_minutes) * p.hourly_rate_paise + 30) / 60 AS unbilled_value_paise
FROM projects p
JOIN clients   c ON c.id = p.client_id
JOIN time_logs t ON t.project_id = p.id
WHERE c.user_id = ?1
  AND p.billing_type = 'hourly'
  AND t.invoice_id IS NULL
  AND t.ended_at IS NOT NULL
GROUP BY p.id
HAVING SUM(t.duration_minutes) > 0
ORDER BY unbilled_value_paise DESC;


-- ---------------------------------------------------------------------
-- REPORT 5: Top clients by revenue, with each one's share of the total.
--
-- WINDOW FUNCTIONS: unlike GROUP BY, a window function does NOT collapse
-- rows. It computes a value over a "window" of rows and keeps every row.
--   SUM(revenue) OVER ()             -> grand total, repeated on each row
--   RANK() OVER (ORDER BY ... DESC)  -> 1, 2, 3 ... (ties share a rank)
--   SUM(revenue) OVER (ORDER BY ...) -> running total, top client downwards
-- The percentage is display-only, so a REAL (100.0 * ...) is fine here.
-- Stored money stays integer paise.
-- ---------------------------------------------------------------------
-- name: top_clients
WITH client_revenue AS (
    SELECT c.id AS client_id, c.name AS client_name, SUM(pay.amount_paise) AS revenue_paise
    FROM payments pay
    JOIN invoices i ON i.id = pay.invoice_id
    JOIN projects p ON p.id = i.project_id
    JOIN clients  c ON c.id = p.client_id
    WHERE c.user_id = ?1
    GROUP BY c.id
)
SELECT RANK() OVER (ORDER BY revenue_paise DESC)                       AS rank,
       client_name,
       revenue_paise,
       ROUND(100.0 * revenue_paise / SUM(revenue_paise) OVER (), 1)    AS share_pct,
       SUM(revenue_paise) OVER (ORDER BY revenue_paise DESC
                                ROWS UNBOUNDED PRECEDING)              AS running_total_paise
FROM client_revenue
ORDER BY rank, client_name;


-- ---------------------------------------------------------------------
-- REPORT 6: Projects that need attention: logged hours exceed the
-- estimate, OR the effective rate is below a threshold.
--   ?2 = threshold rate in paise per hour (e.g. 100000 = Rs 1,000/h)
--
-- Built in steps with CTEs, each one building on the previous:
--   figures: hours per project (GROUP BY + HAVING), plus payments received
--            from a scalar subquery (which avoids fan-out with the logs)
--   rated:   adds the effective rate and an over-estimate flag
--   final:   keeps only the projects that match either condition
-- A WHERE clause cannot use a column alias from its own SELECT list, which
-- is why the computed columns are filtered one step later.
-- ---------------------------------------------------------------------
-- name: projects_needing_attention
WITH figures AS (
    SELECT p.id                    AS project_id,
           p.title                 AS project_title,
           c.name                  AS client_name,
           p.estimated_hours,
           SUM(t.duration_minutes) AS minutes,
           (SELECT COALESCE(SUM(pay.amount_paise), 0)
              FROM payments pay
              JOIN invoices i ON i.id = pay.invoice_id
             WHERE i.project_id = p.id) AS received_paise
    FROM projects p
    JOIN clients   c ON c.id = p.client_id
    JOIN time_logs t ON t.project_id = p.id AND t.ended_at IS NOT NULL
    WHERE c.user_id = ?1
    GROUP BY p.id
    HAVING SUM(t.duration_minutes) > 0
),
rated AS (
    SELECT *,
           (received_paise * 60 + minutes / 2) / minutes AS effective_rate_paise,
           -- A comparison gives 1 or 0. With no estimate it is NULL -> COALESCE to 0.
           COALESCE(minutes > estimated_hours * 60, 0)   AS over_estimate
    FROM figures
)
SELECT *,
       CASE
         WHEN over_estimate = 1 AND effective_rate_paise < ?2 THEN 'Over estimate and low rate'
         WHEN over_estimate = 1                              THEN 'Over estimate'
         ELSE                                                     'Low effective rate'
       END AS reason
FROM rated
WHERE over_estimate = 1 OR effective_rate_paise < ?2
ORDER BY effective_rate_paise;


-- ---------------------------------------------------------------------
-- DASHBOARD TOTALS: earned this month, outstanding, overdue.
--   ?2 = first day of the current month ('YYYY-MM-01')
--
-- The inner query gives one row per invoice with its balance (amount minus
-- payments; LEFT JOIN + GROUP BY). The outer query uses conditional
-- aggregation, SUM(CASE WHEN ... THEN ... END), to add up two different
-- subsets of those rows in a single pass.
-- Outstanding = every sent or overdue invoice's balance (drafts are not
-- owed yet). Overdue is the part of that which is past its due date.
-- ---------------------------------------------------------------------
-- name: dashboard_totals
SELECT
    (SELECT COALESCE(SUM(pay.amount_paise), 0)
       FROM payments pay
       JOIN invoices i ON i.id = pay.invoice_id
       JOIN projects p ON p.id = i.project_id
       JOIN clients  c ON c.id = p.client_id
      WHERE c.user_id = ?1
        AND pay.paid_on >= ?2)                                        AS earned_this_month_paise,
    COALESCE(SUM(CASE WHEN b.status IN ('sent', 'overdue') THEN b.balance_paise END), 0)
                                                                      AS outstanding_paise,
    COALESCE(SUM(CASE WHEN b.status = 'overdue' THEN b.balance_paise END), 0)
                                                                      AS overdue_paise
FROM (
    SELECT i.status,
           i.amount_paise - COALESCE(SUM(pay.amount_paise), 0) AS balance_paise
    FROM invoices i
    JOIN projects p ON p.id = i.project_id
    JOIN clients  c ON c.id = p.client_id
    LEFT JOIN payments pay ON pay.invoice_id = i.id
    WHERE c.user_id = ?1
    GROUP BY i.id
) AS b;
