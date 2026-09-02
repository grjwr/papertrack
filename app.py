"""
PaperTrack - a small Streamlit app for tracking manuscript submission status
shared between a corresponding author and co-authors.

Run:  streamlit run app.py
"""

import os
import re
import json
import sqlite3
import hashlib
import datetime as dt

import streamlit as st

try:
    import requests
except ImportError:
    requests = None

DB_PATH = os.environ.get("PAPERTRACK_DB", "papertrack.db")
UPLOAD_DIR = os.environ.get("PAPERTRACK_UPLOADS", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

STATUSES = [
    "Draft / Not submitted",
    "Submitted to Journal",
    "With Editor",
    "Under Review",
    "Required Reviews Completed",
    "Decision in Process",
    "Major Revision",
    "Minor Revision",
    "Revision Submitted",
    "Accepted",
    "In Production",
    "Published",
    "Rejected",
    "Withdrawn",
]

PUBLISHED_LIKE = {"Published", "In Production", "Accepted"}
CLOSED_LIKE = {"Rejected", "Withdrawn"}


# --------------------------------------------------------------------------
# Database layer: SQLite locally, Postgres when DATABASE_URL is set.
# All SQL below uses "?" placeholders; the wrapper rewrites them for Postgres
# so the individual queries never have to change.
# --------------------------------------------------------------------------
def _database_url():
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    try:
        return st.secrets["DATABASE_URL"]
    except Exception:
        return None


def _admin_emails():
    """Emails allowed to manage accounts. Comma separated, from env or secrets.
    If unset, the first registered account is treated as the admin."""
    raw = os.environ.get("ADMIN_EMAILS")
    if not raw:
        try:
            raw = st.secrets["ADMIN_EMAILS"]
        except Exception:
            raw = ""
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


ADMIN_EMAILS = None  # resolved lazily, after st.secrets is reachable


def is_admin(user):
    global ADMIN_EMAILS
    if ADMIN_EMAILS is None:
        ADMIN_EMAILS = _admin_emails()
    if ADMIN_EMAILS:
        return user["email"].lower() in ADMIN_EMAILS
    # no admin configured: the first account created is the admin
    c = conn()
    row = c.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
    c.close()
    return bool(row) and row["id"] == user["id"]


def _invite_code():
    code = os.environ.get("INVITE_CODE")
    if code:
        return code
    try:
        return st.secrets["INVITE_CODE"]
    except Exception:
        return "changeme"


INVITE_CODE = _invite_code()

USE_PG = bool(_database_url())

if USE_PG:
    import psycopg
    from psycopg.rows import dict_row


class Cur:
    def __init__(self, rows, lastrowid=None):
        self._rows = rows
        self.lastrowid = lastrowid

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class DB:
    def __init__(self):
        if USE_PG:
            self._c = psycopg.connect(_database_url(), row_factory=dict_row)
        else:
            self._c = sqlite3.connect(DB_PATH, check_same_thread=False)
            self._c.row_factory = sqlite3.Row
            self._c.execute("PRAGMA foreign_keys = ON")

    # Tables that have no "id" primary key, so RETURNING id would fail.
    NO_ID_TABLES = ("paper_authors",)

    def execute(self, sql, params=()):
        want_id = False
        if USE_PG:
            sql = sql.replace("?", "%s")
            head = sql.strip().upper()
            has_id = not any(t.upper() in head for t in self.NO_ID_TABLES)
            if head.startswith("INSERT OR IGNORE"):
                sql = sql.replace("INSERT OR IGNORE", "INSERT", 1).rstrip().rstrip(";")
                sql += " ON CONFLICT DO NOTHING"
            elif head.startswith("INSERT") and has_id:
                sql = sql.rstrip().rstrip(";") + " RETURNING id"
                want_id = True
        cur = self._c.cursor()
        cur.execute(sql, params)
        if want_id:
            row = cur.fetchone()
            return Cur([], row["id"] if row else None)
        rows = []
        if cur.description is not None:
            rows = [dict(r) for r in cur.fetchall()]
        return Cur(rows, None if USE_PG else cur.lastrowid)

    def executescript(self, sql):
        if USE_PG:
            self._c.execute(sql)
        else:
            self._c.executescript(sql)

    def commit(self):
        self._c.commit()

    def close(self):
        self._c.close()


def conn():
    return DB()


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'author',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS papers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    journal_name TEXT,
    manuscript_number TEXT,
    date_submitted TEXT,
    file_path TEXT,
    status TEXT NOT NULL DEFAULT 'Draft / Not submitted',
    status_mode TEXT NOT NULL DEFAULT 'manual',
    tracking_link TEXT,
    last_auto_check TEXT,
    last_auto_result TEXT,
    notes TEXT,
    fetched_details TEXT,
    owner_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_authors (
    paper_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    author_order INTEGER DEFAULT 0,
    PRIMARY KEY (paper_id, user_id)
);
CREATE TABLE IF NOT EXISTS status_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    source TEXT NOT NULL,
    changed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS prior_submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id INTEGER NOT NULL,
    journal_name TEXT NOT NULL,
    date_submitted TEXT,
    date_decided TEXT,
    outcome TEXT,
    comments_json TEXT NOT NULL DEFAULT '[]'
);
"""


def init_db():
    ddl = SCHEMA.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY") if USE_PG else SCHEMA
    c = conn()
    c.executescript(ddl)
    # migration for databases created before fetched_details existed
    try:
        c.execute("ALTER TABLE papers ADD COLUMN fetched_details TEXT")
    except Exception:
        pass
    c.commit()
    c.close()


def hash_pw(pw: str) -> str:
    return hashlib.sha256(("papertrack$" + pw).encode()).hexdigest()


def now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Data access helpers
# --------------------------------------------------------------------------
def get_user_by_email(email):
    c = conn()
    row = c.execute("SELECT * FROM users WHERE email = ?", (email.lower().strip(),)).fetchone()
    c.close()
    return row


def create_user(name, email, password, role):
    c = conn()
    c.execute(
        "INSERT INTO users (name, email, password_hash, role, created_at) VALUES (?,?,?,?,?)",
        (name.strip(), email.lower().strip(), hash_pw(password), role, now()),
    )
    c.commit()
    c.close()


def all_users():
    c = conn()
    rows = c.execute("SELECT * FROM users ORDER BY name").fetchall()
    c.close()
    return rows


def papers_for_user(user_id, only_mine=False):
    c = conn()
    if only_mine:
        rows = c.execute(
            "SELECT * FROM papers WHERE owner_id = ? ORDER BY updated_at DESC", (user_id,)
        ).fetchall()
    else:
        rows = c.execute("SELECT * FROM papers ORDER BY updated_at DESC").fetchall()
    c.close()
    return rows


def get_paper(pid):
    c = conn()
    row = c.execute("SELECT * FROM papers WHERE id = ?", (pid,)).fetchone()
    c.close()
    return row


def authors_of(pid):
    c = conn()
    rows = c.execute(
        """SELECT u.* FROM users u
           JOIN paper_authors pa ON pa.user_id = u.id
           WHERE pa.paper_id = ? ORDER BY pa.author_order, u.name""",
        (pid,),
    ).fetchall()
    c.close()
    return rows


def papers_of_author(user_id):
    c = conn()
    rows = c.execute(
        """SELECT p.* FROM papers p
           LEFT JOIN paper_authors pa ON pa.paper_id = p.id
           WHERE pa.user_id = ? OR p.owner_id = ?
           GROUP BY p.id ORDER BY p.updated_at DESC""",
        (user_id, user_id),
    ).fetchall()
    c.close()
    return rows


def set_status(pid, status, source):
    c = conn()
    cur = c.execute("SELECT status FROM papers WHERE id = ?", (pid,)).fetchone()
    if cur and cur["status"] == status:
        c.close()
        return False
    c.execute("UPDATE papers SET status = ?, updated_at = ? WHERE id = ?", (status, now(), pid))
    c.execute(
        "INSERT INTO status_history (paper_id, status, source, changed_at) VALUES (?,?,?,?)",
        (pid, status, source, now()),
    )
    c.commit()
    c.close()
    return True


def history_of(pid):
    c = conn()
    rows = c.execute(
        "SELECT * FROM status_history WHERE paper_id = ? ORDER BY changed_at DESC", (pid,)
    ).fetchall()
    c.close()
    return rows


def role_on_paper(user_id, paper):
    """A person's role is a property of the paper, not of their account."""
    if paper["owner_id"] == user_id:
        return "corresponding"
    return "co-author"


def role_summary(user_id):
    """How many papers this person is corresponding author on vs co-author."""
    papers = papers_of_author(user_id)
    corr = sum(1 for p in papers if p["owner_id"] == user_id)
    return corr, len(papers) - corr


def delete_paper(pid):
    c = conn()
    for table in ("prior_submissions", "status_history", "paper_authors"):
        c.execute(f"DELETE FROM {table} WHERE paper_id = ?", (pid,))
    c.execute("DELETE FROM papers WHERE id = ?", (pid,))
    c.commit()
    c.close()


def owned_paper_count(user_id):
    c = conn()
    row = c.execute("SELECT COUNT(*) AS n FROM papers WHERE owner_id = ?", (user_id,)).fetchone()
    c.close()
    return row["n"] if row else 0


def delete_user(user_id):
    """Only safe for users who own no papers; their co-author links are removed."""
    c = conn()
    c.execute("DELETE FROM paper_authors WHERE user_id = ?", (user_id,))
    c.execute("DELETE FROM users WHERE id = ?", (user_id,))
    c.commit()
    c.close()


def prior_subs(pid):
    c = conn()
    rows = c.execute(
        "SELECT * FROM prior_submissions WHERE paper_id = ? ORDER BY id DESC", (pid,)
    ).fetchall()
    c.close()
    return rows


# --------------------------------------------------------------------------
# Auto status check
# --------------------------------------------------------------------------
AUTO_PATTERNS = [
    (r"under\s+review", "Under Review"),
    (r"reviews?\s+complete", "Required Reviews Completed"),
    (r"decision\s+in\s+process", "Decision in Process"),
    (r"with\s+editor", "With Editor"),
    (r"revi(?:se|sion)\s*[-–]?\s*major", "Major Revision"),
    (r"revi(?:se|sion)\s*[-–]?\s*minor", "Minor Revision"),
    (r"revision\s+submitted", "Revision Submitted"),
    (r"\baccept(?:ed)?\b", "Accepted"),
    (r"in\s+production", "In Production"),
    (r"\bpublished\b", "Published"),
    (r"\breject(?:ed)?\b", "Rejected"),
    (r"submitted\s+to\s+journal", "Submitted to Journal"),
]


def _visible_text(html):
    txt = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S | re.I)
    txt = re.sub(r"<br\s*/?>|</tr>|</p>|</div>|</li>", "\n", txt, flags=re.I)
    txt = re.sub(r"</t[dh]>", " | ", txt, flags=re.I)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = (txt.replace("&nbsp;", " ").replace("&amp;", "&")
              .replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'"))
    lines = [re.sub(r"[ \t]+", " ", l).strip(" |") for l in txt.splitlines()]
    return [l for l in lines if l.strip()]


# Label keywords -> the field we can auto-fill. Everything else is still shown
# to the user, just not mapped to a column.
FIELD_HINTS = {
    "manuscript_number": ("manuscript number", "manuscript no", "ms number", "ms id",
                          "paper id", "submission id", "tracking number", "article number"),
    "title": ("article title", "manuscript title", "title"),
    "journal_name": ("journal", "publication"),
    "date_submitted": ("date submitted", "submission date", "initial date submitted",
                       "date of submission", "received", "date received", "submitted on"),
    "status": ("current status", "status", "stage", "editorial status"),
}


ELSEVIER_API = "https://tnlkuelk67.execute-api.us-east-1.amazonaws.com/tracker"


def _prettify(key):
    key = re.sub(r"(?<!^)(?=[A-Z])", " ", str(key)).replace("_", " ").replace("-", " ")
    return key.strip().capitalize()


def _flatten_json(obj, prefix=""):
    """Turn arbitrary JSON into a flat list of (label, value) pairs."""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            label = _prettify(k)
            out.extend(_flatten_json(v, f"{prefix}{label} " if prefix else f"{label} "))
    elif isinstance(obj, list):
        if obj and all(not isinstance(i, (dict, list)) for i in obj):
            out.append((prefix.strip() or "Items", ", ".join(str(i) for i in obj)))
        else:
            for i, item in enumerate(obj, 1):
                out.extend(_flatten_json(item, f"{prefix}{i}. "))
    else:
        if obj not in (None, "", []):
            out.append((prefix.strip(), str(obj)))
    return out


def scrape_elsevier(url):
    """Elsevier Author Hub renders client-side; the data lives in a JSON API
    keyed by the same uuid that appears in the tracking link."""
    m = re.search(r"uuid=([0-9a-fA-F-]{16,})", url)
    if not m:
        return None
    try:
        r = requests.get(ELSEVIER_API, params={"uuid": m.group(1)}, timeout=20,
                         headers={"User-Agent": "Mozilla/5.0 PaperTrack/1.0",
                                  "Accept": "application/json",
                                  "Origin": "https://track.authorhub.elsevier.com",
                                  "Referer": "https://track.authorhub.elsevier.com/"})
    except Exception as e:
        return [], {}, f"Elsevier API request failed: {e}"
    if r.status_code != 200:
        return [], {}, f"Elsevier API returned HTTP {r.status_code}."
    try:
        data = r.json()
    except Exception:
        return [], {}, "Elsevier API did not return JSON."

    details = _flatten_json(data)
    fields = _map_fields(details)
    if not fields.get("status"):
        blob = " ".join(v for _, v in details).lower()
        for pattern, canonical in AUTO_PATTERNS:
            if re.search(pattern, blob):
                fields["status"] = canonical
                break
    return details, fields, f"Read {len(details)} field(s) from Elsevier's tracker."


def _map_fields(details):
    fields = {}
    for label, value in details:
        low = label.lower().strip(" *#")
        for field, keys in FIELD_HINTS.items():
            if field in fields:
                continue
            if any(low == k or low.endswith(k) or low.startswith(k) for k in keys):
                fields[field] = value
    if "status" in fields:
        low = fields["status"].lower()
        for pattern, canonical in AUTO_PATTERNS:
            if re.search(pattern, low):
                fields["status"] = canonical
                break
    return fields


# Lines that are page furniture rather than data.
NOISE = (
    "is this helpful", "need more help", "we use cookies", "all content on this site",
    "terms and conditions", "privacy policy", "copyright", "track your submission",
    "this is a new submission", "watch to learn", "please visit our",
    "peer review status", "detailed reviewer activity", "manuscript details",
    "submission status and date", "cookienotice", "use of cookies",
)

# Headings whose value sits on the following line.
HEADING_FIELDS = {
    "article title": "title",
    "manuscript title": "title",
    "title": "title",
}


def _clean_line(line):
    # markdown links -> their text, then drop bullets/whitespace
    line = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", line)
    line = re.sub(r"^[\s*\-\u2022]+", "", line)
    return re.sub(r"[ \t]+", " ", line).strip()


def _is_noise(line):
    low = line.lower()
    if any(n in low for n in NOISE):
        return True
    # explanatory blurbs: "The meaning of X: ..." and long prose sentences
    if low.startswith("the meaning of"):
        return True
    return False


def parse_pasted_text(text):
    """Fallback: the user copies the tracking page (Ctrl+A, Ctrl+C) and pastes it.
    Works for every publisher, including ones behind a login."""
    raw = [_clean_line(l) for l in text.splitlines()]
    lines = [l for l in raw if l and not _is_noise(l)]

    details, seen = [], {}
    heading_title = None
    i = 0
    while i < len(lines):
        line = lines[i]
        low = line.lower().rstrip(":").strip()

        # "Article Title" on its own line, value on the next
        if low in HEADING_FIELDS and i + 1 < len(lines):
            nxt = lines[i + 1]
            if ":" not in nxt or len(nxt) > 60:
                heading_title = nxt
                i += 2
                continue

        if ":" in line:
            lab, _, val = line.partition(":")
            lab, val = lab.strip(), val.strip()
            # label on one line, value on the next
            if not val and i + 1 < len(lines) and ":" not in lines[i + 1]:
                val = lines[i + 1].strip()
                i += 1
            # a label should look like a label, not a sentence
            if lab and val and len(lab) <= 45 and len(lab.split()) <= 6:
                key = lab.lower()
                if key not in seen:
                    seen[key] = True
                    details.append((lab, val))
        i += 1

    fields = _map_fields(details)

    blob = " ".join(lines).lower()
    if not fields.get("status"):
        for pattern, canonical in AUTO_PATTERNS:
            if re.search(pattern, blob):
                fields["status"] = canonical
                break

    title = heading_title
    if not title:
        # longest plausible line that isn't a label/value pair
        cands = [l for l in lines
                 if len(l) > 35 and ":" not in l and not l.lower().startswith("http")]
        title = max(cands, key=len) if cands else None
    if title:
        fields["title"] = re.sub(r"^\[[^\]]+\]\s*", "", title).strip()

    note = f"Read {len(details)} field(s) from the pasted text." if details \
        else "Couldn't find any labelled fields in that text."
    return details, fields, note


def scrape_tracking_page(url):
    """Pull every label/value pair we can find off a journal tracking page.

    Returns (details, fields, note):
      details -- ordered list of (label, value) exactly as shown on the page
      fields  -- subset mapped onto our own columns
      note    -- human readable outcome
    """
    if requests is None:
        return [], {}, "The `requests` package is not installed."
    if not url or not url.startswith(("http://", "https://")):
        return [], {}, "No valid tracking URL saved."

    if "authorhub.elsevier.com" in url:
        res = scrape_elsevier(url)
        if res is not None:
            return res

    try:
        r = requests.get(url, timeout=20,
                         headers={"User-Agent": "Mozilla/5.0 PaperTrack/1.0"})
    except Exception as e:
        return [], {}, f"Request failed: {e}"
    if r.status_code != 200:
        return [], {}, f"Page returned HTTP {r.status_code}."

    lines = _visible_text(r.text)
    joined = " ".join(lines).lower()
    if re.search(r"(sign in|log in|username|password)", joined) and len(joined) < 6000:
        return [], {}, ("The link looks like a login page. Most journal portals need a "
                        "signed-in session, so the details can't be read automatically.")

    details, seen = [], set()
    for line in lines:
        pair = None
        if "|" in line:
            bits = [b.strip() for b in line.split("|") if b.strip()]
            if len(bits) == 2:
                pair = (bits[0], bits[1])
        if pair is None and ":" in line:
            lab, _, val = line.partition(":")
            lab, val = lab.strip(), val.strip()
            if lab and val and len(lab) <= 60 and len(val) <= 300:
                pair = (lab, val)
        if pair:
            key = pair[0].lower()
            if key not in seen:
                seen.add(key)
                details.append(pair)

    fields = _map_fields(details)
    if not fields.get("status"):
        for pattern, canonical in AUTO_PATTERNS:
            if re.search(pattern, joined):
                fields["status"] = canonical
                break

    if not details and "status" not in fields:
        return [], {}, "Page fetched, but no recognisable details were found on it."
    return details, fields, f"Read {len(details)} field(s) from the page."


def fetch_status_from_link(url):
    """Kept for the dashboard refresh: returns (status_or_None, note)."""
    details, fields, note = scrape_tracking_page(url)
    return fields.get("status"), note


def run_auto_check(paper_row, silent=False):
    details, fields, note = scrape_tracking_page(paper_row["tracking_link"])
    status = fields.get("status")
    c = conn()
    c.execute(
        """UPDATE papers SET last_auto_check = ?, last_auto_result = ?,
           fetched_details = ? WHERE id = ?""",
        (now(), note, json.dumps(details) if details else None, paper_row["id"]),
    )
    # fill in blanks the journal page told us about
    for col in ("manuscript_number", "date_submitted", "journal_name"):
        if fields.get(col) and not paper_row.get(col):
            c.execute(f"UPDATE papers SET {col} = ? WHERE id = ?",
                      (fields[col], paper_row["id"]))
    c.commit()
    c.close()
    if status:
        set_status(paper_row["id"], status, "auto")
    if not silent:
        (st.success if status else st.warning)(note)
    return status, note


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------
st.set_page_config(page_title="PaperTrack", page_icon="📄", layout="wide")
init_db()


def login_view():
    st.title("📄 PaperTrack")
    st.caption("Shared manuscript status board for corresponding authors and co-authors.")
    tab_login, tab_reg = st.tabs(["Sign in", "Register"])

    with tab_login:
        email = st.text_input("Email", key="li_email")
        pw = st.text_input("Password", type="password", key="li_pw")
        if st.button("Sign in", type="primary"):
            u = get_user_by_email(email)
            if u and u["password_hash"] == hash_pw(pw):
                st.session_state.user = dict(u)
                st.rerun()
            else:
                st.error("Wrong email or password.")

    with tab_reg:
        n = st.text_input("Full name", key="rg_name")
        e = st.text_input("Email", key="rg_email")
        p = st.text_input("Password", type="password", key="rg_pw")
        st.caption("You'll be the corresponding author on papers you add, and a "
                   "co-author on papers others add you to. No role to pick.")
        code = st.text_input("Invite code", type="password", key="rg_code",
                             help="Ask the corresponding author for this.")
        if st.button("Create account"):
            if code != INVITE_CODE:
                st.error("Invalid invite code.")
            elif not (n and e and p):
                st.error("Fill in every field.")
            elif get_user_by_email(e):
                st.error("That email is already registered.")
            else:
                create_user(n, e, p, "author")
                st.success("Account created — sign in from the other tab.")


def paper_card(p, expanded=False):
    authors = authors_of(p["id"])
    names = ", ".join(f"{a['name']} ({a['email']})" for a in authors) or "—"
    with st.expander(f"**{p['title']}** · {p['journal_name'] or 'no journal'} · `{p['status']}`", expanded=expanded):
        with st.container():
            st.markdown(
                f"- **Manuscript no.:** {p['manuscript_number'] or '—'}\n"
                f"- **Initial submission:** {p['date_submitted'] or '—'}\n"
                f"- **Authors:** {names}\n"
                f"- **Status mode:** {p['status_mode']}\n"
                f"- **Last updated:** {p['updated_at']}"
            )
            if p["notes"]:
                st.markdown(f"**Notes:** {p['notes']}")
            if p["tracking_link"]:
                st.markdown(f"[Open journal tracking page]({p['tracking_link']})")
            if p["last_auto_check"]:
                st.caption(f"Last auto-check {p['last_auto_check']} — {p['last_auto_result']}")
            if p.get("fetched_details"):
                with st.popover("Details from the journal page"):
                    for lab, val in json.loads(p["fetched_details"]):
                        st.markdown(f"**{lab}:** {val}")

        st.markdown("**Status history**")
        h = history_of(p["id"])
        if h:
            st.dataframe(
                [{"When": r["changed_at"], "Status": r["status"], "Source": r["source"]} for r in h],
                hide_index=True, use_container_width=True,
            )
        else:
            st.caption("No status changes recorded yet.")

        st.markdown("**Previous journal submissions & reviewer comments**")
        ps = prior_subs(p["id"])
        if not ps:
            st.caption("None recorded.")
        for s in ps:
            st.markdown(f"*{s['journal_name']}* — {s['outcome'] or 'outcome not set'} "
                        f"({s['date_submitted'] or '?'} → {s['date_decided'] or '?'})")
            for i, cm in enumerate(json.loads(s["comments_json"]), 1):
                st.markdown(f"&nbsp;&nbsp;&nbsp;&nbsp;{i}. {cm}", unsafe_allow_html=True)


def dashboard_view(user):
    st.subheader("Common dashboard")
    papers = papers_of_author(user["id"])

    active = [p for p in papers if p["status"] not in PUBLISHED_LIKE | CLOSED_LIKE]
    done = [p for p in papers if p["status"] in PUBLISHED_LIKE]
    closed = [p for p in papers if p["status"] in CLOSED_LIKE]

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("All papers", len(papers))
    m2.metric("In progress", len(active))
    m3.metric("Accepted / published", len(done))
    m4.metric("Rejected / withdrawn", len(closed))

    if st.button("🔄 Refresh all auto-tracked papers"):
        auto = [p for p in papers if p["status_mode"] == "auto" and p["tracking_link"]]
        if not auto:
            st.info("No papers are set to automatic tracking.")
        else:
            bar = st.progress(0.0)
            for i, p in enumerate(auto, 1):
                run_auto_check(p, silent=True)
                bar.progress(i / len(auto))
            st.success(f"Checked {len(auto)} paper(s).")
            st.rerun()

    q = st.text_input("Filter by title, journal or manuscript number", "")
    sel = st.multiselect("Status filter", STATUSES, default=[])

    def keep(p):
        if sel and p["status"] not in sel:
            return False
        if q:
            blob = " ".join(str(p[k] or "") for k in ("title", "journal_name", "manuscript_number")).lower()
            return q.lower() in blob
        return True

    shown = [p for p in papers if keep(p)]
    st.caption(f"{len(shown)} paper(s) shown")
    for p in shown:
        paper_card(p)


def authors_view():
    st.subheader("Authors")
    users = all_users()
    if not users:
        st.info("No users yet.")
        return
    # two people can share a name, so disambiguate the tab labels by email
    seen_names = {}
    for u in users:
        seen_names[u["name"]] = seen_names.get(u["name"], 0) + 1
    labels = [f"{u['name']} · {u['email'].split('@')[0]}" if seen_names[u["name"]] > 1
              else u["name"] for u in users]
    tabs = st.tabs(labels)
    for tab, u in zip(tabs, users):
        with tab:
            corr, co = role_summary(u["id"])
            st.markdown(f"**{u['name']}** · {u['email']}")
            st.caption(f"Corresponding author on {corr} paper(s), co-author on {co}.")
            ps = papers_of_author(u["id"])
            if not ps:
                st.caption("No papers linked to this author.")
                continue
            st.metric("Papers", len(ps))
            st.dataframe(
                [{"Title": p["title"], "Journal": p["journal_name"],
                  "Manuscript no.": p["manuscript_number"], "Status": p["status"],
                  "Submitted": p["date_submitted"],
                  "Corresponding": "yes" if p["owner_id"] == u["id"] else "no"} for p in ps],
                hide_index=True, use_container_width=True,
            )
            for p in ps:
                paper_card(p)


def account_view(user):
    st.subheader("My account")
    corr, co = role_summary(user["id"])
    st.markdown(f"**{user['name']}** · {user['email']}")
    st.caption(f"Corresponding author on {corr} paper(s), co-author on {co}.")

    st.markdown("#### Change password")
    with st.form("pwchange"):
        old = st.text_input("Current password", type="password")
        new1 = st.text_input("New password", type="password")
        new2 = st.text_input("Repeat new password", type="password")
        go = st.form_submit_button("Update password")
    if go:
        if hash_pw(old) != user["password_hash"]:
            st.error("Current password is wrong.")
        elif not new1 or new1 != new2:
            st.error("The new passwords don't match.")
        else:
            c = conn()
            c.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                      (hash_pw(new1), user["id"]))
            c.commit()
            c.close()
            st.session_state.user["password_hash"] = hash_pw(new1)
            st.success("Password updated.")

    st.markdown("#### Close my account")
    if corr:
        st.info(f"You are the corresponding author on {corr} paper(s). Delete those "
                "papers from 'Manage my papers' first, then you can close the account.")
        return
    st.warning("This removes your account and detaches you from any papers you are "
               "listed on. The papers themselves stay.")
    sure = st.checkbox("Yes, close my account")
    if st.button("Close account", type="primary", disabled=not sure):
        delete_user(user["id"])
        st.session_state.pop("user", None)
        st.success("Account closed.")
        st.rerun()


def users_view(current_user):
    st.subheader("Registered users")
    if not is_admin(current_user):
        st.error("Only the account administrator can manage accounts.")
        return
    st.caption("Accounts that are not corresponding author on any paper can be "
               "removed here. Two accounts can share a name, so check the email.")
    users = all_users()

    rows = []
    for u in users:
        rows.append({
            "id": u["id"],
            "Name": u["name"],
            "Email": u["email"],
            "Corresponding on": owned_paper_count(u["id"]),
            "Co-author on": len(papers_of_author(u["id"])) - owned_paper_count(u["id"]),
            "Admin": "yes" if is_admin(u) else "",
            "Joined": u["created_at"][:10],
        })
    st.dataframe(rows, hide_index=True, use_container_width=True)

    dupes = {}
    for u in users:
        dupes.setdefault(u["name"].strip().lower(), []).append(u)
    repeated = {k: v for k, v in dupes.items() if len(v) > 1}
    if repeated:
        st.info("These names belong to more than one account: " +
                "; ".join(f"{v[0]['name']} → " + ", ".join(x["email"] for x in v)
                          for v in repeated.values()))

    st.markdown("#### Remove an account")
    others = [u for u in users if u["id"] != current_user["id"]]
    if not others:
        st.caption("No other accounts.")
        return
    label = {f"{u['name']} <{u['email']}> — corresponding on "
             f"{owned_paper_count(u['id'])} paper(s)": u for u in others}
    pick = st.selectbox("Account", list(label))
    target = label[pick]
    owned = owned_paper_count(target["id"])

    if owned:
        st.warning(f"{target['name']} is the corresponding author on {owned} paper(s). "
                   "Those papers must be deleted or handed over first.")
        return
    st.write(f"Removing **{target['name']}** ({target['email']}) also detaches them "
             "from any papers they are listed on.")
    sure = st.checkbox("Yes, remove this account", key=f"udel{target['id']}")
    if st.button("Remove account", type="primary", disabled=not sure):
        delete_user(target["id"])
        st.success(f"Removed {target['email']}.")
        st.rerun()


def new_paper_view(user):
    st.subheader("Add a paper")
    users = all_users()
    st.caption("Only the title is required. Everything else can be filled in later.")

    # --- optional: pull details off the journal tracking page ---
    st.markdown("##### Import details (optional)")
    t_link, t_paste = st.tabs(["From tracking link", "From pasted page"])

    with t_link:
        lc1, lc2 = st.columns([4, 1])
        link = lc1.text_input("Tracking link", key="np_link", label_visibility="collapsed",
                              placeholder="https://...")
        if lc2.button("Fetch details", use_container_width=True):
            if not link.strip():
                st.warning("Paste a tracking link first.")
            else:
                with st.spinner("Reading the journal page..."):
                    details, fields, note = scrape_tracking_page(link.strip())
                st.session_state.np_fetched = details
                st.session_state.np_fields = fields
                (st.success if details else st.warning)(note)
        st.caption("Works for Elsevier Author Hub links. Portals that need a login "
                   "can't be read this way — use the other tab for those.")

    with t_paste:
        st.caption("Open the tracking page in your browser, select everything "
                   "(Ctrl+A), copy (Ctrl+C), and paste it below.")
        pasted = st.text_area("Pasted page", key="np_paste", height=140,
                              label_visibility="collapsed")
        if st.button("Read pasted text"):
            if not pasted.strip():
                st.warning("Paste the page contents first.")
            else:
                details, fields, note = parse_pasted_text(pasted)
                st.session_state.np_fetched = details
                st.session_state.np_fields = fields
                (st.success if details else st.warning)(note)

    fetched = st.session_state.get("np_fetched", [])
    guess = st.session_state.get("np_fields", {})
    if fetched:
        st.markdown("**Everything found on that page:**")
        st.dataframe([{"Field": lab, "Value": val} for lab, val in fetched],
                     hide_index=True, use_container_width=True)
        st.caption("The recognised fields are pre-filled below; edit anything that looks wrong.")

    with st.form("newpaper"):
        title = st.text_input("Title of the paper *", value=guess.get("title", ""))
        journal = st.text_input("Journal name", value=guess.get("journal_name", ""))
        mno = st.text_input("Manuscript number", value=guess.get("manuscript_number", ""))
        date_sub = st.text_input("Initial date submitted", value=guess.get("date_submitted", ""),
                                 placeholder="YYYY-MM-DD, or leave blank")
        coauthors = st.multiselect(
            "Co-authors", [u["email"] for u in users if u["id"] != user["id"]]
        )
        mode = st.radio("Status source", ["manual", "auto"], horizontal=True,
                        index=1 if fetched else 0,
                        help="Manual before the paper goes under review; auto once the "
                             "journal has given you a tracking link.")
        default_status = guess.get("status") if guess.get("status") in STATUSES else "Submitted to Journal"
        status = st.selectbox("Current status", STATUSES, index=STATUSES.index(default_status))
        notes = st.text_area("Notes")
        ok = st.form_submit_button("Save paper", type="primary")

    if ok:
        if not title.strip():
            st.error("A title is required.")
            return
        c = conn()
        cur = c.execute(
            """INSERT INTO papers (title, journal_name, manuscript_number, date_submitted,
               file_path, status, status_mode, tracking_link, notes, fetched_details,
               owner_id, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (title.strip(), journal.strip() or None, mno.strip() or None,
             date_sub.strip() or None, None, status, mode,
             link.strip() or None, notes.strip() or None,
             json.dumps(fetched) if fetched else None,
             user["id"], now(), now()),
        )
        pid = cur.lastrowid
        c.execute("INSERT INTO paper_authors (paper_id, user_id, author_order) VALUES (?,?,0)",
                  (pid, user["id"]))
        for i, em in enumerate(coauthors, 1):
            u = get_user_by_email(em)
            if u:
                c.execute("INSERT OR IGNORE INTO paper_authors (paper_id, user_id, author_order) VALUES (?,?,?)",
                          (pid, u["id"], i))
        c.execute("INSERT INTO status_history (paper_id, status, source, changed_at) VALUES (?,?,?,?)",
                  (pid, status, "auto" if fetched else "manual", now()))
        c.commit()
        c.close()
        st.session_state.pop("np_fetched", None)
        st.session_state.pop("np_fields", None)
        st.success(f"Saved '{title.strip()}'.")


def manage_view(user):
    st.subheader("Manage my papers")
    mine = papers_for_user(user["id"], only_mine=True)
    if not mine:
        st.info("You haven't added any papers yet.")
        return
    labels = {f"[{p['id']}] {p['title']}": p["id"] for p in mine}
    pick = st.selectbox("Paper", list(labels))
    p = get_paper(labels[pick])

    st.markdown("#### Submission details")
    with st.form("edit"):
        title = st.text_input("Title", p["title"])
        journal = st.text_input("Journal name", p["journal_name"] or "")
        mno = st.text_input("Manuscript number", p["manuscript_number"] or "")
        date_sub = st.text_input("Initial date submitted (YYYY-MM-DD)", p["date_submitted"] or "")
        mode = st.radio("Status source", ["manual", "auto"], horizontal=True,
                        index=0 if p["status_mode"] == "manual" else 1)
        status = st.selectbox("Current status", STATUSES, index=STATUSES.index(p["status"]))
        link = st.text_input("Journal tracking link", p["tracking_link"] or "")
        notes = st.text_area("Notes", p["notes"] or "")
        save = st.form_submit_button("Update", type="primary")

    if save:
        c = conn()
        c.execute(
            """UPDATE papers SET title=?, journal_name=?, manuscript_number=?, date_submitted=?,
               status_mode=?, tracking_link=?, notes=?, updated_at=? WHERE id=?""",
            (title.strip(), journal.strip(), mno.strip(), date_sub.strip() or None,
             mode, link.strip() or None, notes.strip(), now(), p["id"]),
        )
        c.commit()
        c.close()
        set_status(p["id"], status, "manual")
        st.success("Updated.")
        st.rerun()

    if p["status_mode"] == "auto":
        if st.button("Check status now from tracking link"):
            run_auto_check(p)
            st.rerun()

    st.markdown("#### Danger zone")
    with st.expander("Delete this paper"):
        st.warning(f"This permanently removes '{p['title']}' along with its status "
                   "history and previous-submission records. It cannot be undone.")
        sure = st.checkbox("Yes, I want to delete this paper", key=f"delchk{p['id']}")
        if st.button("Delete permanently", type="primary", disabled=not sure,
                     key=f"delbtn{p['id']}"):
            delete_paper(p["id"])
            st.success("Paper deleted.")
            st.rerun()

    st.markdown("#### Co-authors")
    current = {a["id"] for a in authors_of(p["id"])}
    others = [u for u in all_users() if u["id"] != user["id"]]
    picked = st.multiselect("Co-authors on this paper",
                            [u["email"] for u in others],
                            default=[u["email"] for u in others if u["id"] in current])
    if st.button("Save co-authors"):
        c = conn()
        c.execute("DELETE FROM paper_authors WHERE paper_id = ? AND user_id != ?", (p["id"], user["id"]))
        for i, em in enumerate(picked, 1):
            u = get_user_by_email(em)
            if u:
                c.execute("INSERT OR IGNORE INTO paper_authors (paper_id,user_id,author_order) VALUES (?,?,?)",
                          (p["id"], u["id"], i))
        c.commit()
        c.close()
        st.success("Co-authors saved.")

    st.markdown("#### Previous journal submissions")
    for s in prior_subs(p["id"]):
        cols = st.columns([5, 1])
        with cols[0]:
            st.markdown(f"**{s['journal_name']}** — {s['outcome'] or '—'} "
                        f"({s['date_submitted'] or '?'} → {s['date_decided'] or '?'})")
            for i, cm in enumerate(json.loads(s["comments_json"]), 1):
                st.markdown(f"{i}. {cm}")
        with cols[1]:
            if st.button("Delete", key=f"delps{s['id']}"):
                c = conn()
                c.execute("DELETE FROM prior_submissions WHERE id = ?", (s["id"],))
                c.commit()
                c.close()
                st.rerun()

    with st.form("addprior", clear_on_submit=True):
        st.caption("Add a journal this paper was previously submitted to.")
        pj = st.text_input("Journal name *")
        d1 = st.text_input("Date submitted (YYYY-MM-DD)")
        d2 = st.text_input("Date of decision (YYYY-MM-DD)")
        out = st.selectbox("Outcome", ["Rejected", "Withdrawn", "Desk rejected", "Revision requested", "Other"])
        cmts = st.text_area("Reviewer comments — one per line")
        addp = st.form_submit_button("Add previous submission")
    if addp:
        if not pj.strip():
            st.error("Journal name is required.")
        else:
            lst = [l.strip() for l in cmts.splitlines() if l.strip()]
            c = conn()
            c.execute(
                """INSERT INTO prior_submissions (paper_id, journal_name, date_submitted,
                   date_decided, outcome, comments_json) VALUES (?,?,?,?,?,?)""",
                (p["id"], pj.strip(), d1.strip() or None, d2.strip() or None, out, json.dumps(lst)),
            )
            c.commit()
            c.close()
            st.success("Added.")
            st.rerun()


def main():
    if "user" not in st.session_state:
        login_view()
        return
    user = st.session_state.user

    with st.sidebar:
        st.markdown(f"### {user['name']}")
        corr, co = role_summary(user["id"])
        bits = []
        if corr:
            bits.append(f"corresponding on {corr}")
        if co:
            bits.append(f"co-author on {co}")
        st.caption(user["email"])
        if bits:
            st.caption(" · ".join(bits))
        if is_admin(user):
            st.caption("account administrator")
        if st.button("Sign out"):
            del st.session_state.user
            st.rerun()
        st.divider()
        pages = ["Dashboard", "Authors", "Add paper", "Manage my papers"]
        if is_admin(user):
            pages.append("Users")
        pages.append("My account")
        page = st.radio("Go to", pages)

    st.title("📄 PaperTrack")
    if page == "Dashboard":
        dashboard_view(user)
    elif page == "Authors":
        authors_view()
    elif page == "Add paper":
        new_paper_view(user)
    elif page == "Users":
        users_view(user)
    elif page == "My account":
        account_view(user)
    else:
        manage_view(user)


main()
