import subprocess
import json
import sys

# Ensure UTF-8 output
sys.stdout.reconfigure(encoding='utf-8')

handles = ["@pandufuzztoni", "@peraukertas", "@sychowayne"]
summary = []

for handle in handles:
    print(f"--- Pulsing {handle} ---")
    try:
        res = subprocess.run(
            ["C:\\Users\\DELL\\AppData\\Local\\Programs\\Python\\Python311\\python.exe", "quick_audit_tiktok.py", "collect", "--creator", handle, "--posts", "3"],
            capture_output=True, text=True, timeout=240, encoding='utf-8'
        )
        
        if res.returncode != 0:
            if "TikTok shallow discovery failed" in res.stdout or "TikTok shallow discovery failed" in res.stderr:
                print(f"[{handle}] Not found on TikTok.")
                summary.append(f"{handle}: Not Found")
            else:
                print(f"[{handle}] Error: {res.stderr}")
                summary.append(f"{handle}: Error")
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
            summary.append(f"{handle}: Found {len(posts)} posts")
            for p in posts:
                print(f" - Caption: {p.get('caption', '')[:100]}...")
        else:
            print(f"[{handle}] Could not parse output.\n")
            summary.append(f"{handle}: Parse error")
            
    except subprocess.TimeoutExpired:
        print(f"[{handle}] Timeout.\n")
        summary.append(f"{handle}: Timeout")
    except Exception as e:
        print(f"[{handle}] Exception: {e}\n")
        summary.append(f"{handle}: Exception")

print("\n--- BATCH SUMMARY ---")
for s in summary:
    print(s)
