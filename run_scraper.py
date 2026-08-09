#!/usr/bin/env python
"""Runner script for the TikTok comment scraper.

This script is the main entry point for the TikTok comment scraper.
It parses command-line arguments and launches the scraper with the specified options.

Usage:
    python run_scraper.py                   # Scrape 3 videos (default)
    python run_scraper.py --videos 10       # Scrape 10 videos
    python run_scraper.py --percentage 75   # Scrape 75% of comments (default is 50%)
    python run_scraper.py --record          # Record comment button location
    python run_scraper.py --url URL         # Scrape a specific TikTok URL with no limits
    python run_scraper.py --url URL --max-videos 200  # Scrape a specific URL with maximum of 200 videos
"""
import sys
import os
import asyncio
import time
from tiktok_scraper.main import main

if __name__ == "__main__":
    # Check if the script was run with the --record flag
    record_mode = "--record" in sys.argv
    
    # Check if a specific URL was provided
    url = None
    for i, arg in enumerate(sys.argv):
        if arg == "--url" and i + 1 < len(sys.argv):
            url = sys.argv[i + 1]
    
    # If no URL provided via command line, ask the user
    if not url and not record_mode:
        url = input("Enter TikTok URL to scrape (e.g., https://www.tiktok.com/@username): ")
    
    # Parse number of videos to scrape (only used if no specific URL provided)
    num_videos = 3  # Default is now 3 videos to prioritize quality over quantity
    for i, arg in enumerate(sys.argv):
        if arg == "--videos" and i + 1 < len(sys.argv):
            try:
                num_videos = int(sys.argv[i + 1])
                if num_videos <= 0:
                    print("Number of videos must be positive. Using default (3).")
                    num_videos = 3
            except ValueError:
                print(f"Invalid number of videos: {sys.argv[i + 1]}. Using default (3).")
    
    # Parse maximum videos to scrape (used even with specific URL)
    max_videos = 500  # Default maximum
    for i, arg in enumerate(sys.argv):
        if arg == "--max-videos" and i + 1 < len(sys.argv):
            try:
                max_videos = int(sys.argv[i + 1])
                if max_videos <= 0:
                    print("Maximum videos must be positive. Using default (500).")
                    max_videos = 500
            except ValueError:
                print(f"Invalid maximum videos: {sys.argv[i + 1]}. Using default (500).")
    
    # Display startup information
    print("\n===== TikTok Comment Scraper =====")
    print(f"Mode: {'Recording' if record_mode else 'Scraping'}")
    if not record_mode:
        if url:
            print(f"URL to scrape: {url}")
            print(f"Maximum videos to scrape: {max_videos}")
        else:
            print(f"Videos to scrape: {num_videos}")
    print("=================================\n")
    
    if record_mode:
        print("\n" + "="*50)
        print("RUNNING IN RECORDING MODE")
        print("="*50)
        print("\nIn this mode, you will manually click the comment button,")
        print("and the script will record information about that element")
        print("for future automated use.")
        print("\nPress Ctrl+C now if you want to cancel.")
        print("Otherwise, the browser will launch in 5 seconds...")
        time.sleep(5)
    else:
        if url:
            print("The scraper will gather ALL available comments from videos.")
            print("This may take a significant amount of time depending on the account.\n")
        else:
            print("The scraper will gather approximately 50% of available comments from each video.")
            print("This takes time but produces much more comprehensive results.\n")
    
    # Run the main scraper function
    asyncio.run(main(record_mode=record_mode, num_videos=num_videos, default_percentage=100, target_url=url, max_videos=max_videos)) 