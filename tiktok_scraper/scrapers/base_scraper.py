import logging
from typing import List, Dict, Any

class BaseScraper:
    def __init__(self, platform_name: str):
        self.platform_name = platform_name
        self.logger = logging.getLogger(self.platform_name)

    async def search(self, query: str, max_videos: int = 10) -> List[Dict[str, Any]]:
        """
        Search for videos on the platform.
        Returns a list of dictionaries, where each dict has at least 'video_id' and 'url'.
        """
        raise NotImplementedError("Subclasses must implement search()")

    async def extract_comments(self, video_data: Dict[str, Any], max_comments: int = 100) -> Dict[str, Any]:
        """
        Extract comments for a specific video.
        Returns a dictionary containing video metadata and a list of comments.
        """
        raise NotImplementedError("Subclasses must implement extract_comments()")

    def format_output(self, raw_comments: List[Any]) -> List[Dict[str, Any]]:
        """
        Format the raw comments into the standard JSON structure required by the analytics pipeline.
        Standard format for a comment:
        {
            "id": "...",
            "text": "...",
            "author": "...",
            "likes": 0,
            "replies": [],
            "platform": "tiktok"
        }
        """
        raise NotImplementedError("Subclasses must implement format_output()")
