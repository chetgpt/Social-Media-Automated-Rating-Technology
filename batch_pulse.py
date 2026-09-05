import subprocess
import json
import re

handles = ["@badpilot_band", "@siementsband", "@sayitright.official"]
summary = []

for handle in handles:
    print(f"--- Pulsing {handle} ---")
    try:
        res = subprocess.run(
            ["C:\\Users\\DELL\\AppData\\Local\\Programs\\Python\\Python311\\python.exe", "quick_audit_tiktok.py", "collect", "--creator", handle, "--posts", "3"],
            capture_output=True, text=True, timeout=180
        )
        if res.returncode != 0:
            print(f"[{handle}] Error or not found.\n")
            summary.append(f"{handle}: Not Found / Error")
            continue
        
        lines = res.stdout.strip().split('\n')
        data = None
        for line in reversed(lines):
            if line.startswith('{') and 'snapshot' in line:
                try:
                    data = json.loads(line)
                    break
                except:
                    pass
                    
        if data and 'snapshot' in data:
            posts = data['snapshot'].get('posts', [])
            print(f"[{handle}] Found {len(posts)} posts.")
            summary.append(f"{handle}: Found {len(posts)} posts.")
            for p in posts:
                print(f" - Caption: {p.get('caption', '')[:100]}...")
        else:
            print(f"[{handle}] Could not parse output.\n")
            summary.append(f"{handle}: Parse error")
            
    except Exception as e:
        print(f"[{handle}] Exception: {e}\n")
        summary.append(f"{handle}: Timeout/Exception")

print("\n--- BATCH SUMMARY ---")
for s in summary:
    print(s)
