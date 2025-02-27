import os
from fastapi import FastAPI, Request, HTTPException
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, QuickReply, QuickReplyButton, MessageAction, FollowEvent, JoinEvent
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

@handler.add(JoinEvent)
def handle_join(event):
    reply_token = event.reply_token
    line_bot_api.reply_message(
        reply_token,
        TextSendMessage(text="สวัสดีค่ะ คุณลูกค้ากำลังมองหาสินค้าประเภทใดคะ? (กระเบื้องปูพื้น, กระเบื้องบุผนัง, สุขภัณฑ์ ,สี, ขนาด, ลวดลาย , พื้นที่ใช้งาน ฯลฯ) หรือบริการของเราเรื่องไหนคะ? ยินดีให้บริการอย่างเต็มที่เลยค่ะ 😊")
    )

@handler.add(FollowEvent)
def handle_follow(event):
    user_id = event.source.user_id
    reply_token = event.reply_token
    
    line_bot_api.reply_message(
        reply_token,
        TextSendMessage(text="สวัสดีค่ะ คุณลูกค้ากำลังมองหาสินค้าประเภทใดคะ? (กระเบื้องปูพื้น, กระเบื้องบุผนัง, สุขภัณฑ์ ,สี, ขนาด, ลวดลาย , พื้นที่ใช้งาน ฯลฯ) หรือบริการของเราเรื่องไหนคะ? ยินดีให้บริการอย่างเต็มที่เลยค่ะ 😊")
    )


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_message = event.message.text

    if user_message == "เริ่มการสนทนาใหม่":
        # ตอบกลับข้อความพิเศษ
        reply_message = "รบกวนคุณลูกค้าแจ้งว่าต้องการทราบข้อมูลสินค้า หรือบริการใดเพิ่มเติมค่ะ"
    else:
        # ค้นหาเอกสารจาก Azure Cognitive Search
        search_results = search_documents(user_message)

        # หากไม่มีผลลัพธ์ ให้ใช้ข้อความจาก grounding.txt
        grounding_message = grounding_text if not search_results or "Error" in search_results[0] else "\n\n".join(search_results)

        # ส่งข้อความไปยัง Azure OpenAI
        headers = {
            "Content-Type": "application/json",
            "api-key": AZURE_OPENAI_API_KEY
        }

        payload = {
            "messages": [
                {"role": "system", "content": system_message},
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": grounding_message}
            ],
            "max_tokens": 800,
            "temperature": 0.0,
	        "top_p":0.5,
	        "frequency_penalty":1.5,  
            "presence_penalty":1.5,
	        "stop": ["เริ่มการสนทนาใหม่", "admin", "ผู้ดูแลระบบ","ไม่มีข้อมูลในระบบ"],  # เพิ่มคำที่ต้องการให้ AI หยุดเมื่อพบ
            "stream":False  
        }

        
        response = requests.post(AZURE_OPENAI_ENDPOINT, headers=headers, json=payload)
        
        if response.status_code == 200:
            openai_response = response.json()
            reply_message = openai_response["choices"][0]["message"]["content"]
        else:
            reply_message = "ขออภัย ระบบมีปัญหาในการเชื่อมต่อกับ Azure OpenAI"

        # สร้างปุ่ม Quick Reply
        quick_reply_buttons = QuickReply(items=[
            QuickReplyButton(action=MessageAction(label="เริ่มการสนทนาใหม่", text="เริ่มการสนทนาใหม่"))
        ])

        # ส่งข้อความกลับไปยัง Line พร้อม Quick Reply
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply_message, quick_reply=quick_reply_buttons)
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
        
        return documents if documents else ["ไม่พบข้อมูลที่เกี่ยวข้องค่ะ"]
    except Exception as e:
        print(f"Error occurred during Azure Search: {e}")
        return ["ขออภัย ไม่สามารถเรียกข้อมูลได้ค่ะ"]
    

    
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)




