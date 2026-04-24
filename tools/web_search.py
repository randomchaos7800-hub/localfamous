"""Search the web. DuckDuckGo HTML (no API key) with optional SerpAPI fallback."""

import logging

log = logging.getLogger("frank.tools.web_search")

SCHEMA = {
    "name": "web_search",
    "description": "Search the web for current information. Returns titles, URLs, and snippets.",
    "parameters": {
        "query": {
            "type": "string",
            "description": "Search query",
        },
        "num_results": {
            "type": "number",
            "description": "Number of results to return (default: 8, max: 20)",
        },
    },
    "required": ["query"],
}


async def execute(query: str, num_results: int = 8, ctx: dict = {}) -> str:
    import asyncio

    num_results = min(int(num_results), 20)
    vault_get = ctx.get("vault_get")

    # Try SerpAPI first if key is available
    serpapi_key = vault_get("serpapi_key") if vault_get else ""
    if serpapi_key:
        result = await asyncio.to_thread(_serpapi_search, query, num_results, serpapi_key)
        if not result.startswith("Error"):
            return result

    # Try Brave Search API
    brave_key = vault_get("brave_search_key") if vault_get else ""
    if brave_key:
        result = await asyncio.to_thread(_brave_search, query, num_results, brave_key)
        if not result.startswith("Error"):
            return result

    # Fallback: DuckDuckGo HTML (no API key needed)
    return await asyncio.to_thread(_ddg_search, query, num_results)


def _serpapi_search(query: str, num: int, api_key: str) -> str:
    import requests
    try:
        r = requests.get(
            "https://serpapi.com/search",
            params={"q": query, "api_key": api_key, "num": num},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        results = data.get("organic_results", [])
        return _format_results(results[:num], "title", "link", "snippet")
    except Exception as e:
        return f"Error (SerpAPI): {e}"


def _brave_search(query: str, num: int, api_key: str) -> str:
    import requests
    try:
        r = requests.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers={"Accept": "application/json", "X-Subscription-Token": api_key},
            params={"q": query, "count": num},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        results = data.get("web", {}).get("results", [])
        return _format_results(results[:num], "title", "url", "description")
    except Exception as e:
        return f"Error (Brave): {e}"


def _ddg_search(query: str, num: int) -> str:
    import requests
    from bs4 import BeautifulSoup
    import urllib.parse

    try:
        encoded = urllib.parse.quote_plus(query)
        url = f"https://html.duckduckgo.com/html/?q={encoded}"
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        }
        r = requests.get(url, headers=headers, timeout=10)
        r.raise_for_status()

        soup = BeautifulSoup(r.text, "lxml")
        items = soup.find_all("div", class_="result__body")[:num]

        if not items:
            return f"No results found for: {query}"

        lines = [f"Search results for: {query}\n"]
        for i, item in enumerate(items, 1):
            title_el = item.find("a", class_="result__a")
            snippet_el = item.find("a", class_="result__snippet")
            title = title_el.get_text(strip=True) if title_el else "No title"
            href = title_el.get("href", "") if title_el else ""
            # DuckDuckGo wraps URLs — try to extract real URL
            if "uddg=" in href:
                import urllib.parse as up
                qs = up.parse_qs(up.urlparse(href).query)
                href = qs.get("uddg", [href])[0]
            snippet = snippet_el.get_text(strip=True) if snippet_el else ""
            lines.append(f"{i}. {title}\n   {href}\n   {snippet}")

        return "\n".join(lines)
    except Exception as e:
        return f"Error (DuckDuckGo): {e}"


def _format_results(results: list, title_key: str, url_key: str, snippet_key: str) -> str:
    if not results:
        return "No results found."
    lines = []
    for i, r in enumerate(results, 1):
        title = r.get(title_key, "No title")
        url = r.get(url_key, "")
        snippet = r.get(snippet_key, "")
        lines.append(f"{i}. {title}\n   {url}\n   {snippet}")
    return "\n".join(lines)
