#!/usr/bin/env python3
"""TaskBoard — 任务完成看板 (MIT, self-written, Python stdlib only).

Run:  python3 server.py [--port 8780]
Data: data/tasks.json (runtime store, auto-initialised from data/seed_tasks.json)
Auth: Authorization: Bearer <token>  (see tokens.json; tokens map to bot_id + role)
      role=bot  : create / update / transition / submit-for-acceptance
      role=user : accept / reject (only way to reach status=done, PRD Q5)
Summary numbers are ALWAYS aggregated live from task details. Never hand-filled.
"""
import argparse, base64, calendar, hashlib, hmac, json, os, re, sys, threading, urllib.request, urllib.error, uuid
from datetime import datetime, timezone, date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
STORE = os.path.join(DATA_DIR, "tasks.json")
SEED = os.path.join(DATA_DIR, "seed_tasks.json")
TOKENS_FILE = os.path.join(ROOT, "tokens.json")
PUBLIC = os.path.join(ROOT, "public")
LOCK = threading.Lock()

STATUSES = ["todo", "in_progress", "pending_acceptance", "done",
            "awaiting_user", "blocked", "shelved", "cancelled"]
BLOCKER_TYPES = ["awaiting_user", "external", "internal", "resource"]

# PRD §4.2 state machine. `done` is intentionally NOT reachable via generic
# transition: only POST /accept (user role) may set it.
TRANSITIONS = {
    "todo":               ["in_progress", "awaiting_user", "blocked", "shelved", "cancelled"],
    "in_progress":        ["pending_acceptance", "awaiting_user", "blocked", "shelved", "cancelled"],
    "pending_acceptance": ["in_progress"],  # reject path also handled by /reject
    "awaiting_user":      ["in_progress", "shelved", "cancelled"],
    "blocked":            ["in_progress", "shelved", "cancelled"],
    "shelved":            ["todo", "cancelled"],
    "done":               [],
    "cancelled":          [],
}

def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

# ---- statistics periods (CHANGE-03) -------------------------------------
PERIODS = ("day", "week", "month", "year")

def parse_anchor(s):
    """Strict YYYY-MM-DD -> date, else ValueError (spec §4: 400, never
    silently fall back to day behaviour)."""
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", s or ""):
        raise ValueError("anchor 必须是 YYYY-MM-DD 格式，收到：%r" % (s,))
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError("anchor 不是合法日期：%s" % s)

def period_range(period, anchor):
    """CHANGE-03 §1: inclusive [start, end] dates of the period that
    contains anchor. Week = ISO week (Mon..Sun), month = calendar month,
    year = calendar year."""
    if period == "day":
        return anchor, anchor
    if period == "week":
        start = anchor - timedelta(days=anchor.isoweekday() - 1)
        return start, start + timedelta(days=6)
    if period == "month":
        start = anchor.replace(day=1)
        last = calendar.monthrange(anchor.year, anchor.month)[1]
        return start, start.replace(day=last)
    if period == "year":
        return date(anchor.year, 1, 1), date(anchor.year, 12, 31)
    raise ValueError("period 必须是 day/week/month/year，收到：%r" % (period,))

def resolve_scope(q):
    """CHANGE-03 §4. Returns (predicate, echo) where echo carries
    period/anchor/range_start/range_end for the summary response.
    period present -> filter task `date` into [range_start, range_end]
    (the legacy `date` param is ignored, period wins, no stacking);
    period absent -> legacy exact-`date` behaviour, unchanged."""
    period = q.get("period", [None])[0]
    if period is not None:
        if period not in PERIODS:
            raise ValueError("period 必须是 day/week/month/year，收到：%r" % period)
        raw_anchor = q.get("anchor", [None])[0]
        anchor = parse_anchor(raw_anchor) if raw_anchor else datetime.now(timezone.utc).date()
        start, end = period_range(period, anchor)
        lo, hi = start.isoformat(), end.isoformat()
        return (lambda t: lo <= t["date"] <= hi), {
            "period": period, "anchor": anchor.isoformat(),
            "range_start": lo, "range_end": hi}
    d = q.get("date", [None])[0]
    if d:
        return (lambda t: t["date"] == d), {
            "period": None, "anchor": None, "range_start": d, "range_end": d}
    return (lambda t: True), {
        "period": None, "anchor": None, "range_start": None, "range_end": None}

def load_tokens():
    with open(TOKENS_FILE, encoding="utf-8") as f:
        return json.load(f)["tokens"]  # {token: {"bot_id":..., "role":...}}

def load_store():
    if not os.path.exists(STORE):
        with open(SEED, encoding="utf-8") as f:
            seed = json.load(f)
        tasks = []
        for i, s in enumerate(seed["tasks"], 1):
            t = {
                "id": "TB-%03d" % i,
                "title": s["title"],
                "project": s["project"],
                "description": s.get("description", ""),
                "status": s["status"],
                "progress": s["progress"],
                "assignee_bot": s.get("assignee_bot", "main"),
                "reporter": s.get("reporter", "main"),
                "date": s.get("date", "2026-11-17"),
                "started_at": s.get("started_at"),
                "due_at": s.get("due_at"),
                "completed_at": s.get("completed_at"),
                "blocker_type": s.get("blocker_type"),
                "blocker_reason": s.get("blocker_reason"),
                "evidence": s.get("evidence", []),
                "acceptance": s.get("acceptance"),
                "carried_over": s.get("carried_over", False),
                "created_at": s.get("created_at", "2026-11-17T00:00:00Z"),
                "updated_at": s.get("updated_at", "2026-11-17T00:00:00Z"),
                "history": s.get("history") or [
                    {"at": s.get("created_at", "2026-11-17T00:00:00Z"),
                     "by": s.get("reporter", "main"), "from": None,
                     "to": s["status"], "note": "种子数据录入（2026-11-17 当天真实任务，来源 ~/memory/2026-11-17.md）"}
                ],
            }
            tasks.append(t)
        save_store(tasks)
        return tasks
    with open(STORE, encoding="utf-8") as f:
        return json.load(f)["tasks"]

def save_store(tasks):
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"tasks": tasks}, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STORE)

def is_excluded(t):
    """PRD §3.3: items removed from the assistant-side denominator."""
    if t["status"] == "awaiting_user":
        return True, "待用户拍板"
    if t["status"] == "blocked" and t.get("blocker_type") in ("external", "awaiting_user"):
        return True, "被卡住（%s）" % t["blocker_type"]
    if t["status"] == "shelved":
        return True, "已搁置（用户下令）"
    return False, ""

def summarize(tasks, all_tasks=None):
    """Aggregate everything from details. No hand-filled numbers anywhere.
    `tasks` is the (possibly period-filtered) set every rate is computed
    from; `all_tasks` is the unfiltered store, used ONLY for the
    point-in-time pending_my_action count (CHANGE-03 §2: it is an action
    inbox, never period-filtered)."""
    effective = [t for t in tasks if t["status"] != "cancelled"]
    n_total = len(effective)
    done = [t for t in effective if t["status"] == "done"]
    n_done = len(done)
    by_status = {s: 0 for s in STATUSES}
    for t in tasks:
        by_status[t["status"]] += 1
    excluded = []
    for t in effective:
        ex, why = is_excluded(t)
        if ex:
            excluded.append({"id": t["id"], "title": t["title"], "project": t["project"],
                             "status": t["status"], "blocker_type": t.get("blocker_type"),
                             "reason": t.get("blocker_reason") or why})
    n_excl = len(excluded)
    denom_a = n_total - n_excl
    by_project = {}
    for t in effective:
        p = by_project.setdefault(t["project"], {"project": t["project"], "total": 0, "done": 0})
        p["total"] += 1
        if t["status"] == "done":
            p["done"] += 1
    for p in by_project.values():
        p["rate"] = round(p["done"] / p["total"] * 100, 2) if p["total"] else None
    pending_src = all_tasks if all_tasks is not None else tasks
    n_pending_now = sum(1 for t in pending_src
                        if t["status"] in ("pending_acceptance", "awaiting_user"))
    return {
        "n_total": n_total,
        "n_done": n_done,
        "n_cancelled": by_status["cancelled"],
        "total_rate": round(n_done / n_total * 100, 2) if n_total else None,
        "assistant_rate": round(n_done / denom_a * 100, 2) if denom_a else None,
        "assistant_numerator": n_done,
        "assistant_denominator": denom_a,
        "n_excluded": n_excl,
        "excluded": excluded,
        "by_status": by_status,
        "by_project": sorted(by_project.values(), key=lambda x: x["project"]),
        "pending_my_action": n_pending_now,
        "self_check": {"detail_count": len(tasks), "status_sum": sum(by_status.values()),
                       "consistent": len(tasks) == sum(by_status.values())},
    }

def filter_tasks(tasks, q):
    in_scope, _echo = resolve_scope(q)  # raises ValueError on bad period/anchor
    out = [t for t in tasks if in_scope(t)]
    proj = q.get("project", [None])[0]
    if proj:
        wanted = set(proj.split(","))
        out = [t for t in out if t["project"] in wanted]
    status = q.get("status", [None])[0]
    if status:
        wanted = set(status.split(","))
        out = [t for t in out if t["status"] in wanted]
    kw = q.get("q", [None])[0]
    if kw:
        out = [t for t in out if kw.lower() in t["title"].lower()]
    return out

def apply_progress_rule(task, new_status):
    """PRD §3.4 progress fixing/freezing."""
    if new_status == "todo":
        task["progress"] = 0
    elif new_status == "pending_acceptance":
        task["progress"] = 95
    elif new_status == "done":
        task["progress"] = 100
    # blocked / awaiting_user / shelved: freeze at current value (no change)

# =====================================================================
# Enterprise license reservation layer (CHANGE-02-license.md)
#
# Design (honest strength boundary, per CHANGE-02 §4): the signing PRIVATE
# key exists ONLY on the remote license server. This process holds just
# the public key and verifies short-lived, signed entitlement tokens that
# the remote server issues after checking a license key. There is NO
# offline pass-through and NO local issuing capability in this codebase:
# with no valid remote-issued token, enterprise entitlement is off.
# Pure client-side software can never be made mathematically uncrackable;
# the goal is remote issuance + short-lived tokens + server-side
# revocation, so cracking costs more than buying. The Community edition
# never depends on any of this: if the license server is unreachable,
# every free feature keeps working and only enterprise reports itself
# unverifiable.
#
# Config (environment):
#   TASKBOARD_LICENSE_SERVER_URL       e.g. http://127.0.0.1:8791
#   TASKBOARD_LICENSE_PUBLIC_KEY_FILE  PEM file, RSA public key of the
#                                      license server (default below)
# =====================================================================
LICENSE_SERVER_URL = os.environ.get("TASKBOARD_LICENSE_SERVER_URL", "").rstrip("/")
LICENSE_PUBLIC_KEY_FILE = os.environ.get(
    "TASKBOARD_LICENSE_PUBLIC_KEY_FILE", os.path.join(ROOT, "license_public_key.pem"))
LICENSE_CACHE = os.path.join(DATA_DIR, "license.json")
INSTANCE_FILE = os.path.join(DATA_DIR, "instance_id")
TOKEN_PREFIX = "TB1"

def _b64url_decode(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

def _der_read(buf, i):
    tag = buf[i]; i += 1
    ln = buf[i]; i += 1
    if ln & 0x80:
        n = ln & 0x7F
        ln = int.from_bytes(buf[i:i+n], "big"); i += n
    return tag, buf[i:i+ln], i + ln

def _parse_rsa_public_key(pem_text):
    """Minimal DER parser: PKCS#1 RSAPublicKey or SPKI PEM -> (n, e)."""
    b64 = "".join(l.strip() for l in pem_text.splitlines() if not l.startswith("-----"))
    der = base64.b64decode(b64)
    _, seq, _ = _der_read(der, 0)
    tag, first, nxt = _der_read(seq, 0)
    if tag == 0x30:  # SPKI: algorithm sequence, then BIT STRING wrapping PKCS#1
        _, bitstr, _ = _der_read(seq, nxt)
        _, seq, _ = _der_read(bitstr[1:], 0)
        tag, first, nxt = _der_read(seq, 0)
    n = int.from_bytes(first, "big")
    _, e_bytes, _ = _der_read(seq, nxt)
    return n, int.from_bytes(e_bytes, "big")

def _load_public_key():
    try:
        with open(LICENSE_PUBLIC_KEY_FILE, encoding="utf-8") as f:
            return _parse_rsa_public_key(f.read())
    except Exception:
        return None

def _rsa_verify_sha256(pub, message, signature):
    """RSASSA-PKCS1-v1_5 + SHA-256 verification, stdlib only."""
    n, e = pub
    k = (n.bit_length() + 7) // 8
    if len(signature) != k:
        return False
    em = pow(int.from_bytes(signature, "big"), e, n).to_bytes(k, "big")
    t = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(message).digest()
    expected = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    return hmac.compare_digest(em, expected)

def get_instance_id():
    try:
        with open(INSTANCE_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        iid = uuid.uuid4().hex
        with open(INSTANCE_FILE, "w", encoding="utf-8") as f:
            f.write(iid)
        return iid

def verify_entitlement_token(token):
    """Local check of a remote-issued token. Returns (payload, error_code).
    Only verifies signature/expiry/binding — it can never ISSUE anything."""
    if not token or not isinstance(token, str):
        return None, "no_token"
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
        return None, "bad_format"
    pub = _load_public_key()
    if pub is None:
        return None, "public_key_not_configured"
    try:
        signature = _b64url_decode(parts[2])
        payload = json.loads(_b64url_decode(parts[1]).decode("utf-8"))
    except Exception:
        return None, "bad_format"
    signed = (parts[0] + "." + parts[1]).encode("ascii")
    if not _rsa_verify_sha256(pub, signed, signature):
        return None, "bad_signature"
    if payload.get("product") != "taskboard":
        return None, "wrong_product"
    if payload.get("instance_id") != get_instance_id():
        return None, "instance_mismatch"
    if int(payload.get("expires_at", 0)) <= int(datetime.now(timezone.utc).timestamp()):
        return None, "expired"
    return payload, None

def _load_license_cache():
    try:
        with open(LICENSE_CACHE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_license_cache(cache):
    tmp = LICENSE_CACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)
    os.replace(tmp, LICENSE_CACHE)

def _clear_license_cache():
    try:
        os.remove(LICENSE_CACHE)
    except OSError:
        pass

def license_status():
    """Single source of truth for enterprise entitlement on this instance."""
    cache = _load_license_cache()
    base = {"edition": "community", "enterprise_entitled": False, "features": [],
            "expires_at": None, "license_server_configured": bool(LICENSE_SERVER_URL),
            "instance_id": get_instance_id(), "state": "none",
            "detail": "未激活企业版；免费版全部功能不受影响。"}
    if not cache.get("token"):
        return base
    payload, err = verify_entitlement_token(cache["token"])
    if err:
        base["state"] = "expired" if err == "expired" else "invalid"
        base["detail"] = "权益令牌无效（%s），企业版未开启；免费版不受影响。" % err
        return base
    base.update({"edition": "enterprise", "enterprise_entitled": True,
                 "features": payload.get("features", []),
                 "expires_at": datetime.fromtimestamp(payload["expires_at"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "state": "active",
                 "detail": "远程授权服务器签发的短期权益令牌，本地验签通过。"})
    return base

def enterprise_entitled(feature=None):
    """Unified gate entry: ALL enterprise feature checks must go through
    here (server) / GET /api/license/status (clients). No other code path
    may decide enterprise availability."""
    st = license_status()
    if not st["enterprise_entitled"]:
        return False
    if feature is None:
        return True
    feats = st["features"]
    return "*" in feats or "enterprise" in feats or feature in feats

def _license_remote(action, payload):
    """POST to the remote license server. Returns (http_status, json_dict).
    Raises OSError/URLError on network failure."""
    req = urllib.request.Request(
        LICENSE_SERVER_URL + "/v1/" + action,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {"error": "license_server_error"}

class Handler(BaseHTTPRequestHandler):
    server_version = "TaskBoard/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # ---- helpers -------------------------------------------------------
    def send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return None

    def auth(self, need_role=None):
        """Returns (identity, error_response_tuple)."""
        hdr = self.headers.get("Authorization") or ""
        token = hdr[7:] if hdr.startswith("Bearer ") else self.headers.get("X-Bot-Token", "")
        tokens = load_tokens()
        ident = tokens.get(token)
        if not ident:
            return None, (401, {"error": "unauthorized", "detail": "缺少或无效的令牌（Authorization: Bearer <token>）"})
        if need_role and ident["role"] != need_role:
            return None, (403, {"error": "forbidden",
                                "detail": "此动作需要 role=%s，当前身份 %s(role=%s)" % (need_role, ident["bot_id"], ident["role"])})
        return ident, None

    def find(self, tid):
        tasks = load_store()
        for t in tasks:
            if t["id"] == tid:
                return tasks, t
        return tasks, None

    def record(self, task, by, frm, to, note):
        task["history"].append({"at": now_iso(), "by": by, "from": frm, "to": to, "note": note or ""})
        task["updated_at"] = now_iso()

    # ---- routing -------------------------------------------------------
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/api/health":
            return self.send_json(200, {"ok": True, "service": "taskboard", "version": "1.0.0"})
        if u.path == "/api/license/status":
            with LOCK:
                return self.send_json(200, license_status())
        if u.path == "/api/tasks":
            with LOCK:
                try:
                    tasks = filter_tasks(load_store(), q)
                    _pred, echo = resolve_scope(q)
                except ValueError as e:
                    return self.send_json(400, {"error": "bad_period_params", "detail": str(e)})
            resp = {"tasks": tasks, "count": len(tasks)}
            resp.update(echo)
            return self.send_json(200, resp)
        m = re.match(r"^/api/tasks/([\w-]+)$", u.path)
        if m:
            with LOCK:
                _, t = self.find(m.group(1))
            if not t:
                return self.send_json(404, {"error": "not_found"})
            return self.send_json(200, t)
        if u.path == "/api/summary":
            with LOCK:
                all_tasks = load_store()
                try:
                    tasks = filter_tasks(all_tasks, q)
                    _pred, echo = resolve_scope(q)
                except ValueError as e:
                    return self.send_json(400, {"error": "bad_period_params", "detail": str(e)})
            s = summarize(tasks, all_tasks=all_tasks)
            s.update(echo)
            return self.send_json(200, s)
        # static files
        path = u.path.lstrip("/") or "index.html"
        full = os.path.normpath(os.path.join(PUBLIC, path))
        if not full.startswith(PUBLIC) or not os.path.isfile(full):
            return self.send_json(404, {"error": "not_found", "path": u.path})
        ctype = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8"}.get(os.path.splitext(full)[1], "application/octet-stream")
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        u = urlparse(self.path)
        body = self.read_body()
        if body is None:
            return self.send_json(400, {"error": "bad_json"})
        if u.path == "/api/tasks":
            return self.create_task(body)
        m = re.match(r"^/api/license/(activate|verify|deactivate)$", u.path)
        if m:
            return self.license_action(m.group(1), body)
        m = re.match(r"^/api/tasks/([\w-]+)/(transition|submit|accept|reject)$", u.path)
        if m:
            tid, action = m.group(1), m.group(2)
            if action == "transition":
                return self.transition(tid, body)
            if action == "submit":
                return self.submit(tid, body)
            if action == "accept":
                return self.accept(tid, body)
            if action == "reject":
                return self.reject(tid, body)
        return self.send_json(404, {"error": "not_found", "path": u.path})

    def do_PATCH(self):
        u = urlparse(self.path)
        m = re.match(r"^/api/tasks/([\w-]+)$", u.path)
        if not m:
            return self.send_json(404, {"error": "not_found"})
        body = self.read_body()
        if body is None:
            return self.send_json(400, {"error": "bad_json"})
        ident, err = self.auth(need_role="bot")
        if err:
            return self.send_json(*err)
        with LOCK:
            tasks, t = self.find(m.group(1))
            if not t:
                return self.send_json(404, {"error": "not_found"})
            if t["status"] in ("done", "cancelled"):
                return self.send_json(409, {"error": "terminal_state", "detail": "终态任务不可修改，如需重做请新建任务"})
            if "progress" in body:
                p = body["progress"]
                if t["status"] != "in_progress":
                    return self.send_json(422, {"error": "progress_rule",
                        "detail": "只有进行中状态可上报进度（PRD §3.4），当前状态 %s 的 progress 由状态规则固定/冻结" % t["status"]})
                if not isinstance(p, int) or p % 10 != 0 or not (10 <= p <= 80):
                    return self.send_json(422, {"error": "progress_rule",
                        "detail": "进行中 progress 必须是 10 的倍数且在 10–80 之间（PRD §3.4）"})
                if not (body.get("note") or "").strip():
                    return self.send_json(422, {"error": "progress_rule",
                        "detail": "进度更新必须带一句进度备注 note（PRD §3.4）"})
                old = t["progress"]
                t["progress"] = p
                self.record(t, ident["bot_id"], t["status"], t["status"],
                            "进度 %s→%s：%s" % (old, p, body["note"].strip()))
            for k in ("title", "description", "project", "due_at", "started_at"):
                if k in body:
                    t[k] = body[k]
            if "evidence" in body and isinstance(body["evidence"], list):
                t["evidence"] = body["evidence"]
            t["reporter"] = ident["bot_id"]
            t["updated_at"] = now_iso()
            save_store(tasks)
            return self.send_json(200, t)

    # ---- license actions (CHANGE-02) -----------------------------------
    def license_action(self, action, body):
        ident, err = self.auth(need_role="user")
        if err:
            return self.send_json(*err)
        with LOCK:
            if action == "deactivate":
                cache = _load_license_cache()
                remote_ok = False
                if LICENSE_SERVER_URL and cache.get("token"):
                    try:
                        code, _ = _license_remote("deactivate",
                            {"entitlement_token": cache["token"], "instance_id": get_instance_id()})
                        remote_ok = code == 200
                    except Exception:
                        remote_ok = False
                _clear_license_cache()
                st = license_status()
                st["remote_deactivated"] = remote_ok
                return self.send_json(200, st)
            if not LICENSE_SERVER_URL:
                return self.send_json(503, {"error": "license_server_not_configured",
                    "detail": "未配置 TASKBOARD_LICENSE_SERVER_URL，企业版无法远程验证；免费版全部功能不受影响。",
                    "enterprise_entitled": False})
            cache = _load_license_cache()
            try:
                if action == "activate":
                    key = (body.get("license_key") or "").strip()
                    if not key:
                        return self.send_json(422, {"error": "validation", "detail": "license_key 必填"})
                    code, resp = _license_remote("activate",
                        {"license_key": key, "instance_id": get_instance_id(), "product": "taskboard"})
                else:  # verify: refresh the cached token against the remote server
                    if not cache.get("token"):
                        return self.send_json(409, {"error": "no_active_license",
                            "detail": "本地无已缓存的权益令牌，请先 activate"})
                    code, resp = _license_remote("verify",
                        {"entitlement_token": cache["token"], "instance_id": get_instance_id(), "product": "taskboard"})
            except Exception:
                st = license_status()
                return self.send_json(502, {"error": "license_server_unreachable",
                    "detail": "远程授权服务器不可达，企业版暂时无法验证；免费版全部功能照常运行。",
                    "enterprise_entitled": st["enterprise_entitled"], "status": st})
            if code != 200 or not resp.get("entitlement_token"):
                if action == "verify" and code in (401, 403, 410):
                    _clear_license_cache()  # remote says revoked/expired: drop it
                return self.send_json(code if 400 <= code < 600 else 502,
                    {"error": resp.get("error", "license_rejected"),
                     "detail": resp.get("detail", "远程授权服务器拒绝了本次验证"),
                     "enterprise_entitled": False})
            token = resp["entitlement_token"]
            payload, verr = verify_entitlement_token(token)
            if verr:
                return self.send_json(502, {"error": "token_verification_failed",
                    "detail": "远程返回的权益令牌本地验签未通过（%s），未予采信" % verr,
                    "enterprise_entitled": False})
            _save_license_cache({"token": token, "verified_at": now_iso(),
                                 "license_id": payload.get("license_id")})
            return self.send_json(200, license_status())

    # ---- actions -------------------------------------------------------
    def create_task(self, body):
        ident, err = self.auth(need_role="bot")
        if err:
            return self.send_json(*err)
        for k in ("title", "project"):
            if not (body.get(k) or "").strip():
                return self.send_json(422, {"error": "validation", "detail": "字段 %s 必填" % k})
        with LOCK:
            tasks = load_store()
            nums = [int(t["id"].split("-")[1]) for t in tasks if t["id"].startswith("TB-") and t["id"][3:].isdigit()]
            t = {
                "id": "TB-%03d" % ((max(nums) + 1) if nums else 1),
                "title": body["title"].strip(),
                "project": body["project"].strip(),
                "description": body.get("description", ""),
                "status": "todo", "progress": 0,
                "assignee_bot": body.get("assignee_bot") or ident["bot_id"],
                "reporter": ident["bot_id"],
                "date": body.get("date") or now_iso()[:10],
                "started_at": body.get("started_at"), "due_at": body.get("due_at"),
                "completed_at": None, "blocker_type": None, "blocker_reason": None,
                "evidence": body.get("evidence") or [],
                "acceptance": None, "carried_over": False,
                "created_at": now_iso(), "updated_at": now_iso(), "history": [],
            }
            self.record(t, ident["bot_id"], None, "todo", body.get("note", "创建任务"))
            tasks.append(t)
            save_store(tasks)
            return self.send_json(201, t)

    def transition(self, tid, body):
        ident, err = self.auth(need_role="bot")
        if err:
            return self.send_json(*err)
        to = body.get("to_status")
        if to not in STATUSES:
            return self.send_json(422, {"error": "validation", "detail": "to_status 必须是 8 种状态之一"})
        if to == "done":
            return self.send_json(403, {"error": "acceptance_required",
                "detail": "已完成只能由用户验收通过产生，请走 POST /api/tasks/%s/accept（PRD Q5）" % tid})
        with LOCK:
            tasks, t = self.find(tid)
            if not t:
                return self.send_json(404, {"error": "not_found"})
            frm = t["status"]
            if to not in TRANSITIONS[frm]:
                return self.send_json(409, {"error": "illegal_transition",
                    "detail": "不允许从 %s 流转到 %s（PRD §4.2）" % (frm, to),
                    "allowed": TRANSITIONS[frm]})
            if to == "pending_acceptance":
                ev = body.get("evidence") or t.get("evidence")
                if not ev:
                    return self.send_json(422, {"error": "evidence_required",
                        "detail": "无证据不得进入待验收（PRD §4.2），请在 evidence 中提供交付物/截图/上线地址"})
                t["evidence"] = ev
            if to == "blocked":
                bt, br = body.get("blocker_type"), (body.get("blocker_reason") or "").strip()
                if bt not in BLOCKER_TYPES:
                    return self.send_json(422, {"error": "validation", "detail": "进入被卡住必须带合法 blocker_type（%s）" % "/".join(BLOCKER_TYPES)})
                if not br:
                    return self.send_json(422, {"error": "validation", "detail": "进入被卡住必须带 blocker_reason"})
                t["blocker_type"], t["blocker_reason"] = bt, br
            if to == "shelved":
                br = (body.get("blocker_reason") or body.get("note") or "").strip()
                if not br:
                    return self.send_json(422, {"error": "validation", "detail": "进入已搁置必须带原因（用户下令搁置的依据）"})
                t["blocker_reason"] = br
            if to == "awaiting_user":
                t["blocker_reason"] = (body.get("blocker_reason") or body.get("note") or t.get("blocker_reason"))
            if to == "in_progress":
                t["blocker_type"], t["blocker_reason"] = None, None
                if t["progress"] in (0, 95, 100):
                    t["progress"] = 10
            if to == "todo":
                t["progress"] = 0
            apply_progress_rule(t, to)
            t["status"] = to
            t["reporter"] = ident["bot_id"]
            self.record(t, ident["bot_id"], frm, to, body.get("note", ""))
            save_store(tasks)
            return self.send_json(200, t)

    def submit(self, tid, body):
        body = dict(body or {})
        body["to_status"] = "pending_acceptance"
        return self.transition(tid, body)

    def accept(self, tid, body):
        ident, err = self.auth(need_role="user")
        if err:
            return self.send_json(*err)
        with LOCK:
            tasks, t = self.find(tid)
            if not t:
                return self.send_json(404, {"error": "not_found"})
            if t["status"] != "pending_acceptance":
                return self.send_json(409, {"error": "illegal_transition",
                    "detail": "只有待验收任务可验收通过，当前状态 %s" % t["status"]})
            frm = t["status"]
            t["status"] = "done"
            t["progress"] = 100
            t["completed_at"] = now_iso()
            t["acceptance"] = {"by": ident["bot_id"], "at": now_iso(),
                               "result": "accepted", "note": body.get("note", "")}
            self.record(t, ident["bot_id"], frm, "done", body.get("note", "用户验收通过"))
            save_store(tasks)
            return self.send_json(200, t)

    def reject(self, tid, body):
        ident, err = self.auth(need_role="user")
        if err:
            return self.send_json(*err)
        reason = (body.get("reason") or "").strip()
        if not reason:
            return self.send_json(422, {"error": "validation", "detail": "打回必须填写原因 reason（PRD §4.2）"})
        with LOCK:
            tasks, t = self.find(tid)
            if not t:
                return self.send_json(404, {"error": "not_found"})
            if t["status"] != "pending_acceptance":
                return self.send_json(409, {"error": "illegal_transition",
                    "detail": "只有待验收任务可打回，当前状态 %s" % t["status"]})
            frm = t["status"]
            t["status"] = "in_progress"
            t["progress"] = min(t["progress"], 80) if t["progress"] <= 80 else 80
            if t["progress"] == 95:
                t["progress"] = 80
            t["acceptance"] = {"by": ident["bot_id"], "at": now_iso(),
                               "result": "rejected", "note": reason}
            self.record(t, ident["bot_id"], frm, "in_progress", "用户打回：" + reason)
            save_store(tasks)
            return self.send_json(200, t)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8780")))
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    load_store()  # initialise runtime store from seed on first run
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print("TaskBoard listening on http://%s:%d  (Ctrl+C to stop)" % (args.host, args.port))
    srv.serve_forever()

if __name__ == "__main__":
    main()
