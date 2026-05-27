import httpx
from bs4 import BeautifulSoup
import json
import os
import asyncio
from datetime import datetime
import ollama

# Configuration
NEWS_URL = "https://www.aljazeera.com/xml/rss/all.xml"
STORAGE_DIR = "storage"
NEWS_FILE = os.path.join(STORAGE_DIR, "latest_news.json")
MODEL = "qwen2.5:0.5b" # Using the fast model we verified

# Telegram Config
TELEGRAM_TOKEN = "8550539119:AAHMyO_9KSzqxMGbv31uDof63-IZstDgEk4"
TELEGRAM_CHAT_ID = "8058658801"

async def fetch_aljazeera_news():
    print(f"[{datetime.now()}] Fetching news from Al Jazeera RSS...")
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            response = await client.get(NEWS_URL, timeout=15.0)
            response.raise_for_status()
            
        soup = BeautifulSoup(response.text, 'xml')
        items = soup.find_all('item', limit=5)
        
        news_items = []
        for item in items:
            title = item.find('title')
            description = item.find('description')
            
            if title:
                news_items.append({
                    "title": title.get_text().strip(),
                    "summary": description.get_text().strip() if description else "No summary available",
                    "timestamp": datetime.now().isoformat()
                })
        return news_items
    except Exception as e:
        print(f"Error fetching news: {e}")
        return []

async def analyze_impact(news_item):
    prompt = f"""
Analyze the following news headline and summary for Market Impact and Sentiment.
News: {news_item['title']}
Summary: {news_item['summary']}

Format your response exactly like this:
Impact: [Low/Medium/High] - [Affected sectors, e.g., Oil, Tech, Gold]
Sentiment: [Bullish/Bearish/Neutral]
Analysis: [One sentence explanation]
"""
    try:
        response = await asyncio.to_thread(
            ollama.generate, 
            model=MODEL, 
            prompt=prompt
        )
        return response['response'].strip()
    except Exception as e:
        return f"Analysis failed: {e}"

async def send_telegram_notification(news_items):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram token or chat ID missing.")
        return

    print(f"[{datetime.now()}] Sending Telegram notifications...")
    message = "<b>🌍 GLOBAL INTELLIGENCE UPDATE</b>\n\n"
    for item in news_items:
        impact_line = item['analysis'].split('\n')[0]
        message += f"• <b>{item['title']}</b>\n{impact_line}\n\n"
    
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    
    try:
        async with httpx.AsyncClient() as client:
            await client.post(url, json=payload)
    except Exception as e:
        print(f"Failed to send Telegram notification: {e}")

async def main():
    if not os.path.exists(STORAGE_DIR):
        os.makedirs(STORAGE_DIR)
        
    news = await fetch_aljazeera_news()
    if not news:
        print("No news found.")
        return

    analyzed_news = []
    for item in news:
        print(f"Analyzing: {item['title']}...")
        item['analysis'] = await analyze_impact(item)
        analyzed_news.append(item)

    with open(NEWS_FILE, "w") as f:
        json.dump(analyzed_news, f, indent=2)
    
    print(f"Successfully saved {len(analyzed_news)} news items to {NEWS_FILE}")
    
    # Send notification
    await send_telegram_notification(analyzed_news)

if __name__ == "__main__":
    asyncio.run(main())
