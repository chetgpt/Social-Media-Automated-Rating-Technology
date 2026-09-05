import sys
import time
from playwright.sync_api import sync_playwright

def search_tiktok_handles():
    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
            context = browser.contexts[0]
            page = context.new_page()
            
            queries = ["runwayml", "luma ai", "pika labs", "suno ai", "udio"]
            results = {}
            
            for query in queries:
                print(f"Searching for {query}...")
                page.goto(f"https://www.tiktok.com/search/user?q={query.replace(' ', '%20')}")
                page.wait_for_timeout(3000)
                
                # Try to extract the first user result
                users = page.query_selector_all('[data-e2e="search-user-info-container"]')
                if users:
                    first_user = users[0]
                    # get the text content of the element containing the title
                    title_elem = first_user.query_selector('p[data-e2e="search-user-title"]')
                    if title_elem:
                        handle = title_elem.inner_text().strip()
                        results[query] = handle
                        print(f"Found {handle} for {query}")
                    else:
                        print(f"No handle found for {query}")
                else:
                    print(f"No user results found for {query}")
            
            page.close()
            return results
        except Exception as e:
            print(f"Error: {e}")

if __name__ == "__main__":
    search_tiktok_handles()
