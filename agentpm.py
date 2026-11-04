#!/usr/bin/env python3
"""BNH PM Compliance Agent — command line.  (Use .venv/bin/python when DATABASE_URL points at Neon.)

  agentpm.py db init | seed | status                   create tables / load demo data + first CoS / check connection
  agentpm.py users                                       list accounts
  agentpm.py user-add "Name" email@x.com COS|PM          add an account
  agentpm.py user-deactivate email@x.com                 deactivate + end their sessions
  agentpm.py assign P01 email@x.com                      make a PM responsible for a portfolio
  agentpm.py run [--llm mock|openrouter] [--mode auto|assist|shadow] [--as-of YYYY-MM-DD] [--portfolio P01]
  agentpm.py serve [--port 8765] [--host 127.0.0.1] [--daily-at 08:00]
  agentpm.py review | approve REV-x [--note] | reject REV-x [--dismiss] [--note]
  agentpm.py dismiss <fingerprint> --reason ... | suppress P04 RULE [--month] [--days] --reason ...
  agentpm.py severity <fingerprint> LEVEL --reason ... | pause | resume | overrides | outbox | reset
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import date, timedelta

import getpass
import socket

from agent.actions import decide_review
from agent.audit import CATEGORIES, set_context
from agent.config import load_config
from agent.datasource import DbDataSource, seed_demo
from agent.mailer import Mailer
from agent.runner import get_state, run_once
from agent.store import now_iso

BY = "cli"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="BNH PM Compliance Agent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("db"); d.add_argument("action", choices=["init", "seed", "status"])
    sub.add_parser("users")
    ua = sub.add_parser("user-add"); ua.add_argument("name"); ua.add_argument("email"); ua.add_argument("role", choices=["COS", "PM"])
    ud = sub.add_parser("user-deactivate"); ud.add_argument("email")
    asg = sub.add_parser("assign"); asg.add_argument("portfolio"); asg.add_argument("email")
    r = sub.add_parser("run")
    r.add_argument("--llm", choices=["mock", "openrouter"], help="default: the AI setting chosen in Settings")
    r.add_argument("--mode", choices=["auto", "assist", "shadow"])
    r.add_argument("--as-of", help="evaluate as if today were this date (default: today, WAT)")
    r.add_argument("--portfolio", action="append", help="limit to portfolio id (repeatable)")
    r.add_argument("--trigger", default="manual", choices=["manual", "schedule", "webhook"])
    r.add_argument("--delay", type=float, default=0.0, help="seconds per node, to watch it in the visualizer")
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--host", default="127.0.0.1")

    sub.add_parser("review")
    a = sub.add_parser("approve"); a.add_argument("id"); a.add_argument("--note", default="")
    j = sub.add_parser("reject"); j.add_argument("id"); j.add_argument("--note", default="")
    j.add_argument("--dismiss", action="store_true", help="also mark the finding as a false positive")
    di = sub.add_parser("dismiss"); di.add_argument("fingerprint"); di.add_argument("--reason", required=True)
    u = sub.add_parser("suppress"); u.add_argument("portfolio"); u.add_argument("rule")
    u.add_argument("--month", default="*"); u.add_argument("--days", type=int, default=30)
    u.add_argument("--reason", required=True)
    v = sub.add_parser("severity"); v.add_argument("fingerprint")
    v.add_argument("level", choices=["LOW", "MEDIUM", "HIGH", "CRITICAL"]); v.add_argument("--reason", required=True)
    for k in ("pause", "resume"):
        x = sub.add_parser(k); x.add_argument("--reason", default="")
    sub.add_parser("overrides"); sub.add_parser("outbox"); sub.add_parser("reset")
    au = sub.add_parser("audit", help="show the audit trail")
    au.add_argument("--category", choices=CATEGORIES); au.add_argument("--q"); au.add_argument("--actor")
    au.add_argument("--run"); au.add_argument("--limit", type=int, default=40); au.add_argument("--json", action="store_true")
    sub.add_parser("audit-verify", help="verify the audit hash chain")
    ax = sub.add_parser("audit-export", help="write the audit trail to CSV"); ax.add_argument("path")
    args = ap.parse_args(argv)
    cfg = load_config()
    try:
        who = getpass.getuser()
    except Exception:  # noqa: BLE001
        who = "unknown"
    # CLI actions are audited as the OS user on this machine
    set_context(actor=f"cli:{who}@{socket.gethostname()}"[:254], actor_role="CLI", ip="local")

    if args.cmd == "run":
        print(json.dumps(run_once(mode=args.mode, llm_kind=args.llm, as_of=args.as_of, trigger=args.trigger,
                                  portfolio_ids=args.portfolio, step_delay=args.delay,
                                  triggered_by=f"cli:{who}"), indent=2))
        return 0
    if args.cmd == "serve":
        from server import serve
        serve(args.port, host=args.host)
        return 0

    state = get_state(cfg)
    db = state.db
    if args.cmd == "db":
        if args.action == "status":
            print(f"{db.describe()}\n{db.ping()}")
            for t in ("users", "portfolios", "submissions", "findings", "review_items", "email_log", "overrides"):
                print(f"  {t:<14} {db.one(f'SELECT COUNT(*) AS n FROM {t}')['n']}")
        elif args.action == "init":
            print(f"schema ready on {db.describe()}")
        else:
            if not cfg.seed_cos_email:
                print("Set SEED_COS_EMAIL in .env first (the first Chief of Staff account, used to sign in).")
                return 1
            c = seed_demo(db, cfg, cfg.data_dir)
            n = state.seed_overrides()
            print(f"seeded on {db.describe()}: {c}, overrides +{n}")
    elif args.cmd == "users":
        for row in db.q("SELECT u.name, u.email, u.role, u.status, u.last_login_at, u.last_login_ip, "
                        "(SELECT COUNT(*) FROM portfolios p WHERE p.pm_user_id = u.id) AS portfolios "
                        "FROM users u ORDER BY u.role, u.name"):
            print(f"{row['role']:<4} {row['status']:<11} {row['email']:<36} {row['name']:<22} "
                  f"portfolios={row['portfolios']}  last login {row['last_login_at'] or '—'} {row['last_login_ip'] or ''}")
    elif args.cmd == "user-add":
        uid = str(uuid.uuid4())
        db.insert("users", {"id": uid, "name": args.name, "email": args.email.strip().lower(),
                            "role": args.role, "status": "ACTIVE", "created_at": now_iso()})
        state.audit("user.created", target_type="user", target_id=uid, summary=f"{args.role} account created for {args.email}",
                    email=args.email.strip().lower(), name=args.name, role=args.role)
        print(f"added {args.role} {args.email}")
    elif args.cmd == "user-deactivate":
        row = db.one("SELECT id FROM users WHERE email = %s", [args.email.strip().lower()])
        if not row:
            print("no such user"); return 1
        db.x("UPDATE users SET status = 'DEACTIVATED' WHERE id = %s", [row["id"]])
        db.x("UPDATE auth_sessions SET revoked_at = %s WHERE user_id = %s AND revoked_at IS NULL", [now_iso(), row["id"]])
        state.audit("user.status_changed", target_type="user", target_id=row["id"],
                    summary=f"{args.email} deactivated (sessions revoked)", email=args.email, after="DEACTIVATED")
        print("deactivated; sessions revoked")
    elif args.cmd == "assign":
        row = db.one("SELECT id, role FROM users WHERE email = %s", [args.email.strip().lower()])
        if not row or row["role"] != "PM":
            print("no PM account with that email (add one with user-add)"); return 1
        n = db.x("UPDATE portfolios SET pm_user_id = %s WHERE id = %s", [row["id"], args.portfolio])
        if n:
            state.audit("portfolio.pm_assigned", target_type="portfolio", target_id=args.portfolio,
                        summary=f"{args.portfolio}: PM → {args.email}", after=args.email)
        print("assigned" if n else "no such portfolio")
    elif args.cmd == "review":
        items = [i for i in state.review.all() if i["status"] == "pending"]
        for i in items:
            f = i["finding"]
            print(f"{i['id']}  {f['portfolio_id']} {f['rule_id']:<24} {i['action_type']:<15} "
                  f"conf={f['confidence']:.2f}  fp={f['fingerprint']}\n          held because: {i['reason']}"
                  + (f"\n          agent note: {f['llm_note']}" if f.get("llm_note") else ""))
        print(f"{len(items)} pending")
    elif args.cmd in ("approve", "reject"):
        snap = DbDataSource(db).load()
        res = decide_review(args.id, args.cmd, BY, args.note, state, snap, Mailer(cfg, state),
                            dismiss_finding=getattr(args, "dismiss", False))
        print(f"{args.id} -> {res['item']['status']}")
    elif args.cmd == "dismiss":
        print(state.overrides.add("DISMISS", BY, fingerprint=args.fingerprint, reason=args.reason))
    elif args.cmd == "suppress":
        until = (date.today() + timedelta(days=args.days)).isoformat()
        print(state.overrides.add("SUPPRESS", BY, portfolio_id=args.portfolio, rule_id=args.rule,
                                  reporting_month=args.month, until=until, reason=args.reason))
    elif args.cmd == "severity":
        print(state.overrides.add("SEVERITY", BY, fingerprint=args.fingerprint, severity=args.level, reason=args.reason))
    elif args.cmd in ("pause", "resume"):
        print(state.overrides.add(args.cmd.upper(), BY, reason=args.reason))
    elif args.cmd == "overrides":
        print(json.dumps(state.overrides.all(), indent=2, default=str))
    elif args.cmd == "outbox":
        for m in state.emails.list():
            print(f"{m['ts'][:19]}  {m['status']:<9} {m['delivery']:<9} {m['to_email']:<32} {m['subject']}")
    elif args.cmd == "audit":
        rows = state.trail.list(limit=args.limit, category=args.category, q=args.q, actor=args.actor, run_id=args.run)
        if args.json:
            print(json.dumps(rows, indent=2, default=str))
        for r in ([] if args.json else reversed(rows)):
            print(f"#{r['seq']:<6} {r['ts'][:19]}  {r['outcome']:<7} {r['event']:<26} {(r['actor'] or ''):<28} "
                  f"{(r['ip'] or ''):<16} {r['summary'] or ''}")
    elif args.cmd == "audit-verify":
        res = state.trail.verify()
        state.audit("audit.verified", outcome="success" if res["ok"] else "failure",
                    summary=f"chain {'intact' if res['ok'] else 'BROKEN at seq ' + str(res['broken_at'])}",
                    **{k: v for k, v in res.items() if k != "head"})
        print(("✓ chain intact: " if res["ok"] else "✖ chain BROKEN: ") + json.dumps(res))
        return 0 if res["ok"] else 2
    elif args.cmd == "audit-export":
        text = state.trail.export_csv()
        with open(args.path, "w", encoding="utf-8-sig", newline="") as fh:
            fh.write(text)
        state.audit("audit.exported", summary=f"CSV export to {args.path}", rows=text.count(chr(10)) - 1)
        print(f"wrote {text.count(chr(10)) - 1} rows to {args.path}")
    elif args.cmd == "reset":
        state.reset()
        print("agent activity cleared (users, portfolios, submissions kept; seed override restored)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
