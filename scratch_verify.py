import urllib.request

handles = ['@elevenlabsio', '@elevenlabs', '@runwayml', '@heygen_official', '@suno', '@suno_ai_', '@sunoai']

for h in handles:
    try:
        url = f'https://www.tiktok.com/{h}'
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        html = urllib.request.urlopen(req).read().decode('utf-8')
        if '"uniqueId":"' in html or 'uniqueId' in html:
            print(f'{h}: VALID')
        else:
            print(f'{h}: INVALID')
    except Exception as e:
        print(f'{h}: ERROR {e}')
