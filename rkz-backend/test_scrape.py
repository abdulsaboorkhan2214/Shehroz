import asyncio
from enrichment import scrape_website

async def test():
    url = "https://www.howardair.com"
    result = await scrape_website(url)
    print("TEXT:", result["text"][:500])
    print("OWNER:", result["owner_name"])
    print("SOCIALS:", result["socials"])
    print("ERROR:", result["error"])

asyncio.run(test())
