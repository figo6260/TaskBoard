# TaskBoard · 任务完成看板

[中文](README.md) | English

A task-quantification dashboard for a "solo owner + multiple AI bots" workflow: every time a bot finishes a piece of work it writes it through the API, and the owner can see — in 3 seconds on a phone — how much got done today, what is stuck, and what is waiting for their decision.

- Overview with a large percentage ring + total breakdown (numerator and denominator always visible)
- Assistant-side completion rate (excludes items waiting for the owner's decision, blocked externally, or shelved by the owner — every exclusion is expandable and auditable)
- Per-project bar chart (absolute numbers + percentages)
- Gantt chart (the timeline always covers the full selected day 00:00–24:00; when the selected day is today, a "now" line is always shown at the current time; tasks without start/end times go to an unscheduled area — no fake times are invented)
- 8-state machine + owner acceptance/rejection (a task only counts as done after the owner accepts it)
- Open REST API: bots write directly with individual tokens; all summaries are aggregated live from task details — no hand-entered numbers

## Quick start (zero dependencies)

Only the Python 3.10+ standard library is needed — no `pip install` at all:

```bash
python3 server.py            # default http://127.0.0.1:8780
python3 server.py --port 9000
```

Then open the address in a browser. On first start, runtime data `data/tasks.json` is generated from `data/seed_tasks.json` (sanitized demo data: same structure and statistical distribution as real use, fictional content); delete `data/tasks.json` to reset back to the seed.

## API

Tokens live in `tokens.json` (the values in this repo are local development demo values — replace them before any non-local deployment). Send requests with the header `Authorization: Bearer <token>`. `role=bot` may write tasks; only `role=user` (the owner) can accept/reject.

| Method | Path | Description |
|---|---|---|
| GET | `/api/health` | Health check |
| GET | `/api/tasks?date=&period=&anchor=&project=&status=&q=` | Task list (filterable) |
| GET | `/api/tasks/{id}` | Single task detail (evidence, blockers, history) |
| GET | `/api/summary?date=&period=&anchor=&project=&status=&q=` | Summary: totals / done count / overall completion rate / assistant-side rate / per-project / per-status counts / exclusion details / self-check |

Statistics periods: `period` accepts day/week/month/year (week = ISO Monday–Sunday, month = calendar month, year = calendar year) and `anchor` is the anchor date (YYYY-MM-DD, defaults to today). When both are given, tasks are filtered by the derived [range_start, range_end] and `date` is ignored; without `period`, the legacy exact-`date` behaviour is kept. The summary response echoes `period`/`anchor`/`range_start`/`range_end`; an invalid period/anchor returns 400. The "waiting for me" count in the summary is a point-in-time number (all pending-acceptance + awaiting-decision items, counted live) and is not affected by the period.
| POST | `/api/tasks` | Create a task (bot) |
| PATCH | `/api/tasks/{id}` | Update progress (in-progress must be a multiple of 10 between 10–80 and carry a note) / fields / evidence (bot) |
| POST | `/api/tasks/{id}/transition` | State transition (bot, validated against the state machine) |
| POST | `/api/tasks/{id}/submit` | Submit for acceptance (bot; rejected without evidence; progress fixed at 95) |
| POST | `/api/tasks/{id}/accept` | Accept → done (user only, progress 100) |
| POST | `/api/tasks/{id}/reject` | Reject → in progress (user only, reason required, progress falls back to ≤80) |
| GET | `/api/license/status` | Enterprise entitlement status (single entry point; read-only verification result of the locally cached token) |
| POST | `/api/license/activate` | Submit a license key → verified by the remote license server → exchanged for a short-lived entitlement token (user only) |
| POST | `/api/license/verify` | Re-verify remotely and refresh the entitlement token (user only) |
| POST | `/api/license/deactivate` | Deactivate: notify the remote server to revoke + clear the local token cache (user only) |

Key validations: no evidence → cannot enter pending-acceptance; no `blocker_type` + `blocker_reason` → cannot enter blocked; no bot can ever set a task to done directly; terminal states (done/cancelled) cannot transition further.

## Metrics (reproducible definitions)

- Overall completion rate = done / effective total (total − cancelled)
- Per-project completion rate = project done / project effective total
- Assistant-side completion rate = done / (effective total − awaiting owner decision − blocked with external/awaiting_user − shelved)
- progress: todo 0; in progress 10–80 (multiples of 10, note required); blocked / awaiting decision / shelved frozen; pending acceptance 95; done 100

## Technology choices

- Backend: Python standard library `http.server`, zero third-party dependencies, one-command startup.
- Frontend: vanilla HTML/CSS/JS; the ring, bars and Gantt are all hand-written SVG (no chart library).
- Gantt choice: Frappe Gantt (MIT) was evaluated; to keep zero dependencies and full control over the metrics, the MVP implements the same functionality as hand-written SVG (start/end bars + progress fill + grouping + full-day timeline + today line). It can be swapped for Frappe Gantt, or the Enterprise advanced Gantt (see Editions), when drag-to-reschedule / dependency arrows are needed.
- Only mechanisms were learned from open-source projects (Plane's view organisation, OpenProject's Gantt conventions, Vikunja's API-first design, Kanboard's statistics definitions); no AGPL/GPL code was copied.

## Editions (Open Core)

This repository is the **Community edition**, run under an Open Core model (the full split is an internal operations document):

- **Community (MIT, free and open source)**: everything in this repo — the complete simple task-quantification set: overview ring, assistant-side completion rate, per-project bar chart, basic Gantt, task details with the 8-state flow, filters, open API read/write and summaries, direct bot-token writes, owner acceptance/rejection, the "waiting for me" inbox, and data self-checks. Single machine / self-hosted, single workspace.
- **Enterprise (paid, kept out of this repo)**: advanced features are reserved for a paid edition and will ship later as a separate private repository plugged in as an extension pack. **This repo contains no Enterprise feature code**; only the remote license-verification interface layer is reserved (see below). Candidate list (final list to be decided separately; this section is not a price/feature commitment):
  - Multi-user / multi-workspace, org structure and fine-grained permissions, SSO
  - Advanced historical-trend analytics, burndown / cumulative flow, period views, baseline comparison (planned vs. actual assessment)
  - Advanced Gantt with task dependencies / milestones / critical path
  - Batch operations, scheduled notifications/reminders, audit report export and sharing
  - Large-scale / high-concurrency use, enterprise support and SLA

### Enterprise extension points (reserved only, not implemented)

To allow a future Enterprise edition to plug in, the architecture keeps only these placeholders (the MVP implements no Enterprise features):

1. **Single entitlement entry point**: whether Enterprise features are available is decided solely by entitlement status — server-side `enterprise_entitled()`, client-side `GET /api/license/status`, and front-end `window.TB_ENTERPRISE`. Every Enterprise feature must go through this entry point; no local boolean switches.
2. **View plug-in placeholder**: the front end renders four independent blocks (overview / bars / Gantt / details); future advanced views (trends / burndown / dependency Gantt) can mount as new blocks without touching the existing four.
3. **Data boundary**: all metrics depend only on task-detail fields (see the API table); Enterprise analytics consume the same details, so the Community edition needs no embedded paid logic.

### Enterprise license-key interface (reserved layer)

The free edition **does not depend** on a license key: with no key at all, every feature above works as usual. License keys are reserved for future Enterprise features only, and verification **must be remote**:

- **Configuration**: environment variables `TASKBOARD_LICENSE_SERVER_URL` (remote license server URL) and `TASKBOARD_LICENSE_PUBLIC_KEY_FILE` (path to the license server's RSA public key PEM, default `license_public_key.pem` in the repo root).
- **Flow**: `activate` submits license key + this instance's ID to the remote server → after remote checks (existence / activation / expiry / binding) the server issues a **signed, expiring, short-lived entitlement token** → the local side verifies the signature (public key), expiry and instance binding, then caches the token; `verify` refreshes the token remotely (the remote side can revoke; revoked/expired tokens stop working); `deactivate` notifies the remote server to revoke and clears the local cache. After a token expires, Enterprise features switch off automatically until re-verified remotely.
- **No local issuing capability**: the signing private key exists only on the remote license server; the main service contains public-key verification only. There is no offline bypass — with no remote configured or reachable, Enterprise is simply inactive.
- **When the remote is unreachable**: all free-edition APIs and pages keep working; only Enterprise features report "cannot verify". Revocation/expiry stops Enterprise features only — it never locks user data.
- **Strength boundary (stated honestly)**: no purely local software can be made absolutely uncrackable. This design aims to make cracking cost more than buying: remote issuing, short-lived tokens, server-side revocation/banning of abusive keys, and — in the future — delivering the valuable Enterprise capabilities with remote participation rather than a local switch.
- **Local integration testing**: `tools/mock_license_server.py` is a test-only mock remote license server (its private key lives only in its own state directory; demo license key `TB-ENT-DEMO-0001`); `python3 tools/test_license_flow.py` runs the full activation / invalid key / expiry / remote shutdown flow in one command. The real license server and issuing console will be built when the Enterprise edition is started.

## Layout

```
server.py            Backend (API + static file serving + reserved license layer)
public/              Frontend (index.html / style.css / app.js)
data/seed_tasks.json Seed data (sanitized demo data)
data/tasks.json      Runtime data (generated on first start, gitignored)
data/license.json    Entitlement token cache (generated at runtime, gitignored)
data/instance_id     Local instance ID (generated at runtime, gitignored)
tools/               Mock remote license server + full license-flow test script (test only)
tokens.json          Token table (development demo values — replace before production)
LICENSE              MIT
```

## License

MIT — see [LICENSE](LICENSE).
