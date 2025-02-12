import os
from fastapi import FastAPI, Request, HTTPException
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import requests
from dotenv import load_dotenv
import openai
from azure.search.documents import SearchClient
from azure.core.credentials import AzureKeyCredential

load_dotenv()
# สร้าง FastAPI instance
app = FastAPI()

# ดึงค่าจาก Environment Variables
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY")
AZURE_SEARCH_ENDPOINT = os.getenv("AZURE_SEARCH_ENDPOINT")
AZURE_SEARCH_KEY = os.getenv("AZURE_SEARCH_KEY")
AZURE_SEARCH_INDEX = os.getenv("AZURE_SEARCH_INDEX")

# ตรวจสอบว่าค่าถูกตั้งไว้
if not all([
    LINE_CHANNEL_SECRET, LINE_CHANNEL_ACCESS_TOKEN, 
    AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY,
    AZURE_SEARCH_ENDPOINT, AZURE_SEARCH_KEY, AZURE_SEARCH_INDEX
]):
    raise ValueError("Environment variables not set properly")

# Initialize Azure OpenAI
openai.api_type = "azure"
openai.api_base = AZURE_OPENAI_ENDPOINT
openai.api_key = AZURE_OPENAI_API_KEY
openai.api_version = "2024-08-01-preview"


# ตั้งค่า Line Messaging API
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

@app.get("/")
async def read_root():
    return {"message": "Hello, world!"}

@app.post("/callback")
async def callback(request: Request):
    # รับ webhook จาก Line
    signature = request.headers["X-Line-Signature"]
    body = await request.body()
    
    try:
        handler.handle(body.decode("utf-8"), signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    return "OK"

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_message = event.message.text

    # ค้นหาเอกสารจาก Azure Cognitive Search
    search_results = search_documents(user_message)

    # หากไม่มีผลลัพธ์ ให้ตอบกลับว่าหาไม่พบ
    if not search_results or "Error" in search_results[0]:
        grounding_message = "No relevant documents found for the query."
    else:
        grounding_message = "\n\n".join(search_results)

    # ส่งข้อความไปยัง Azure OpenAI
    headers = {
        "Content-Type": "application/json",
        "api-key": AZURE_OPENAI_API_KEY
    }
    payload = {
            "messages": [
                {
                    "role": "system",
                    "content": grounding_message
                },
                {
                    "role": "user",
                    "content": user_message
                }
            ],
            "max_tokens": 800,
            "temperature": 0.2
        }
    
    response = requests.post(AZURE_OPENAI_ENDPOINT, headers=headers, json=payload)
    
    if response.status_code == 200:
        openai_response = response.json()
        bot_reply = openai_response["choices"][0]["message"]["content"]
    else:
        bot_reply = "ขออภัย ระบบมีปัญหาในการเชื่อมต่อกับ Azure OpenAI"

    # ส่งข้อความกลับไปยัง Line
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=bot_reply)
    )

def search_documents(query, top=5):
    """Search for relevant documents in Azure Cognitive Search."""
    try:
        print(f"Querying Azure Search with: {query}")
        search_client = SearchClient(
            endpoint=AZURE_SEARCH_ENDPOINT,
            index_name=AZURE_SEARCH_INDEX,
            credential=AzureKeyCredential(AZURE_SEARCH_KEY)
        )
        results = search_client.search(search_text=query, top=top)
        
        # รวม title และ chunk
        documents = []
        for result in results:
            title = result.get("title", "No Title")
            chunk = result.get("chunk", "No Content")
            documents.append(f"Title: {title}\nContent: {chunk}")
        
        # Log results
        print(f"Documents fetched: {documents}")
        
        return documents if documents else ["No relevant documents found."]
    except Exception as e:
        print(f"Error occurred during Azure Search: {e}")
        return ["Error: Unable to retrieve documents."]
    
#if __name__ == "__main__":
    #import uvicorn
    #uvicorn.run(app, host="0.0.0.0", port=8000,"h11-max-incomplete-event-size 16384")