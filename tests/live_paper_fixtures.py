"""Synthetic exact0.1SOL quote/cost/FX records, never operator transactions."""
import datetime as dt
import json
import sqlite3

from test_forward_evidence_accounting import _row

PROPOSAL='checked-paper-proposal'


def snapshot():
    stamp=dt.datetime.now(dt.timezone.utc).isoformat()
    return {'wallet_sol':1.,'dry_run':True,'updated_at':stamp,'wallet_checked_at':stamp}


def write_cohort(root, *, net=10., count=50, age_hours=48):
    started=dt.datetime.now(dt.timezone.utc)-dt.timedelta(hours=age_hours)
    rows=[_row(address=f'LiveEvidenceMint{i}',opened=started+dt.timedelta(minutes=30*i),pct=net)
          for i in range(count)]
    data=root/'data'
    data.mkdir(exist_ok=True)
    (data/'paper_closed_trades.jsonl').write_text('\n'.join(json.dumps(row) for row in rows),encoding='utf-8')
    from analytics.paper_forward import write_paper_forward_report
    report=write_paper_forward_report({'proposal_id':PROPOSAL,'forward_run_id':'trial',
        'forward_started_at':started.isoformat(),'config_hash':'hash','config_profile':'challenger'},root=root)
    with sqlite3.connect(data/'memebotdatabase.db') as db:
        db.execute('CREATE TABLE IF NOT EXISTS bot_runtime_state (bot_id TEXT PRIMARY KEY, wallet_sol REAL, dry_run INTEGER, updated_at TEXT, wallet_checked_at TEXT)')
        value=snapshot()
        db.execute('INSERT OR REPLACE INTO bot_runtime_state VALUES (?,?,?,?,?)',('main',1.,1,value['updated_at'],value['wallet_checked_at']))
    return report
