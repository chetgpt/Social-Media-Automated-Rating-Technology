import os, sqlite3

base_dir = r'd:\Kita Co. Lab\Music Audit Tool\Music Audit\TEST Tiktok Scraper Modules\comments_data'
runs = []
for root, dirs, files in os.walk(base_dir):
    if '.git' in root: continue
    for f in files:
        if f.endswith('.sqlite') or f.endswith('.sqlite3'):
            path = os.path.join(root, f)
            if os.path.getsize(path) > 0:
                try:
                    conn = sqlite3.connect(path)
                    c = conn.cursor()
                    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='engage_tiktok_runs'")
                    if c.fetchone():
                        c.execute("SELECT run_id, workflow, status, evidence_ready FROM engage_tiktok_runs")
                        for r in c.fetchall():
                            runs.append({'path': path, 'run_id': r[0], 'workflow': r[1], 'status': r[2], 'count': r[3]})
                    conn.close()
                except Exception as e:
                    pass

for run in runs:
    print(f"Path: {run['path']} | Run ID: {run['run_id']} | Workflow: {run['workflow']} | Status: {run['status']} | Count: {run['count']}")
