import os

base_dir = r'd:\Kita Co. Lab\Music Audit Tool\Music Audit\TEST Tiktok Scraper Modules\comments_data'
count = 0
for root, dirs, files in os.walk(base_dir):
    for f in files:
        if f.endswith('.sqlite3'):
            path = os.path.join(root, f)
            size = os.path.getsize(path)
            if size > 0:
                print(f'{path} : {size} bytes')
                count += 1

