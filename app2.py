import os
import json
import time
import threading
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime
import ssl

from flask import Flask, jsonify, request, abort
from werkzeug.exceptions import HTTPException
from google.oauth2 import service_account
from googleapiclient.discovery import build


# =====================================================
# PATHS + LOGGING
# =====================================================

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
SECRETS_PATH = os.path.join(APP_DIR, "secrets", "service_account.json")
LOG_DIR = os.path.join(APP_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logger = logging.getLogger("classassist")
logger.setLevel(logging.INFO)

handler = RotatingFileHandler(
    os.path.join(LOG_DIR, "app.log"),
    maxBytes=1_000_000,
    backupCount=5,
    encoding="utf-8",
)
handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(handler)


# =====================================================
# CONFIG
# =====================================================

def load_config():
    if not os.path.exists(CONFIG_PATH):
        raise FileNotFoundError("Missing config.json")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def build_sheets_service():
    if not os.path.exists(SECRETS_PATH):
        raise FileNotFoundError("Missing service_account.json")

    creds = service_account.Credentials.from_service_account_file(
        SECRETS_PATH,
        scopes=SCOPES
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


# =====================================================
# HELPERS
# =====================================================

def normalize(v):
    return str(v or "").strip()


def safe_parse_timestamp(value):
    if not value:
        return None

    s = str(value).strip()
    formats = [
        "%m/%d/%Y %I:%M:%S %p",
        "%m/%d/%Y %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
    ]

    for f in formats:
        try:
            return datetime.strptime(s, f)
        except Exception:
            pass

    return None


def col_to_letter(n):
    out = ""
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# =====================================================
# AUTH (basic)
# =====================================================

def get_expected_token(cfg):
    # supports either key name so config can evolve without breaking
    return normalize(cfg.get("teacher_passcode") or cfg.get("admin_token"))


def require_write_auth():
    expected = get_expected_token(CFG or {})
    # If you have not set a passcode yet, allow writes (dev mode).
    if not expected:
        return

    provided = normalize(
        request.headers.get("X-Auth-Token") or
        request.args.get("token") or
        (request.get_json(silent=True) or {}).get("token")
    )

    if provided != expected:
        abort(401, description="UNAUTHORIZED")


# =====================================================
# POLLER
# =====================================================

class SheetPoller:
    def __init__(self, cfg):
        self.sheet_id = cfg["sheet_id"]
        self.sheet_name = cfg.get("worksheet_name", "Form Responses 1")
        self.interval = int(cfg.get("poll_seconds", 30))
        self.teacher = cfg.get("teacher_name", "Teacher")

        # statuses to show in dashboard
        self.visible_statuses = {"OPEN", "IN_PROGRESS"}

        self._tickets = []
        self._meta = {"last_ok": None, "last_error": None}

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._service = None
        self._write_lock = threading.Lock()  # serialize sheet writes

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def get_state(self):
        with self._lock:
            return list(self._tickets), dict(self._meta)

    # -------------------------

    def _loop(self):
        while not self._stop.is_set():
            try:
                if not self._service:
                    self._service = build_sheets_service()

                tickets = self._read_sheet()

                with self._lock:
                    self._tickets = tickets
                    self._meta["last_ok"] = datetime.now().isoformat(timespec="seconds")
                    self._meta["last_error"] = None

            except Exception as e:
                logger.exception("Poll error")
                with self._lock:
                    self._meta["last_error"] = str(e)

            time.sleep(self.interval)

    # -------------------------

    def _safe_execute(self, request_obj, retries=2):
        """Execute a Google API request with a small retry on SSL handshake failures.

        This protects the app from occasional TLS/proxy hiccups that would otherwise crash an action.
        """
        last = None
        for attempt in range(int(retries)):
            try:
                return request_obj.execute()
            except ssl.SSLError as e:
                last = e
                logger.exception("SSL error during Google Sheets call (attempt %s/%s)", attempt + 1, retries)
                try:
                    # Rebuild the service (fresh connection) and retry.
                    self._service = build_sheets_service()
                except Exception:
                    pass
                time.sleep(0.4 * (attempt + 1))
        # If we still fail, bubble up so caller can report NOT_CONFIRMED / meta error.
        raise last

    def _read_sheet(self):
        result = self._safe_execute(self._service.spreadsheets().values().get(
            spreadsheetId=self.sheet_id,
            range=f"{self.sheet_name}!A1:ZZ"
        ))

        values = result.get("values", [])
        if len(values) < 2:
            return []

        headers = values[0]
        idx = {normalize(h): i for i, h in enumerate(headers)}

        def get(row, name):
            i = idx.get(name)
            return "" if i is None or i >= len(row) else row[i]

        tickets = []
        seen = set()

        for r in range(1, len(values)):
            row = values[r]
            row_num = r + 1

            student = normalize(get(row, "Student"))
            period = normalize(get(row, "Period"))
            status = normalize(get(row, "Status")).upper()
            ticket_id = normalize(get(row, "TicketId"))
            ts = safe_parse_timestamp(get(row, "Timestamp"))

            if not status:
                status = "OPEN"

            if status not in self.visible_statuses:
                continue

            if not student or not period:
                continue

            if not ticket_id:
                ticket_id = f"ROW_{row_num}"

            key = (student.upper(),)
            if key in seen:
                continue
            seen.add(key)

            wait = None
            if ts:
                wait = int((datetime.now() - ts).total_seconds())

            tickets.append({
                "ticket_id": ticket_id,
                "student": student,
                "period": period,
"help_type": normalize(get(row, "Help Type")),
                "timestamp": normalize(get(row, "Timestamp")),
                "wait_seconds": wait,
                "row": row_num,
                "status": status,
                "claimed_by": normalize(get(row, "ClaimedBy")),
                "claimed_at": normalize(get(row, "ClaimedAt")),
            })

        # OPEN first, then IN_PROGRESS, then by longest wait
        def sort_key(t):
            status_rank = 0 if t.get("status") == "OPEN" else 1
            ws = t.get("wait_seconds")
            ws_rank = -(ws if isinstance(ws, int) else -1)
            # if ws is None, treat as very small priority
            if ws is None:
                ws_rank = 10**12
            return (status_rank, ws_rank)

        tickets.sort(key=sort_key)
        return tickets

    # -------------------------

    def _get_sheet_data_and_cols(self):
        data = self._safe_execute(self._service.spreadsheets().values().get(
            spreadsheetId=self.sheet_id,
            range=f"{self.sheet_name}!A1:ZZ"
        )).get("values", [])

        if len(data) < 2:
            return None, None

        headers = data[0]
        col = {normalize(h): i + 1 for i, h in enumerate(headers)}  # 1-based index
        return data, col
    def _batch_update_cells(self, row_index_1based, col_map, updates_dict):
        """Write a batch of cell updates AND verify the write.

        Returns (ok, msg). msg can be:
          - OK
          - NO_VALID_COLUMNS (nothing to write because columns missing)
          - NOT_CONFIRMED (write may have happened, but we could not verify)
        """
        body = {"valueInputOption": "RAW", "data": []}

        # Build batch update ranges (skip optional columns that don't exist)
        updates = {k: v for k, v in (updates_dict or {}).items() if k in col_map}

        for col_name, val in updates.items():
            cell = f"{col_to_letter(col_map[col_name])}{row_index_1based}"
            body["data"].append({
                "range": f"{self.sheet_name}!{cell}",
                "values": [[val]]
            })

        if not body["data"]:
            return False, "NO_VALID_COLUMNS"

        # Serialize writes (protects against rapid repeat actions)
        with self._write_lock:
            # 1) Write
            self._safe_execute(self._service.spreadsheets().values().batchUpdate(
                spreadsheetId=self.sheet_id,
                body=body
            ))

            # 2) Verify
            try:
                row_vals = self._safe_execute(self._service.spreadsheets().values().get(
                    spreadsheetId=self.sheet_id,
                    range=f"{self.sheet_name}!A{row_index_1based}:ZZ{row_index_1based}"
                )).get("values", [])

                if not row_vals:
                    return False, "NOT_CONFIRMED"

                row = row_vals[0]

                def read_cell(col_name):
                    idx_1 = col_map.get(col_name)
                    if not idx_1:
                        return ""
                    i0 = idx_1 - 1
                    return normalize(row[i0]) if i0 < len(row) else ""

                for k, v in updates.items():
                    if read_cell(k) != normalize(v):
                        return False, "NOT_CONFIRMED"

            except Exception:
                logger.exception("Verify error")
                return False, "NOT_CONFIRMED"

        return True, "OK"

    def _update_cache_ticket(self, ticket_id, patch):
        with self._lock:
            for t in self._tickets:
                if normalize(t.get("ticket_id")) == normalize(ticket_id):
                    t.update(patch)
                    break

            # If the ticket is no longer visible (resolved/no_show), remove it.
            status = normalize(patch.get("status")).upper() if patch.get("status") else ""
            if status and status not in self.visible_statuses:
                self._tickets = [
                    t for t in self._tickets
                    if normalize(t.get("ticket_id")) != normalize(ticket_id)
                ]

            self._meta["last_ok"] = datetime.now().isoformat(timespec="seconds")
            self._meta["last_error"] = None

    # -------------------------

    def apply_action(self, ticket_id, action, tags=""):
        action = normalize(action).lower()
        ticket_id = normalize(ticket_id)

        if not ticket_id:
            return False, "MISSING_TICKET_ID"

        data, col = self._get_sheet_data_and_cols()
        if data is None:
            return False, "NO_DATA"

        for needed in ["TicketId", "Status", "LastUpdated"]:
            if needed not in col:
                return False, f"Missing column {needed}"

        target_row_1based = None
        for i in range(1, len(data)):
            row = data[i]
            cell_val = ""
            try:
                cell_val = row[col["TicketId"] - 1]
            except Exception:
                cell_val = ""
            if normalize(cell_val) == ticket_id:
                target_row_1based = i + 1
                break

        if not target_row_1based:
            return False, "NOT_FOUND"

        now = now_str()

        if action == "claim":
            updates = {
                "Status": "IN_PROGRESS",
                "ClaimedAt": now,
                "ClaimedBy": self.teacher,
                "LastUpdated": now,
            }
            ok, msg = self._batch_update_cells(target_row_1based, col, updates)
            if ok:
                self._update_cache_ticket(ticket_id, {
                    "status": "IN_PROGRESS",
                    "claimed_at": now,
                    "claimed_by": self.teacher,
                })
            return ok, msg

        if action == "reopen":
            updates = {
                "Status": "OPEN",
                "ClaimedAt": "",
                "ClaimedBy": "",
                "LastUpdated": now,
            }
            ok, msg = self._batch_update_cells(target_row_1based, col, updates)
            if ok:
                self._update_cache_ticket(ticket_id, {
                    "status": "OPEN",
                    "claimed_at": "",
                    "claimed_by": "",
                })
            return ok, msg

        if action == "no_show":
            updates = {
                "Status": "NO_SHOW",
                "NoShowAt": now,
                "NoShowBy": self.teacher,
                "LastUpdated": now,
            }
            ok, msg = self._batch_update_cells(target_row_1based, col, updates)
            if ok:
                self._update_cache_ticket(ticket_id, {"status": "NO_SHOW"})
            return ok, msg

        if action == "resolve":
            updates = {
                "Status": "RESOLVED",
                "ResolvedAt": now,
                "ResolvedBy": self.teacher,
                "LastUpdated": now,
            }
            t = normalize(tags)
            if t:
                updates["TeacherTags"] = t
                updates["TagsAt"] = now
                if "follow up" in t.lower():
                    updates["FollowUp"] = "YES"
                    updates["FollowUpAt"] = now
            ok, msg = self._batch_update_cells(target_row_1based, col, updates)
            if ok:
                self._update_cache_ticket(ticket_id, {"status": "RESOLVED"})
            return ok, msg

        return False, "BAD_ACTION"



    def suggest_next(self, period_filter="", help_type_filter="", status_filter="OPEN"):
        """
        Suggest the next ticket to help, without needing seat data.
        Rules (Smart Queue):
          1) Prefer OPEN tickets (default)
          2) Longest wait first
          3) Period ascending (numeric when possible)
          4) Student name ascending
        Optional filters:
          - period_filter: exact match
          - help_type_filter: exact match
          - status_filter: OPEN or IN_PROGRESS (default OPEN)
        """
        period_filter = normalize(period_filter)
        help_type_filter = normalize(help_type_filter)
        status_filter = normalize(status_filter).upper() or "OPEN"

        with self._lock:
            tickets = list(self._tickets)

        # basic filtering
        if status_filter:
            tickets = [t for t in tickets if normalize(t.get("status")).upper() == status_filter]

        if period_filter:
            tickets = [t for t in tickets if normalize(t.get("period")) == period_filter]

        if help_type_filter:
            tickets = [t for t in tickets if normalize(t.get("help_type")) == help_type_filter]

        if not tickets:
            return None

        def period_num(p):
            p = normalize(p)
            try:
                return int(p)
            except Exception:
                return 10**9  # unknown/blank goes last

        def wait_val(w):
            return w if isinstance(w, int) else -1

        # Longest wait first, then period asc, then student name
        tickets.sort(
            key=lambda t: (
                -wait_val(t.get("wait_seconds")),
                period_num(t.get("period")),
                normalize(t.get("student")).lower(),
            )
        )
        return tickets[0]


# =====================================================
# FLASK
# =====================================================

app = Flask(__name__)
app.config["JSONIFY_PRETTYPRINT_REGULAR"] = False

CFG = None
POLLER = None


@app.after_request
def no_cache(resp):
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp



@app.errorhandler(401)
def err_401(e):
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "message": "UNAUTHORIZED"}), 401
    return "UNAUTHORIZED", 401


@app.errorhandler(404)
def err_404(e):
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "message": "NOT_FOUND"}), 404
    return "NOT_FOUND", 404


@app.errorhandler(500)
def err_500(e):
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "message": "SERVER_ERROR"}), 500
    return "SERVER_ERROR", 500


@app.route("/")
def dashboard():
    return DASHBOARD_HTML


@app.route("/api/tickets")
def api_tickets():
    t, m = POLLER.get_state()
    return jsonify({
        "tickets": t,
        "meta": m,
        "poll_seconds": int(CFG.get("poll_seconds", 30)),
        "teacher_name": normalize(CFG.get("teacher_name", "Teacher")),
        "auth_enabled": bool(get_expected_token(CFG)),
    })


@app.route("/api/action", methods=["POST"])
def api_action():
    try:
        require_write_auth()
        data = request.get_json(force=True, silent=True) or {}
        ticket_id = data.get("ticket_id", "")
        action = data.get("action", "")
        tags = data.get("tags", "")
        ok, msg = POLLER.apply_action(ticket_id, action, tags=tags)
        return jsonify({"ok": ok, "message": msg})
    except HTTPException:
        raise
    except Exception:
        logger.exception("api_action error")
        return jsonify({"ok": False, "message": "SERVER_ERROR"}), 500



@app.route("/api/suggest")
def api_suggest():
    """
    Returns the next suggested ticket (or null) based on Smart Queue rules.
    Optional query params:
      - period
      - help_type
      - status (OPEN or IN_PROGRESS; default OPEN)
    """
    period = request.args.get("period", "")
    help_type = request.args.get("help_type", "")
    status = request.args.get("status", "OPEN")
    t = POLLER.suggest_next(period_filter=period, help_type_filter=help_type, status_filter=status)
    return jsonify({"ticket": t})


# =====================================================
# DASHBOARD HTML
# =====================================================

DASHBOARD_HTML = r"""
<!doctype html>
<html>
<head>
<title>ClassAssist</title>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<style>
    body{font-family:Arial;margin:20px;max-width:1100px}
    .topbar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:12px}
    .pill{display:inline-block;padding:6px 10px;border:1px solid #ddd;border-radius:999px;font-size:12px;background:#fafafa}
    .controls{display:flex;flex-wrap:wrap;gap:10px;align-items:end;margin:10px 0 14px}
    .controls label{font-size:12px;color:#444;display:block;margin-bottom:4px}
    .controls input,.controls select{padding:8px;border:1px solid #ccc;border-radius:8px}
    .controls .group{display:flex;flex-direction:column}
    .card{border:1px solid #ccc;padding:12px;margin:8px 0;border-radius:10px}
    .green{border-left:10px solid green}
    .yellow{border-left:10px solid gold}
    .red{border-left:10px solid red}
    .blue{border-left:10px solid dodgerblue}
    .muted{color:#666;font-size:12px}
    .row{display:flex;flex-wrap:wrap;gap:10px;align-items:center;justify-content:space-between}
    .btns{display:flex;gap:8px;flex-wrap:wrap}
    button{padding:8px 10px;border:1px solid #bbb;border-radius:8px;background:#fff;cursor:pointer}
    button:hover{background:#f6f6f6}
    button.primary{border-color:#111}
    button.danger{border-color:#b00020}
    .details{margin-top:10px;padding:10px;background:#fafafa;border:1px solid #eee;border-radius:10px;display:none}
    .flash{animation:flash 1.2s ease-in-out}
    @keyframes flash{0%{transform:scale(1);box-shadow:0 0 0 rgba(0,0,0,0)}50%{transform:scale(1.01);box-shadow:0 10px 25px rgba(0,0,0,0.12)}100%{transform:scale(1);box-shadow:0 0 0 rgba(0,0,0,0)}}
    .small{font-size:12px}
    .right{margin-left:auto}

    .sticky{position:sticky;top:0;z-index:1000;background:#fff;padding-top:4px}
    .sticky:after{content:'';display:block;height:10px}

    #board{display:flex;gap:14px;align-items:flex-start;flex-wrap:wrap}
    .col{flex:1;min-width:320px}
    .colTitle{margin:6px 0 8px}
    .toast{position:fixed;right:18px;bottom:18px;z-index:9999;
           background:#111;color:#fff;padding:10px 12px;border-radius:10px;
           box-shadow:0 6px 24px rgba(0,0,0,.25);display:none;font-size:13px}
    .toast.ok{background:#0b7a2a}
    .toast.err{background:#a60000}
    #overlay{position:fixed;inset:0;background:rgba(0,0,0,.25);z-index:9998;
             display:none;align-items:center;justify-content:center}
    #overlay .box{background:#fff;border-radius:14px;padding:16px 18px;min-width:280px;
                  box-shadow:0 10px 40px rgba(0,0,0,.25)}
    .spinner{width:22px;height:22px;border-radius:50%;
             border:3px solid #ddd;border-top-color:#111;animation:spin .9s linear infinite}
    @keyframes spin{to{transform:rotate(360deg)}}

</style>
</head>
<body>

<h2>ClassAssist Dashboard</h2>

<div class="sticky">
<div class="topbar">
    <span class="pill" id="metaPill">Loading…</span>
    <span class="pill" id="refreshPill">Next refresh: --</span>
    <span class="pill" id="statsPill">Stats: --</span>
    <span class="pill" id="authPill" style="display:none"></span>
</div>

<div class="controls">
    <div class="group">
        <label>Search student</label>
        <input id="q" placeholder="Type a name…" />
    </div>

    <div class="group">
        <label>Period</label>
        <select id="period">
            <option value="">All</option>
        </select>
    </div>

    <div class="group">
        <label>Status</label>
        <select id="status">
            <option value="">All</option>
            <option value="OPEN">OPEN</option>
            <option value="IN_PROGRESS">IN_PROGRESS</option>
        </select>
    </div>

    <div class="group">
        <label>Help type</label>
        <select id="helpType">
            <option value="">All</option>
        </select>
    </div>

    <div class="group">
        <label>Sort</label>
        <select id="sortMode">
            <option value="queue">Smart queue</option>
            <option value="wait">Longest wait</option>
            <option value="newest">Newest</option>
            <option value="period">Period</option>
            <option value="status">Status</option>
        </select>
    </div>

    <div class="group">
        <label>Filters</label>
        <button onclick="clearFilters()">Clear</button>
        <div class="muted small">Reset search and dropdowns</div>
    </div>

    <div class="group">
        <label>Refresh</label>
        <div style="display:flex;gap:8px;align-items:center">
            <button onclick="load()">Refresh now</button>
            <label class="small"><input type="checkbox" id="pauseRefresh" /> Pause</label>
        </div>
        <div class="muted small">Pause stops auto refresh</div>
    </div>

    <div class="group">
        <label>Next</label>
        <button class="primary" onclick="suggestNext()">Suggest next</button>
        <div class="muted small">Uses Smart queue</div>
    </div>

    <div class="group">
        <label class="small">Alerts</label>
        <div style="display:flex;gap:10px;align-items:center">
            <label class="small"><input type="checkbox" id="sound" /> Sound</label>
            <label class="small"><input type="checkbox" id="desktop" /> Desktop</label>
            <label class="small"><input type="checkbox" id="highlight" checked /> Highlight</label>
        </div>
    </div>

    <div class="group right">
        <label>Teacher passcode</label>
        <div style="display:flex;gap:8px;align-items:center">
            <input id="token" placeholder="(only needed if enabled)" />
            <button onclick="saveToken()">Save</button>
        </div>
        <div class="muted small" id="tokenHint"></div>
    </div>
</div>
</div>


<div id="board">
    <div class="col">
        <div class="muted small colTitle"><b>OPEN</b></div>
        <div id="listOpen"></div>
    </div>
    <div class="col">
        <div class="muted small colTitle"><b>IN PROGRESS</b></div>
        <div id="listProgress"></div>
    </div>
</div>

<div id="toast" class="toast"></div>

<div id="overlay">
    <div class="box">
        <div style="display:flex;gap:10px;align-items:center">
            <div class="spinner"></div>
            <div>
                <div id="overlayText" style="font-weight:700">Saving…</div>
                <div id="overlaySub" class="muted" style="margin-top:2px">Please wait. Do not click again.</div>
            </div>
        </div>
    </div>
</div>


<script>
let POLL_MS = 30000;
let nextLoadAt = 0;
let countdownTimer = null;
let pollTimer = null;

let lastTicketIds = new Set();     // from previous refresh
let lastRedIds = new Set();        // tickets that were red on previous refresh
let newlyArrivedIds = new Set();
let actionBusy = false;

// Quick-tag notes (zero typing)
const TAGS_PRIMARY = ["Small group","1 on 1","Reteach","Practice","Missing work","Follow up"]; 
const TAGS_EXTRA = ["Vocab","Check understanding"]; 
const selectedTagsByTicket = {}; // {ticketId: Set([...])}
const customTagsByTicket = {};   // {ticketId: Set([...])}
const otherEnabledByTicket = {}; // {ticketId: boolean}
let lastSuggestedId = "";

function showOverlay(text, sub){
    const ov = document.getElementById("overlay");
    const t = document.getElementById("overlayText");
    const s = document.getElementById("overlaySub");
    if(t) t.textContent = text || "Saving…";
    if(s) s.textContent = sub || "Please wait. Do not click again.";
    if(ov) ov.style.display = "flex";
}
function hideOverlay(){
    const ov = document.getElementById("overlay");
    if(ov) ov.style.display = "none";
}
function showToast(text, kind){
    const el = document.getElementById("toast");
    if(!el) return;
    el.className = "toast" + (kind ? (" " + kind) : "");
    el.textContent = text || "";
    el.style.display = "block";
    clearTimeout(el._t);
    el._t = setTimeout(()=>{ el.style.display = "none"; }, 1600);
}

function lockCardButtons(btn){
    const card = btn.closest(".card");
    if(!card) return [];
    const buttons = Array.from(card.querySelectorAll("button"));
    for(const b of buttons){
        b.disabled = true;
        b.style.opacity = 0.6;
        b.style.cursor = "not-allowed";
    }
    return buttons;
}
function unlockButtons(buttons){
    for(const b of buttons){
        b.disabled = false;
        b.style.opacity = "";
        b.style.cursor = "";
    }
}

async function handleAction(btn, ticketId, action){
    // Hard throttle: one action at a time, plus per-card lock.
    if(actionBusy || btn.disabled) return;
    actionBusy = true;

    const originalText = btn.textContent;
    btn.textContent = "Working…";
    const locked = lockCardButtons(btn);

    // Minimum overlay time so the user naturally slows down.
    const minMs = 900;
    const start = Date.now();
    showOverlay("Saving…", "Please wait. Do not click again.");

    try{
        await doAction(ticketId, action);
        showToast("Completed", "ok");
    }catch(e){
        // doAction already showed an error toast; keep message honest
        if(String(e || "").includes("NOT_CONFIRMED")){
            showToast("Not confirmed. Refresh and try again.", "err");
        }
    }finally{
        const elapsed = Date.now() - start;
        const wait = Math.max(0, minMs - elapsed);
        setTimeout(()=>{
            hideOverlay();
            btn.textContent = originalText;
            unlockButtons(locked);
            actionBusy = false;
        }, wait);
    }
}
   // just for flashing on this render

function buildTagHtml(ticketId){
    const sel = selectedTagsByTicket[ticketId] || new Set();
    const all = TAGS_PRIMARY.concat(TAGS_EXTRA);
    return all.map(tag => {
        const id = `tag_${ticketId.replace(/[^a-zA-Z0-9_]/g, "_")}_${tag.replace(/[^a-zA-Z0-9_]/g, "_")}`;
        const checked = sel.has(tag) ? "checked" : "";
        const cls = TAGS_PRIMARY.includes(tag) ? "" : "muted";
        return `<label class="small ${cls}" style="margin-right:10px;white-space:nowrap">`
             + `<input type="checkbox" id="${id}" ${checked} onchange="toggleTag('${ticketId}','${tag}',this.checked)"/> ${escapeHtml(tag)}`
             + `</label>`;
    }).join("");
}

function toggleTag(ticketId, tag, isOn){
    if(!selectedTagsByTicket[ticketId]) selectedTagsByTicket[ticketId] = new Set();
    if(isOn) selectedTagsByTicket[ticketId].add(tag);
    else selectedTagsByTicket[ticketId].delete(tag);
}

function toggleOther(ticketId, isOn){
    otherEnabledByTicket[ticketId] = !!isOn;
    const wrap = document.getElementById("otherWrap_" + safeId(ticketId));
    if(wrap) wrap.style.display = isOn ? "inline-flex" : "none";
}

function addCustomTag(ticketId){
    const sid = safeId(ticketId);
    const input = document.getElementById("otherInput_" + sid);
    if(!input) return;
    const raw = (input.value || "").trim();
    if(!raw) return;

    if(!customTagsByTicket[ticketId]) customTagsByTicket[ticketId] = new Set();
    customTagsByTicket[ticketId].add(raw);
    input.value = "";

    requestLoad();
}

function removeCustomTag(ticketId, tag){
    const set = customTagsByTicket[ticketId];
    if(!set) return;
    set.delete(tag);
    requestLoad();
}

function buildCustomTagHtml(ticketId){
    const sid = safeId(ticketId);
    const enabled = !!otherEnabledByTicket[ticketId];
    const checked = enabled ? "checked" : "";
    const set = customTagsByTicket[ticketId] || new Set();
    const tags = Array.from(set);

    let chips = "";
    if(tags.length){
        chips = `<div style="margin-top:6px;display:flex;gap:6px;flex-wrap:wrap">` +
            tags.map(t => {
                const et = escapeHtml(t);
                return `<span style="border:1px solid #ddd;border-radius:999px;padding:4px 8px;background:#fafafa;cursor:pointer"
                    title="Click to remove"
                    onclick="removeCustomTag('${jsQuote(ticketId)}','${jsQuote(t)}')">${et} ✕</span>`;
            }).join("") +
        `</div>`;
    }

    const wrapStyle = enabled ? "display:inline-flex;gap:8px;align-items:center" : "display:none";
    return `
    <div class="small" style="margin-top:8px">
        <label class="small" style="margin-right:10px;white-space:nowrap">
            <input type="checkbox" ${checked} onchange="toggleOther('${jsQuote(ticketId)}',this.checked)"/> Other
        </label>
        <span id="otherWrap_${sid}" style="${wrapStyle}">
            <input id="otherInput_${sid}" placeholder="Quick note/tag…"
                style="padding:6px 8px;border:1px solid #ccc;border-radius:8px;width:220px"
                onkeydown="if(event.key==='Enter'){event.preventDefault();addCustomTag('${jsQuote(ticketId)}');}" />
            <button onclick="addCustomTag('${jsQuote(ticketId)}')">Add</button>
        </span>
        ${chips}
    </div>`;
}


function escapeHtml(s){
    return String(s ?? "").replace(/[&<>"']/g, m => ({
        "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"
    }[m]));
}

function jsQuote(s){
    return String(s ?? "").replace(/\\/g, "\\\\").replace(/'/g, "\\'");
}
function safeId(s){
    return String(s ?? "").replace(/[^a-zA-Z0-9_]/g, "_");
}

function minsLabel(waitSeconds){
    if(waitSeconds === null || waitSeconds === undefined) return "--";
    return String(Math.floor(waitSeconds / 60));
}

function getToken(){
    return localStorage.getItem("classassist_token") || "";
}

function saveToken(){
    const v = document.getElementById("token").value.trim();
    localStorage.setItem("classassist_token", v);
    load();
    return true;
}

function clearFilters(){
    document.getElementById("q").value = "";
    document.getElementById("period").value = "";
    document.getElementById("status").value = "";
    document.getElementById("helpType").value = "";
    document.getElementById("sortMode").value = "queue";
    requestLoad();
}


function pingBeep(){
    try{
        const ctx = new (window.AudioContext || window.webkitAudioContext)();
        const o = ctx.createOscillator();
        const g = ctx.createGain();
        o.type = "sine";
        o.frequency.value = 880;
        g.gain.value = 0.03;
        o.connect(g);
        g.connect(ctx.destination);
        o.start();
        setTimeout(()=>{ o.stop(); ctx.close(); }, 120);
    }catch(e){}
}

async function maybeDesktopNotify(title, body){
    if(!document.getElementById("desktop").checked) return;
    if(!("Notification" in window)) return;

    if(Notification.permission === "default"){
        try{ await Notification.requestPermission(); }catch(e){}
    }
    if(Notification.permission !== "granted") return;

    try{
        new Notification(title, { body });
    }catch(e){}
}

function buildOptions(selectEl, values){
    const current = selectEl.value;
    const opts = ['<option value="">All</option>']
        .concat(values.filter(Boolean).sort().map(v => `<option value="${escapeHtml(v)}">${escapeHtml(v)}</option>`));
    selectEl.innerHTML = opts.join("");
    selectEl.value = current;
}

function applyFiltersAndSort(tickets){
    const q = document.getElementById("q").value.trim().toLowerCase();
    const p = document.getElementById("period").value.trim();
    const st = document.getElementById("status").value.trim();
    const ht = document.getElementById("helpType").value.trim();
    const sortMode = document.getElementById("sortMode").value;

    let out = tickets.slice();

    if(q){
        out = out.filter(t => String(t.student||"").toLowerCase().includes(q));
    }
    if(p){
        out = out.filter(t => String(t.period||"") === p);
    }
    if(st){
        out = out.filter(t => String(t.status||"") === st);
    }
    if(ht){
        out = out.filter(t => String(t.help_type||"") === ht);
    }

    if(sortMode === "queue"){
        const rank = (s)=> s==="OPEN" ? 0 : 1;
        const pnum = (p)=>{ const x = String(p||"").trim(); const n = parseInt(x,10); return Number.isFinite(n) ? n : 1e9; };
        const wv = (w)=> (w===null||w===undefined) ? -1 : Number(w);
        out.sort((a,b) => {
            const ra = rank(String(a.status||"").toUpperCase());
            const rb = rank(String(b.status||"").toUpperCase());
            if(ra !== rb) return ra - rb;
            const wa = wv(a.wait_seconds);
            const wb = wv(b.wait_seconds);
            if(wa !== wb) return wb - wa;
            const pa = pnum(a.period);
            const pb = pnum(b.period);
            if(pa !== pb) return pa - pb;
            return String(a.student||"").localeCompare(String(b.student||""));
        });
    }else if(sortMode === "wait"){
        out.sort((a,b) => (b.wait_seconds ?? -1) - (a.wait_seconds ?? -1));
    }else if(sortMode === "newest"){
        out.sort((a,b) => {
            const ta = Date.parse(a.timestamp || "") || 0;
            const tb = Date.parse(b.timestamp || "") || 0;
            return tb - ta;
        });
    }else if(sortMode === "period"){
        out.sort((a,b) => String(a.period||"").localeCompare(String(b.period||"")));
    }else if(sortMode === "status"){
        const rank = (s)=> s==="OPEN" ? 0 : 1;
        out.sort((a,b) => rank(a.status) - rank(b.status));
    }

    return out;
}

function scheduleTimers(){
    if(pollTimer) clearInterval(pollTimer);
    const pause = document.getElementById("pauseRefresh");
    if(pause && pause.checked) return;
    pollTimer = setInterval(load, POLL_MS);
}

function scheduleCountdown(){
    if(countdownTimer) clearInterval(countdownTimer);
    countdownTimer = setInterval(() => {
        const pill = document.getElementById("refreshPill");
        const pause = document.getElementById("pauseRefresh");
        if(pause && pause.checked){
            pill.textContent = "Next refresh: Paused";
            return;
        }
        const diff = Math.max(0, Math.floor((nextLoadAt - Date.now()) / 1000));
        pill.textContent = "Next refresh: " + diff + "s";
    }, 250);
}

let loadDebounce = null;
function requestLoad(){
    clearTimeout(loadDebounce);
    loadDebounce = setTimeout(()=>load(), 220);
}

async function load(){
    if(actionBusy) return;
    const pause = document.getElementById("pauseRefresh");
    if(pause && pause.checked) return;

    let r, j;
    try{
        r = await fetch("/api/tickets", { cache:"no-store" });
        j = await r.json();
    }catch(e){
        const pill = document.getElementById("metaPill");
        if(pill) pill.textContent = "Last OK: - | Error: " + String(e || "refresh_failed");
        showToast("Refresh failed", "err");
        return;
    }

    POLL_MS = (Number(j.poll_seconds) || 30) * 1000;
    nextLoadAt = Date.now() + POLL_MS;

    document.getElementById("token").value = getToken();

    document.getElementById("metaPill").textContent =
        "Last OK: " + (j.meta.last_ok || "-") + " | Error: " + (j.meta.last_error || "-");

    // quick stats
    const openCount = j.tickets.filter(t => t.status === "OPEN").length;
    const progCount = j.tickets.filter(t => t.status === "IN_PROGRESS").length;
    const waits = j.tickets.map(t => Number(t.wait_seconds)).filter(n => Number.isFinite(n) && n >= 0);
    const avgMin = waits.length ? Math.round((waits.reduce((a,b)=>a+b,0)/waits.length)/60) : 0;
    const maxMin = waits.length ? Math.floor((Math.max(...waits))/60) : 0;
    document.getElementById("statsPill").textContent =
        "Stats: OPEN " + openCount + " | IN PROGRESS " + progCount + " | Avg " + avgMin + "m | Max " + maxMin + "m";

    const authPill = document.getElementById("authPill");
    const tokenHint = document.getElementById("tokenHint");
    if(j.auth_enabled){
        authPill.style.display = "inline-block";
        authPill.textContent = "Passcode: ON";
        tokenHint.textContent = "Passcode required for Claim/Resolve/No-show/Reopen.";
    }else{
        authPill.style.display = "none";
        tokenHint.textContent = "Passcode is OFF. Add teacher_passcode in config.json to enable.";
    }

    // build filter options from live data
    const periods = Array.from(new Set(j.tickets.map(t => String(t.period||"").trim()).filter(Boolean)));
    const helpTypes = Array.from(new Set(j.tickets.map(t => String(t.help_type||"").trim()).filter(Boolean)));
    buildOptions(document.getElementById("period"), periods);
    buildOptions(document.getElementById("helpType"), helpTypes);

    // new ticket detection (compared to previous refresh)
    const currentIds = new Set(j.tickets.map(t => t.ticket_id));
    newlyArrivedIds = new Set();
    for(const id of currentIds){
        if(!lastTicketIds.has(id)) newlyArrivedIds.add(id);
    }

    // overdue detection (red OPEN tickets)
    const currentRed = new Set(
        j.tickets.filter(t => (t.wait_seconds ?? 0) >= 300 && t.status === "OPEN").map(t => t.ticket_id)
    );
    let newOverdueCount = 0;
    for(const id of currentRed){
        if(!lastRedIds.has(id)) newOverdueCount++;
    }

    const newCount = newlyArrivedIds.size;

    if(newCount > 0){
        if(document.getElementById("sound").checked) pingBeep();
        await maybeDesktopNotify("New help ticket(s)", newCount + " new ticket(s) added.");
    }
    if(newOverdueCount > 0){
        if(document.getElementById("sound").checked) pingBeep();
        await maybeDesktopNotify("Overdue ticket(s)", newOverdueCount + " ticket(s) hit red.");
    }

    // render
    render(applyFiltersAndSort(j.tickets));

    // update baselines for next refresh
    lastTicketIds = currentIds;
    lastRedIds = currentRed;

    scheduleTimers();
    scheduleCountdown();
}

function render(tickets){
    const listOpen = document.getElementById("listOpen");
    const listProgress = document.getElementById("listProgress");
    listOpen.innerHTML = "";
    listProgress.innerHTML = "";

    for(const t of tickets){
        let cls = "green";
        const ws = t.wait_seconds;

        if(t.status === "IN_PROGRESS"){
            cls = "blue";
        }else if(ws !== null && ws !== undefined){
            if(ws >= 300) cls = "red";
            else if(ws >= 180) cls = "yellow";
        }

        const detailsId = "d_" + String(t.ticket_id).replace(/[^a-zA-Z0-9_]/g, "_");
const helpType = escapeHtml(t.help_type || "--");
        const ts = escapeHtml(t.timestamp || "--");
        const claimedBy = escapeHtml(t.claimed_by || "--");
        const claimedAt = escapeHtml(t.claimed_at || "--");

        const waitMin = minsLabel(ws);
        const status = escapeHtml(t.status || "OPEN");

        const flash = (document.getElementById("highlight").checked && newlyArrivedIds.has(t.ticket_id))
            ? "flash"
            : "";

        let tagsHtml = "";
        if(t.status === "IN_PROGRESS"){
            tagsHtml = `<div class="small" style="margin-top:6px">${buildTagHtml(t.ticket_id)}</div>` + buildCustomTagHtml(t.ticket_id);
        }

        let btns = "";
        if(t.status === "OPEN"){
            btns += `<button class="primary" onclick="handleAction(this,'${escapeHtml(t.ticket_id)}','claim')">Claim</button>`;
            btns += `<button class="danger" onclick="handleAction(this,'${escapeHtml(t.ticket_id)}','no_show')">No-show</button>`;
            btns += `<button onclick="handleAction(this,'${escapeHtml(t.ticket_id)}','resolve')">Resolve</button>`;
        }else if(t.status === "IN_PROGRESS"){
            btns += `<button onclick="handleAction(this,'${escapeHtml(t.ticket_id)}','reopen')">Back to OPEN</button>`;
            btns += `<button class="danger" onclick="handleAction(this,'${escapeHtml(t.ticket_id)}','no_show')">No-show</button>`;
            btns += `<button onclick="handleAction(this,'${escapeHtml(t.ticket_id)}','resolve')">Resolve</button>`;
        }else{
            btns += `<button onclick="handleAction(this,'${escapeHtml(t.ticket_id)}','reopen')">Back to OPEN</button>`;
        }

        const target = (t.status === "IN_PROGRESS") ? listProgress : listOpen;
        target.innerHTML += `
        <div class="card ${cls} ${flash}" id="card_${String(t.ticket_id).replace(/[^a-zA-Z0-9_]/g, "_")}">
            <div class="row">
                <div>
                    <div><b>${escapeHtml(t.student)}</b> <span class="muted">(${status})</span></div>
                    <div class="muted">Wait: ${waitMin} min | Period: ${escapeHtml(t.period)}</div>
                    ${tagsHtml}
                </div>
                <div class="btns">
                    ${btns}
                    <button onclick="toggleDetails('${detailsId}')">Details</button>
                </div>
            </div>

            <div class="details" id="${detailsId}">
<div><b>Help Type:</b> ${helpType}</div>
                <div><b>Submitted:</b> ${ts}</div>
                <div><b>Claimed By:</b> ${claimedBy}</div>
                <div><b>Claimed At:</b> ${claimedAt}</div>
                <div class="muted small">Ticket ID: ${escapeHtml(t.ticket_id)}</div>
            </div>
        </div>`;
    }

    if(tickets.length === 0){
        listOpen.innerHTML = `<div class="muted">No tickets match your filters.</div>`;
        listProgress.innerHTML = ``;
        return;
    }

    if(listOpen.innerHTML.trim() === ""){
        listOpen.innerHTML = `<div class="muted">No OPEN tickets match your filters.</div>`;
    }
    if(listProgress.innerHTML.trim() === ""){
        listProgress.innerHTML = `<div class="muted">No IN_PROGRESS tickets match your filters.</div>`;
    }
}

function toggleDetails(id){
    const el = document.getElementById(id);
    if(!el) return;
    el.style.display = (el.style.display === "block") ? "none" : "block";
}


async function suggestNext(){
    // Use current period/helpType/status filters to suggest a ticket
    const period = document.getElementById("period").value.trim();
    const helpType = document.getElementById("helpType").value.trim();
    const status = document.getElementById("status").value.trim() || "OPEN";

    const qs = new URLSearchParams();
    if(period) qs.set("period", period);
    if(helpType) qs.set("help_type", helpType);
    if(status) qs.set("status", status);

    const r = await fetch("/api/suggest?" + qs.toString(), { cache:"no-store" });
    const j = await r.json();
    if(!j.ticket){
        alert("No matching tickets to suggest.");
        return;
    }

    const id = String(j.ticket.ticket_id || "");
    lastSuggestedId = id;
    showToast("Suggested: " + (j.ticket.student || "Student"), "ok");
    const card = document.getElementById("card_" + id.replace(/[^a-zA-Z0-9_]/g, "_"));
    if(card){
        card.classList.add("flash");
        card.scrollIntoView({ behavior: "smooth", block: "center" });
        setTimeout(()=>card.classList.remove("flash"), 1400);
    }else{
        alert("Suggested: " + (j.ticket.student || "Student") + " (Ticket " + id + ")");
    }
}

async function doAction(ticketId, action){
    const token = getToken();
    const payload = { ticket_id: ticketId, action: action };

    // Only attach tags/notes when resolving (keeps Claim fast + avoids slowing you down).
    if(String(action).toLowerCase() === "resolve"){
        const parts = [];
        const sel = selectedTagsByTicket[ticketId];
        if(sel && sel.size) parts.push(...Array.from(sel));

        const custom = customTagsByTicket[ticketId];
        if(custom && custom.size) parts.push(...Array.from(custom));

        if(parts.length){
            payload.tags = parts.join(", ");
        }
    }

    const headers = { "Content-Type":"application/json" };
    if(token) headers["X-Auth-Token"] = token;

    const r = await fetch("/api/action", {
        method: "POST",
        headers,
        body: JSON.stringify(payload)
    });

    if(r.status === 401){
        showToast("Unauthorized. Check passcode.", "err");
        throw new Error("UNAUTHORIZED");
    }

    const j = await r.json();
    if(!j.ok){
        const msg = (j.message || "UNKNOWN");
        if(msg === "NOT_CONFIRMED" || String(msg).startsWith("VERIFY_")){
            showToast("Not confirmed. Refresh and try again.", "err");
            throw new Error("NOT_CONFIRMED");
        }
        showToast("Not saved: " + msg, "err");
        throw new Error(msg);
    }

    // quick refresh
    load();
    return true;
}

// reload when filters change (debounced so typing doesn't spam the server)
["q","period","status","helpType","sortMode"].forEach(id => {
    document.getElementById(id).addEventListener("input", () => requestLoad());
    document.getElementById(id).addEventListener("change", () => requestLoad());
});

document.getElementById("pauseRefresh").addEventListener("change", () => {
    // If unpausing, refresh immediately.
    if(!document.getElementById("pauseRefresh").checked){
        load();
    }
    scheduleTimers();
});


function onGlobalKey(e){
    const tag = (e.target && e.target.tagName) ? e.target.tagName.toLowerCase() : "";
    if(tag === "input" || tag === "select" || tag === "textarea") return;
    const k = String(e.key||"").toLowerCase();
    if(k === "n"){ suggestNext(); }
    if(!lastSuggestedId) return;
    if(k === "c"){ doAction(lastSuggestedId, "claim"); }
    if(k === "r"){ doAction(lastSuggestedId, "resolve"); }
}
document.addEventListener("keydown", onGlobalKey);

load();
</script>
</body>
</html>
"""


# =====================================================
# START
# =====================================================

def main():
    global CFG, POLLER

    CFG = load_config()
    POLLER = SheetPoller(CFG)
    POLLER.start()

    host = "127.0.0.1"
    port = int(CFG.get("port", 5000))

    print(f"\nRunning → http://{host}:{port}\n")
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()