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
# Database
# --------------------------------------------------------------------------
def conn():
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def init_db():
    c = conn()
    c.executescript(
        """
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
            owner_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (owner_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS paper_authors (
            paper_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            author_order INTEGER DEFAULT 0,
            PRIMARY KEY (paper_id, user_id),
            FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS status_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            paper_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            source TEXT NOT NULL,
            changed_at TEXT NOT NULL,
            FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS prior_submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            paper_id INTEGER NOT NULL,
            journal_name TEXT NOT NULL,
            date_submitted TEXT,
            date_decided TEXT,
            outcome TEXT,
            comments_json TEXT NOT NULL DEFAULT '[]',
            FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE
        );
        """
    )
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
    (r"reviews?\s+completed", "Required Reviews Completed"),
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


def fetch_status_from_link(url):
    """Best effort. Returns (status_or_None, human_readable_note)."""
    if requests is None:
        return None, "The `requests` package is not installed."
    if not url or not url.startswith(("http://", "https://")):
        return None, "No valid tracking URL saved."
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0 PaperTrack/1.0"})
    except Exception as e:
        return None, f"Request failed: {e}"

    if r.status_code != 200:
        return None, f"Page returned HTTP {r.status_code}."

    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", r.text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).lower()

    if re.search(r"(sign in|log in|username|password)", text) and len(text) < 6000:
        return None, "The link appears to be a login page — journal portals need a session, so auto-check can't read it."

    for pattern, status in AUTO_PATTERNS:
        if re.search(pattern, text):
            return status, f"Matched '{status}' on the page."
    return None, "Page fetched, but no recognisable status text found."


def run_auto_check(paper_row, silent=False):
    status, note = fetch_status_from_link(paper_row["tracking_link"])
    c = conn()
    c.execute(
        "UPDATE papers SET last_auto_check = ?, last_auto_result = ? WHERE id = ?",
        (now(), note, paper_row["id"]),
    )
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
        role = st.selectbox("Role", ["corresponding", "author"], key="rg_role",
                            help="Corresponding authors can create and edit papers. Authors have read access.")
        if st.button("Create account"):
            if not (n and e and p):
                st.error("Fill in every field.")
            elif get_user_by_email(e):
                st.error("That email is already registered.")
            else:
                create_user(n, e, p, role)
                st.success("Account created — sign in from the other tab.")


def paper_card(p, expanded=False):
    authors = authors_of(p["id"])
    names = ", ".join(a["name"] for a in authors) or "—"
    with st.expander(f"**{p['title']}** · {p['journal_name'] or 'no journal'} · `{p['status']}`", expanded=expanded):
        c1, c2 = st.columns([2, 1])
        with c1:
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
        with c2:
            if p["file_path"] and os.path.exists(p["file_path"]):
                with open(p["file_path"], "rb") as f:
                    st.download_button("Download manuscript", f.read(),
                                       file_name=os.path.basename(p["file_path"]),
                                       key=f"dl{p['id']}")

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
    papers = papers_for_user(user["id"])

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
    tabs = st.tabs([u["name"] for u in users])
    for tab, u in zip(tabs, users):
        with tab:
            st.markdown(f"**{u['name']}** · {u['email']} · role: `{u['role']}`")
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


def new_paper_view(user):
    st.subheader("Add a paper")
    users = all_users()
    with st.form("newpaper", clear_on_submit=True):
        title = st.text_input("Title of the paper *")
        journal = st.text_input("Journal name")
        mno = st.text_input("Manuscript number")
        date_sub = st.date_input("Initial date submitted", value=dt.date.today())
        no_date = st.checkbox("Not submitted yet (ignore the date above)")
        up = st.file_uploader("Submitted file", type=["pdf", "docx", "doc", "tex", "zip"])
        coauthors = st.multiselect(
            "Co-authors", [u["email"] for u in users if u["id"] != user["id"]]
        )
        mode = st.radio("Status source", ["manual", "auto"], horizontal=True,
                        help="Use manual before the paper goes under review; switch to auto once the journal gives you a tracking link.")
        status = st.selectbox("Current status", STATUSES, index=1)
        link = st.text_input("Journal tracking link (for auto mode)")
        notes = st.text_area("Notes")
        ok = st.form_submit_button("Save paper", type="primary")

    if ok:
        if not title.strip():
            st.error("A title is required.")
            return
        path = None
        if up is not None:
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", up.name)
            path = os.path.join(UPLOAD_DIR, f"{int(dt.datetime.now().timestamp())}_{safe}")
            with open(path, "wb") as f:
                f.write(up.getbuffer())
        c = conn()
        cur = c.execute(
            """INSERT INTO papers (title, journal_name, manuscript_number, date_submitted,
               file_path, status, status_mode, tracking_link, notes, owner_id, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (title.strip(), journal.strip(), mno.strip(),
             None if no_date else str(date_sub), path, status, mode,
             link.strip() or None, notes.strip(), user["id"], now(), now()),
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
                  (pid, status, "manual", now()))
        c.commit()
        c.close()
        st.success("Paper saved.")


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
        newfile = st.file_uploader("Replace submitted file", type=["pdf", "docx", "doc", "tex", "zip"])
        save = st.form_submit_button("Update", type="primary")

    if save:
        path = p["file_path"]
        if newfile is not None:
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", newfile.name)
            path = os.path.join(UPLOAD_DIR, f"{int(dt.datetime.now().timestamp())}_{safe}")
            with open(path, "wb") as f:
                f.write(newfile.getbuffer())
        c = conn()
        c.execute(
            """UPDATE papers SET title=?, journal_name=?, manuscript_number=?, date_submitted=?,
               status_mode=?, tracking_link=?, notes=?, file_path=?, updated_at=? WHERE id=?""",
            (title.strip(), journal.strip(), mno.strip(), date_sub.strip() or None,
             mode, link.strip() or None, notes.strip(), path, now(), p["id"]),
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
        st.caption(f"{user['email']} · {user['role']}")
        if st.button("Sign out"):
            del st.session_state.user
            st.rerun()
        st.divider()
        pages = ["Dashboard", "Authors"]
        if user["role"] == "corresponding":
            pages += ["Add paper", "Manage my papers"]
        page = st.radio("Go to", pages)

    st.title("📄 PaperTrack")
    if page == "Dashboard":
        dashboard_view(user)
    elif page == "Authors":
        authors_view()
    elif page == "Add paper":
        new_paper_view(user)
    else:
        manage_view(user)


main()
