"""Authentication: password hashing, the logged-in-user dependency, and the
register / login / logout routes.

Password storage format:  pbkdf2_sha256$<iterations>$<salt hex>$<hash hex>
A random salt per user means two users with the same password get
different hashes, so precomputed (rainbow) tables are useless.
"""

import hashlib
import hmac
import secrets
import sqlite3

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from app import db
from app.forms import validate_registration
from app.templating import flash, render

ALGORITHM = "pbkdf2_sha256"
ITERATIONS = 600_000  # OWASP 2023 recommendation for PBKDF2-SHA256


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    return f"{ALGORITHM}${ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, iterations, salt_hex, hash_hex = stored_hash.split("$")
    except ValueError:
        return False
    if algorithm != ALGORITHM:
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
    )
    # compare_digest takes the same time whether or not the hashes match,
    # so an attacker cannot learn anything from the response time.
    return hmac.compare_digest(digest.hex(), hash_hex)


# ---------------------------------------------------------------------------
# Sessions and routes
#
# After login the session cookie holds only {"user_id": <id>}. Starlette's
# SessionMiddleware signs the cookie with a secret key, so a user cannot
# edit it to become someone else.
# ---------------------------------------------------------------------------

router = APIRouter()

# Used when the email is unknown, so that a failed login takes the same time
# whether or not the account exists (this hides which emails are registered).
_DUMMY_HASH = hash_password("not-a-real-password")


class LoginRequired(Exception):
    """Raised by current_user. main.py turns it into a redirect to /login."""


def current_user(request: Request, conn: sqlite3.Connection = Depends(db.get_db)) -> sqlite3.Row:
    """FastAPI dependency: the logged-in user's row, or redirect to /login."""
    user_id = request.session.get("user_id")
    if user_id is None:
        raise LoginRequired()
    user = conn.execute("SELECT id, name, email FROM users WHERE id = ?", (user_id,)).fetchone()
    if user is None:  # the account no longer exists
        request.session.clear()
        raise LoginRequired()
    return user


def redirect(url: str) -> RedirectResponse:
    # 303 See Other: after a POST, the browser follows with a GET, so
    # refreshing the page does not resubmit the form.
    return RedirectResponse(url, status_code=303)


@router.get("/register")
def register_form(request: Request):
    return render(request, "auth/register.html", values={}, errors={})


@router.post("/register")
async def register(request: Request, conn: sqlite3.Connection = Depends(db.get_db)):
    form = await request.form()
    data, errors = validate_registration(form)
    if not errors:
        try:
            cur = conn.execute(
                "INSERT INTO users (name, email, password_hash) VALUES (?, ?, ?)",
                (data["name"], data["email"], hash_password(data["password"])),
            )
        except sqlite3.IntegrityError:  # UNIQUE(email) rejected it
            errors["email"] = "An account with this email already exists."
        else:
            request.session.clear()
            request.session["user_id"] = cur.lastrowid
            flash(request, f"Welcome, {data['name']}!")
            return redirect("/")
    return render(request, "auth/register.html", status_code=400,
                  values={"name": data["name"], "email": data["email"]}, errors=errors)


@router.get("/login")
def login_form(request: Request):
    return render(request, "auth/login.html", email="", error=None)


@router.post("/login")
async def login(request: Request, conn: sqlite3.Connection = Depends(db.get_db)):
    form = await request.form()
    email = (form.get("email") or "").strip().lower()
    password = form.get("password") or ""
    user = conn.execute(
        "SELECT id, name, password_hash FROM users WHERE email = ?", (email,)
    ).fetchone()
    stored = user["password_hash"] if user else _DUMMY_HASH
    if verify_password(password, stored) and user is not None:
        # Clear the old session first, so a session from before login
        # cannot be reused after it (prevents session fixation).
        request.session.clear()
        request.session["user_id"] = user["id"]
        return redirect("/")
    # Same message for "no such email" and "wrong password".
    return render(request, "auth/login.html", status_code=400,
                  email=email, error="Invalid email or password.")


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    flash(request, "You have been logged out.")
    return redirect("/login")
