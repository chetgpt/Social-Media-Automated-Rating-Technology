import os, sqlite3

base_dir = r'd:\Kita Co. Lab\Music Audit Tool\Music Audit\TEST Tiktok Scraper Modules\comments_data'

db_files = []
for root, dirs, files in os.walk(base_dir):
    for f in files:
        if f.endswith('.sqlite3') or f.endswith('.db') or f.endswith('.sqlite'):
            db_files.append(os.path.join(root, f))

eligible_runs = []

for db_path in db_files:
    try:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='workflow_state'")
        if not c.fetchone():
            conn.close()
            continue
            
        c.execute("SELECT run_id, workflow, status, collection_count FROM workflow_state ORDER BY updated_at DESC LIMIT 1")
        row = c.fetchone()
        if row:
            run_id, workflow, status, collection_count = row
            post_count = 0
            try:
                c.execute("SELECT COUNT(*) FROM posts")
                post_count = c.fetchone()[0]
            except:
                pass
                
            eligible_runs.append({
                'path': db_path,
                'run_id': run_id,
                'workflow': workflow,
                'status': status,
                'count': post_count
            })
        conn.close()
    except Exception as e:
        pass

for run in eligible_runs:
    print(f"Path: {run['path']} | Run ID: {run['run_id']} | Workflow: {run['workflow']} | Status: {run['status']} | Count: {run['count']}")

