import os
from fastapi import FastAPI, Request, HTTPException
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import requests
from dotenv import load_dotenv
import openai
import redis
import json
from azure.search.documents import SearchClient
from azure.core.credentials import AzureKeyCredential

# โหลด environment variables
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
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
REDIS_DB = int(os.getenv("REDIS_DB", 0))

# ตรวจสอบ Environment Variables
if not all([
    LINE_CHANNEL_SECRET, LINE_CHANNEL_ACCESS_TOKEN, 
    AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY,
    AZURE_SEARCH_ENDPOINT, AZURE_SEARCH_KEY, AZURE_SEARCH_INDEX
]):
    raise ValueError("Environment variables not set properly")

# ตั้งค่า Redis Cache
redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, decode_responses=True)

# ตั้งค่า Azure OpenAI
openai.api_type = "azure"
openai.api_base = AZURE_OPENAI_ENDPOINT
openai.api_key = AZURE_OPENAI_API_KEY
openai.api_version = "2024-08-01-preview"

# ฟังก์ชันอ่านไฟล์ข้อความ
def read_file(filename):
    if os.path.exists(filename):
        with open(filename, "r", encoding="utf-8") as file:
            return file.read().strip()
    return ""

# โหลดข้อความจาก system.txt และ grounding.txt
system_message = read_file("system.txt")
grounding_text = read_file("grounding.txt")

# ตั้งค่า Line Messaging API
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

@app.get("/")
async def read_root():
    return {"message": "Hello, world!"}

@app.post("/callback")
async def callback(request: Request):
    signature = request.headers["X-Line-Signature"]
    body = await request.body()
    
    try:
        handler.handle(body.decode("utf-8"), signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    return "OK"

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id  # ดึง ID ของผู้ใช้ Line
    user_message = event.message.text

    # ดึง Context เก่าของ User จาก Redis (ถ้ามี)
    chat_history = get_chat_history(user_id)

    # ค้นหาเอกสารจาก Azure Cognitive Search
    search_results = search_documents(user_message)

    # หากไม่มีผลลัพธ์ ให้ใช้ข้อความจาก grounding.txt
    grounding_message = grounding_text if not search_results or "Error" in search_results[0] else "\n\n".join(search_results)

    # เพิ่มข้อความใหม่ใน Context
    chat_history.append({"role": "user", "content": user_message})
    chat_history.append({"role": "assistant", "content": grounding_message})

    # ส่งข้อความไปยัง Azure OpenAI
    try:
        response = openai.ChatCompletion.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": system_message},
                *chat_history  # ส่ง Context เก่าไปให้ AI
            ],
            max_tokens=800,
            temperature=0.5
        )

        bot_reply = response["choices"][0]["message"]["content"]

    except Exception as e:
        print(f"Error calling Azure OpenAI: {e}")
        bot_reply = "ขออภัย ระบบมีปัญหาในการเชื่อมต่อกับ Azure OpenAI"

    # บันทึกข้อความใหม่ลง Redis
    chat_history.append({"role": "assistant", "content": bot_reply})
    save_chat_history(user_id, chat_history)

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
        
        documents = []
        for result in results:
            title = result.get("title", "No Title")
            chunk = result.get("chunk", "No Content")
            documents.append(f"Title: {title}\nContent: {chunk}")
        
        print(f"Documents fetched: {documents}")
        
        return documents if documents else ["No relevant documents found."]
    except Exception as e:
        print(f"Error occurred during Azure Search: {e}")
        return ["Error: Unable to retrieve documents."]

def get_chat_history(user_id):
    """ดึงประวัติแชทของ User จาก Redis"""
    history = redis_client.get(user_id)
    return json.loads(history) if history else []

def save_chat_history(user_id, messages):
    """บันทึกประวัติแชทของ User ลง Redis (Session-Based)"""
    redis_client.set(user_id, json.dumps(messages), ex=1800)  # 30 นาทีหมดอายุ

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
