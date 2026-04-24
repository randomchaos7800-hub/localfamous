"""Fetch a URL and return clean markdown text."""

import logging

log = logging.getLogger("frank.tools.web_fetch")

SCHEMA = {
    "name": "web_fetch",
    "description": "Fetch a URL and return its content as clean text/markdown. Good for reading articles, docs, GitHub files.",
    "parameters": {
        "url": {
            "type": "string",
            "description": "URL to fetch",
        },
        "max_chars": {
            "type": "number",
            "description": "Max characters to return (default: 8000)",
        },
    },
    "required": ["url"],
}


async def execute(url: str, max_chars: int = 8000, ctx: dict = {}) -> str:
    import asyncio
    import requests
    from bs4 import BeautifulSoup

    def _fetch():
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
        }
        try:
            r = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
            r.raise_for_status()
            content_type = r.headers.get("content-type", "")

            if "json" in content_type:
                return r.text[:max_chars]

            if "html" not in content_type and "text" not in content_type:
                return f"[Binary content: {content_type}]"

            soup = BeautifulSoup(r.text, "lxml")

            # Remove noise
            for tag in soup(["script", "style", "nav", "footer", "header", "aside", "ads"]):
                tag.decompose()

            # Try to find main content
            main = (
                soup.find("main")
                or soup.find("article")
                or soup.find(id="content")
                or soup.find(class_="content")
                or soup.find("body")
                or soup
            )

            text = main.get_text(separator="\n", strip=True)
            # Collapse blank lines
            lines = [l for l in text.splitlines() if l.strip()]
            return "\n".join(lines)

        except requests.exceptions.Timeout:
            return f"Error: request timed out for {url}"
        except requests.exceptions.HTTPError as e:
            return f"Error: HTTP {e.response.status_code} for {url}"
        except Exception as e:
            return f"Error fetching {url}: {e}"

    result = await asyncio.to_thread(_fetch)
    if len(result) > max_chars:
        result = result[:max_chars] + f"\n\n[Truncated at {max_chars} chars]"
    return result
