import os
from fastapi import FastAPI, Request, HTTPException
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, QuickReply, QuickReplyButton, MessageAction, FollowEvent, StickerSendMessage, ImageSendMessage
import requests
from dotenv import load_dotenv
import openai
from azure.search.documents import SearchClient
from azure.core.credentials import AzureKeyCredential
from collections import deque  # ใช้ deque สำหรับเก็บประวัติข้อความแบบ FIFO

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
AZURE_OAI_DEPLOYMENT = os.getenv("AZURE_OAI_DEPLOYMENT")

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
openai.api_version = "2024-05-01-preview"

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

# Session Memory สำหรับเก็บประวัติการสนทนา (จำ 5 ข้อความล่าสุดของแต่ละ user)
session_memory = {}

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
    user_id = event.source.user_id
    user_message = event.message.text

    # ถ้าเป็นการเริ่มต้นใหม่ ให้ล้าง session
    if user_message == "เริ่มการสนทนาใหม่":
        session_memory[user_id] = deque(maxlen=5)  # รีเซ็ต session
        reply_message = "🔄 คุณสามารถสอบถามข้อมูลสินค้าได้เลยค่ะ"
    else:
        # ดึงข้อมูลจาก Azure Search
        search_results = search_documents(user_message)

        # กำหนดข้อความพื้นฐาน
        grounding_message = "\n\n".join(search_results) if search_results else "ไม่พบข้อมูลที่เกี่ยวข้องค่ะ"

        # เพิ่มข้อมูลที่ผู้ใช้คุยก่อนหน้าเข้าไปใน Context
        if user_id not in session_memory:
            session_memory[user_id] = deque(maxlen=5)  # เก็บได้ 5 ประโยคล่าสุด

        session_memory[user_id].append({"role": "user", "content": user_message})
        session_memory[user_id].append({"role": "assistant", "content": grounding_message})

        # สร้างข้อความเพื่อส่งไปยัง Azure OpenAI
        messages = [{"role": "system", "content": "คุณเป็นผู้ช่วยที่สามารถตอบคำถามเกี่ยวกับสินค้าได้"}]
        messages.extend(session_memory[user_id])  # ใส่ประวัติการสนทนาเข้าไป
        messages.append({"role": "user", "content": user_message})  # ข้อความล่าสุด

        response = openai.ChatCompletion.create(
            model=AZURE_OAI_DEPLOYMENT,
            messages=messages,
            max_tokens=700,
            temperature=0.4,
            top_p=0.7,
            frequency_penalty=0.0,
            presence_penalty=0.0
        )


        headers = {"Content-Type": "application/json", "api-key": AZURE_OPENAI_API_KEY}
        response = requests.post(AZURE_OPENAI_ENDPOINT, headers=headers, json=payload)

        if response.status_code == 200:
            openai_response = response.json()
            #reply_message = openai_response["choices"][0]["message"]["content"]
            reply_message = response["choices"][0]["message"]["content"]

        else:
            reply_message = "ขออภัย ระบบมีปัญหาในการเชื่อมต่อกับ Azure OpenAI"

    # สร้าง Quick Reply
    quick_reply_buttons = QuickReply(items=[
        QuickReplyButton(action=MessageAction(label="🔄 เริ่มใหม่", text="เริ่มการสนทนาใหม่")),
        QuickReplyButton(action=MessageAction(label="🔍 ค้นหาสินค้า", text="ค้นหาสินค้าใหม่")),
        QuickReplyButton(action=MessageAction(label="📞 ติดต่อเจ้าหน้าที่", text="ติดต่อเจ้าหน้าที่"))
    ])

    # ส่งข้อความกลับไปยัง Line
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_message, quick_reply=quick_reply_buttons))
    

def search_documents(query, top=5):
    """ ค้นหาข้อมูลจาก Azure Cognitive Search """
    try:
        search_client = SearchClient(
            endpoint=AZURE_SEARCH_ENDPOINT,
            index_name=AZURE_SEARCH_INDEX,
            credential=AzureKeyCredential(AZURE_SEARCH_KEY)
        )
        results = search_client.search(search_text=query, top=top)

        documents = [f"🔹 [สินค้า] {r.get('title', 'No Title')}\n{r.get('chunk', 'No Content')}" for r in results]
        return documents if documents else ["ไม่พบข้อมูลที่เกี่ยวข้องค่ะ"]
    
    except Exception as e:
        return [f"ขออภัย ไม่สามารถเรียกข้อมูลได้ค่ะ: {e}"]

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
