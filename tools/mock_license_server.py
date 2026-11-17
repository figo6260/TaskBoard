#!/usr/bin/env python3
"""MOCK remote license server for TaskBoard — TEST ONLY, not the product.

This simulates the future remote authorization service described in
CHANGE-02-license.md. It is the ONLY place a signing private key exists;
the main TaskBoard service (server.py) never sees it.

Run:
    python3 tools/mock_license_server.py [--port 8791] [--ttl 3600]

State (keys, license registry, revocations) lives in
tools/.mock_license_state/ (gitignored) or $MOCK_LICENSE_STATE.
On first run it generates a 1024-bit RSA keypair (pure stdlib, test-grade
size only) and writes the PUBLIC key to <state>/license_public_key.pem —
point TASKBOARD_LICENSE_PUBLIC_KEY_FILE at that file for the main server.

Demo license keys (seeded on first run, override with $MOCK_LICENSE_KEYS):
    TB-ENT-DEMO-0001   (active, enterprise features)
    TB-ENT-REVOKED-0002 (present but inactive/revoked)

Token format: TB1.<b64url(payload_json)>.<b64url(RSASSA-PKCS1v15-SHA256 sig)>
"""
import argparse, base64, hashlib, json, os, secrets, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE_DIR = os.environ.get("MOCK_LICENSE_STATE") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".mock_license_state")
KEY_FILE = os.path.join(STATE_DIR, "private_key.json")
PUB_FILE = os.path.join(STATE_DIR, "license_public_key.pem")
REG_FILE = os.path.join(STATE_DIR, "licenses.json")
REVOKED_FILE = os.path.join(STATE_DIR, "revoked_tokens.json")
TTL = int(os.environ.get("MOCK_TOKEN_TTL", "3600"))

# ---------- pure-stdlib RSA (test grade) ----------
def _is_probable_prime(n, rounds=16):
    if n < 2:
        return False
    small = [2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37]
    for p in small:
        if n % p == 0:
            return n == p
    d, r = n - 1, 0
    while d % 2 == 0:
        d //= 2; r += 1
    for _ in range(rounds):
        a = secrets.randbelow(n - 3) + 2
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True

def _gen_prime(bits):
    while True:
        c = secrets.randbits(bits) | (1 << (bits - 1)) | 1
        if _is_probable_prime(c):
            return c

def _der_len(n):
    if n < 128:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b

def _der_int(x):
    b = x.to_bytes(max(1, (x.bit_length() + 7) // 8), "big")
    if b[0] & 0x80:
        b = b"\x00" + b
    return b"\x02" + _der_len(len(b)) + b

def _public_key_pem(n, e):
    body = _der_int(n) + _der_int(e)
    der = b"\x30" + _der_len(len(body)) + body
    b64 = base64.b64encode(der).decode()
    lines = "\n".join(b64[i:i+64] for i in range(0, len(b64), 64))
    return "-----BEGIN RSA PUBLIC KEY-----\n%s\n-----END RSA PUBLIC KEY-----\n" % lines

def load_or_create_keys():
    os.makedirs(STATE_DIR, exist_ok=True)
    if os.path.exists(KEY_FILE):
        with open(KEY_FILE) as f:
            k = json.load(f)
        return k["n"], k["e"], k["d"]
    print("mock: generating 1024-bit RSA keypair (first run only)...", file=sys.stderr)
    e = 65537
    while True:
        p, q = _gen_prime(512), _gen_prime(512)
        if p == q:
            continue
        phi = (p - 1) * (q - 1)
        try:
            d = pow(e, -1, phi)
        except ValueError:
            continue
        n = p * q
        break
    with open(KEY_FILE, "w") as f:
        json.dump({"n": n, "e": e, "d": d}, f)
    os.chmod(KEY_FILE, 0o600)
    with open(PUB_FILE, "w") as f:
        f.write(_public_key_pem(n, e))
    return n, e, d

def load_registry():
    if os.path.exists(REG_FILE):
        with open(REG_FILE) as f:
            return json.load(f)
    keys = [k.strip() for k in os.environ.get(
        "MOCK_LICENSE_KEYS", "TB-ENT-DEMO-0001").split(",") if k.strip()]
    reg = {k: {"plan": "enterprise", "active": True,
               "features": ["multi_workspace", "advanced_gantt", "trends",
                            "baseline_review", "audit_export", "batch_ops", "notifications"]}
           for k in keys}
    reg["TB-ENT-REVOKED-0002"] = {"plan": "enterprise", "active": False, "features": []}
    with open(REG_FILE, "w") as f:
        json.dump(reg, f, indent=1)
    return reg

def load_revoked():
    if os.path.exists(REVOKED_FILE):
        with open(REVOKED_FILE) as f:
            return set(json.load(f))
    return set()

def save_revoked(s):
    with open(REVOKED_FILE, "w") as f:
        json.dump(sorted(s), f)

N, E, D = load_or_create_keys()

def _sign(message: bytes) -> bytes:
    k = (N.bit_length() + 7) // 8
    t = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(message).digest()
    em = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    return pow(int.from_bytes(em, "big"), D, N).to_bytes(k, "big")

def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")

def _b64url_decode(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

def issue_token(license_key, instance_id):
    reg = load_registry()
    lic = reg[license_key]
    now = int(time.time())
    payload = {"product": "taskboard", "license_id": license_key,
               "instance_id": instance_id, "plan": lic["plan"],
               "features": lic["features"], "issued_at": now,
               "expires_at": now + TTL}
    body = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64url(_sign(("TB1." + body).encode("ascii")))
    return "TB1." + body + "." + sig, payload

def parse_and_verify_token(token):
    """Mock-side check: signature + revocation. Returns (payload, error)."""
    try:
        head, body, sig = token.split(".")
        if head != "TB1":
            return None, "bad_format"
        import hmac as _h
        k = (N.bit_length() + 7) // 8
        em = pow(int.from_bytes(_b64url_decode(sig), "big"), E, N).to_bytes(k, "big")
        t = bytes.fromhex("3031300d060960864801650304020105000420") + \
            hashlib.sha256(("TB1." + body).encode("ascii")).digest()
        expected = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
        if not _h.compare_digest(em, expected):
            return None, "bad_signature"
        payload = json.loads(_b64url_decode(body).decode())
    except Exception:
        return None, "bad_format"
    if sig in load_revoked():
        return None, "revoked"
    if payload.get("expires_at", 0) <= int(time.time()):
        return None, "expired"
    return payload, None

class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("mock-license: %s\n" % (fmt % args))

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            return self._json(200, {"ok": True, "service": "mock-license-server",
                                    "public_key_file": PUB_FILE, "token_ttl_seconds": TTL})
        return self._json(404, {"error": "not_found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n).decode() or "{}")
        except Exception:
            return self._json(400, {"error": "bad_json"})
        if self.path == "/v1/activate":
            key = (body.get("license_key") or "").strip()
            inst = body.get("instance_id") or ""
            reg = load_registry()
            if key not in reg:
                return self._json(404, {"error": "license_not_found", "detail": "注册码不存在"})
            if not reg[key]["active"]:
                return self._json(403, {"error": "license_inactive", "detail": "注册码已停用/吊销"})
            if not inst:
                return self._json(422, {"error": "validation", "detail": "instance_id 必填"})
            token, payload = issue_token(key, inst)
            return self._json(200, {"entitlement_token": token, "expires_at": payload["expires_at"]})
        if self.path == "/v1/verify":
            payload, err = parse_and_verify_token(body.get("entitlement_token") or "")
            if err:
                return self._json(410 if err == "expired" else 401,
                                  {"error": "token_" + err, "detail": "权益令牌%s" % err})
            if payload.get("instance_id") != body.get("instance_id"):
                return self._json(401, {"error": "instance_mismatch", "detail": "实例不匹配"})
            reg = load_registry()
            lic = reg.get(payload.get("license_id"))
            if not lic or not lic["active"]:
                return self._json(403, {"error": "license_inactive", "detail": "注册码已停用/吊销"})
            token, payload = issue_token(payload["license_id"], payload["instance_id"])
            return self._json(200, {"entitlement_token": token, "expires_at": payload["expires_at"]})
        if self.path == "/v1/deactivate":
            token = body.get("entitlement_token") or ""
            parts = token.split(".")
            if len(parts) == 3:
                revoked = load_revoked()
                revoked.add(parts[2])
                save_revoked(revoked)
            return self._json(200, {"ok": True})
        return self._json(404, {"error": "not_found"})

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("MOCK_LICENSE_PORT", "8791")))
    ap.add_argument("--ttl", type=int, default=None)
    args = ap.parse_args()
    global TTL
    if args.ttl is not None:
        TTL = args.ttl
    print("mock license server on http://127.0.0.1:%d (ttl=%ss, state=%s, pubkey=%s)"
          % (args.port, TTL, STATE_DIR, PUB_FILE))
    ThreadingHTTPServer(("127.0.0.1", args.port), H).serve_forever()

if __name__ == "__main__":
    main()
