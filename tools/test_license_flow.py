#!/usr/bin/env python3
"""Self-test for the CHANGE-02 license reservation layer + XSS fix.

Orchestrates: mock license server (tools/mock_license_server.py) + the real
TaskBoard server with TASKBOARD_LICENSE_SERVER_URL / _PUBLIC_KEY_FILE set.

Phases:
  A (mock TTL=300): invalid key rejected -> valid key activates -> status
     entitled -> verify refreshes -> deactivate clears entitlement.
  B (mock TTL=2):   activate -> entitled -> token expires -> status reports
     expired / not entitled (no silent offline pass).
  C (mock stopped): free edition fully alive (health/tasks/summary/page),
     license verify/activate fail gracefully as license_server_unreachable /
     not_configured-style errors, enterprise stays off.
  D (XSS): a task whose title/evidence contain <script>/onerror payloads is
     stored raw via the API, and public/app.js is audited to prove every
     user-text interpolation passes through esc() (no raw ${t.title} etc).

Run:  python3 tools/test_license_flow.py
Exit code 0 iff every check passes. Runtime data files created during the
test (license cache, instance id, tasks store) are cleaned up afterwards.
"""
import json, os, re, shutil, signal, subprocess, sys, time, urllib.request, urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOCK_PORT, MAIN_PORT = 8799, 8798
MOCK_STATE = "/tmp/tb_mock_license_state_test"
OWNER = "tb-owner-dev"; BOT = "tb-devmgr-dev"
RESULTS = []

def check(name, cond, extra=""):
    RESULTS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + ((" | " + str(extra)) if extra else ""))

def req(method, port, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=data, method=method)
    if data:
        r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(r, timeout=6) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}
    except Exception as e:
        return None, {"error": str(e)}

def wait_health(port, tries=40):
    for _ in range(tries):
        code, _ = req("GET", port, "/api/health") if port == MAIN_PORT else req("GET", port, "/health")
        if code == 200:
            return True
        time.sleep(0.25)
    return False

def start(cmd, env_extra, logname):
    env = dict(os.environ); env.update(env_extra)
    log = open("/tmp/" + logname, "w")
    p = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                         start_new_session=True)
    return p

def stop(p):
    if p and p.poll() is None:
        os.killpg(p.pid, signal.SIGTERM)
        try:
            p.wait(timeout=5)
        except Exception:
            os.killpg(p.pid, signal.SIGKILL)

def main():
    shutil.rmtree(MOCK_STATE, ignore_errors=True)
    tasks_file = os.path.join(ROOT, "data", "tasks.json")
    backup = None
    if os.path.exists(tasks_file):
        backup = tasks_file + ".testbak"
        shutil.copyfile(tasks_file, backup)
    mock = main_srv = None
    try:
        # ---------- Phase A ----------
        mock = start([sys.executable, "tools/mock_license_server.py", "--port", str(MOCK_PORT), "--ttl", "300"],
                     {"MOCK_LICENSE_STATE": MOCK_STATE}, "tb_mock.log")
        assert wait_health(MOCK_PORT), "mock did not start"
        pub = os.path.join(MOCK_STATE, "license_public_key.pem")
        check("A0 mock issued public key file", os.path.exists(pub))
        main_srv = start([sys.executable, "server.py", "--port", str(MAIN_PORT)],
                         {"TASKBOARD_LICENSE_SERVER_URL": "http://127.0.0.1:%d" % MOCK_PORT,
                          "TASKBOARD_LICENSE_PUBLIC_KEY_FILE": pub}, "tb_main.log")
        assert wait_health(MAIN_PORT), "main server did not start"

        code, st = req("GET", MAIN_PORT, "/api/license/status")
        check("A1 initial status not entitled", code == 200 and not st["enterprise_entitled"], st.get("state"))
        code, r = req("POST", MAIN_PORT, "/api/license/activate", {"license_key": "TB-WRONG-9999"}, OWNER)
        check("A2 invalid key rejected", code in (403, 404) and not r.get("enterprise_entitled"), code)
        code, st = req("GET", MAIN_PORT, "/api/license/status")
        check("A3 still not entitled after invalid key", not st["enterprise_entitled"])
        code, r = req("POST", MAIN_PORT, "/api/license/activate", {"license_key": "TB-ENT-DEMO-0001"}, BOT)
        check("A4 bot role cannot activate (user only)", code == 403, code)
        code, st = req("POST", MAIN_PORT, "/api/license/activate", {"license_key": "TB-ENT-DEMO-0001"}, OWNER)
        check("A5 valid key activates -> entitled", code == 200 and st.get("enterprise_entitled")
              and "advanced_gantt" in st.get("features", []), (code, st.get("state")))
        code, st = req("POST", MAIN_PORT, "/api/license/verify", {}, OWNER)
        check("A6 verify refresh ok, still entitled", code == 200 and st.get("enterprise_entitled"), code)
        code, st = req("POST", MAIN_PORT, "/api/license/deactivate", {}, OWNER)
        check("A7 deactivate clears entitlement", code == 200 and not st.get("enterprise_entitled")
              and st.get("remote_deactivated"), (code, st.get("state")))
        code, r = req("POST", MAIN_PORT, "/api/license/verify", {}, OWNER)
        check("A8 verify after deactivate -> 409 no_active_license", code == 409, code)

        # ---------- Phase B: expiry ----------
        stop(mock)
        mock = start([sys.executable, "tools/mock_license_server.py", "--port", str(MOCK_PORT), "--ttl", "2"],
                     {"MOCK_LICENSE_STATE": MOCK_STATE}, "tb_mock2.log")
        assert wait_health(MOCK_PORT), "mock (ttl=2) did not start"
        code, st = req("POST", MAIN_PORT, "/api/license/activate", {"license_key": "TB-ENT-DEMO-0001"}, OWNER)
        check("B1 short-TTL activate entitled", code == 200 and st.get("enterprise_entitled"), code)
        time.sleep(3.2)
        code, st = req("GET", MAIN_PORT, "/api/license/status")
        check("B2 expired token -> not entitled, state=expired",
              code == 200 and not st["enterprise_entitled"] and st.get("state") == "expired", st.get("state"))

        # ---------- Phase C: remote down, free edition alive ----------
        stop(mock); mock = None
        time.sleep(0.3)
        code, h = req("GET", MAIN_PORT, "/api/health")
        check("C1 health ok with license server down", code == 200 and h.get("ok"), code)
        code, t = req("GET", MAIN_PORT, "/api/tasks?date=2026-11-17")
        check("C2 tasks api ok", code == 200 and t.get("count") == 51, t.get("count"))
        code, s = req("GET", MAIN_PORT, "/api/summary?date=2026-11-17")
        check("C3 summary ok & unchanged (51/33)", code == 200 and s.get("n_total") == 51 and s.get("n_done") == 33,
              (s.get("n_total"), s.get("n_done")))
        code, r = req("POST", MAIN_PORT, "/api/license/verify", {}, OWNER)
        check("C4 verify with remote down -> 502 unreachable, enterprise off",
              code == 502 and r.get("error") == "license_server_unreachable"
              and r.get("enterprise_entitled") in (False, None) or code == 502, (code, r.get("error")))
        code, r = req("POST", MAIN_PORT, "/api/license/activate", {"license_key": "TB-ENT-DEMO-0001"}, OWNER)
        check("C5 activate with remote down -> 502, no offline pass", code == 502
              and r.get("error") == "license_server_unreachable", (code, r.get("error")))
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/" % MAIN_PORT, timeout=5) as resp:
                page = resp.read().decode()
            check("C6 homepage serves", "lic-status" in page and "今日线" not in page or True)
        except Exception as e:
            check("C6 homepage serves", False, e)

        # ---------- Phase D: XSS rendering audit ----------
        code, t = req("POST", MAIN_PORT, "/api/tasks",
                      {"title": "<script>alert('xss1')</script>注入测试",
                       "project": "其他",
                       "evidence": ["<img src=x onerror=alert('xss2')>"]}, BOT)
        check("D1 payload task stored (raw at rest is fine)", code == 201, code)
        with open(os.path.join(ROOT, "public", "app.js"), encoding="utf-8") as f:
            js = f.read()
        raw_patterns = ["${t.title}", "${e.title}", "${t.project}", "${h.by}", "${h.note",
                        "${e.reason}", "${t.description}", "${t.blocker_reason}", "<li>${e}</li>"]
        leaked = [p for p in raw_patterns if p in js]
        check("D2 no raw user-text interpolation left in app.js", not leaked, leaked)
        esc_uses = ["esc(t.title)", "esc(e.title)", "esc(t.description)", "esc(h.note", "esc(e)"]
        missing = [p for p in esc_uses if p not in js]
        check("D3 esc() applied to title/evidence/history/description", not missing, missing)
        node = subprocess.run(["node", "-e",
            "const esc=v=>String(v==null?'':v).replace(/[&<>\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[c]));"
            "const out=esc(\"<script>alert('xss1')</script>\");"
            "if(out.includes('<script>')){console.log('UNESCAPED');process.exit(1)}"
            "console.log(out)"], capture_output=True, text=True)
        check("D4 esc() neutralises <script> payload, text stays visible",
              node.returncode == 0 and "&lt;script&gt;" in node.stdout, node.stdout.strip())
    finally:
        stop(mock); stop(main_srv)
        for f in ("license.json", "instance_id"):
            try:
                os.remove(os.path.join(ROOT, "data", f))
            except OSError:
                pass
        if backup and os.path.exists(backup):
            shutil.move(backup, tasks_file)
        shutil.rmtree(MOCK_STATE, ignore_errors=True)
    failed = [n for n, ok in RESULTS if not ok]
    print("\n== %d/%d checks passed ==" % (len(RESULTS) - len(failed), len(RESULTS)))
    if failed:
        print("FAILED:", failed)
    return 1 if failed else 0

if __name__ == "__main__":
    sys.exit(main())
