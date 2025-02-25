import os
from fastapi import FastAPI, Request, HTTPException
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, QuickReply, QuickReplyButton, MessageAction
import requests
from dotenv import load_dotenv
import openai
from azure.search.documents import SearchClient
from azure.core.credentials import AzureKeyCredential
import uuid

load_dotenv()

# 🔹 สร้าง FastAPI instance
app = FastAPI()

# 🔹 ดึงค่าจาก Environment Variables
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY")
AZURE_SEARCH_ENDPOINT = os.getenv("AZURE_SEARCH_ENDPOINT")
AZURE_SEARCH_KEY = os.getenv("AZURE_SEARCH_KEY")
AZURE_SEARCH_INDEX_1 = os.getenv("AZURE_SEARCH_INDEX")   # ✅ Index เดิม (vector-xxxx)
AZURE_SEARCH_INDEX_2 = os.getenv("AZURE_SEARCH_INDEX_2") # ✅ Index ใหม่ (chat-history)

# 🔹 ตรวจสอบว่าค่าถูกต้อง
if not all([
    LINE_CHANNEL_SECRET, LINE_CHANNEL_ACCESS_TOKEN, 
    AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY,
    AZURE_SEARCH_ENDPOINT, AZURE_SEARCH_KEY, AZURE_SEARCH_INDEX_1, AZURE_SEARCH_INDEX_2
]):
    raise ValueError("Environment variables not set properly")

# 🔹 Initialize Azure OpenAI
openai.api_type = "azure"
openai.api_base = AZURE_OPENAI_ENDPOINT
openai.api_key = AZURE_OPENAI_API_KEY
openai.api_version = "2024-08-01-preview"

# 🔹 ฟังก์ชันอ่านไฟล์ข้อความ
def read_file(filename):
    if os.path.exists(filename):
        with open(filename, "r", encoding="utf-8") as file:
            return file.read().strip()
    return ""

# 🔹 โหลดข้อความจาก system.txt และ grounding.txt
system_message = read_file("system.txt")
grounding_text = read_file("grounding.txt")

# 🔹 ตั้งค่า Line Messaging API
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
    user_message = event.message.text.strip()
    user_id = event.source.user_id  # 🔹 ใช้ User ID เพื่อจัดการประวัติการสนทนา

    if user_message == "เริ่มการสนทนาใหม่":
        reply_message = "รบกวนคุณลูกค้าแจ้งว่าต้องการทราบข้อมูลสินค้า หรือบริการใดเพิ่มเติมค่ะ"
    else:
        # 🔹 ค้นหาข้อมูลจากทั้งสอง Index
        search_results = search_documents(user_message)

        # 🔹 หากไม่มีผลลัพธ์ ให้ใช้ grounding.txt
        grounding_message = grounding_text if not search_results or "Error" in search_results[0] else "\n\n".join(search_results)

        # 🔹 ส่งข้อความไปยัง Azure OpenAI
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
            "temperature": 0.5,
            "top_p": 0.95,
            "frequency_penalty": 0,
            "presence_penalty": 0,
            "stop":"เริ่มการสนทนาใหม่",
            "stream":False  
        }
        
        response = requests.post(f"{AZURE_OPENAI_ENDPOINT}/chat/completions", headers=headers, json=payload)
        
        if response.status_code == 200:
            openai_response = response.json()
            reply_message = openai_response["choices"][0]["message"]["content"]
        else:
            reply_message = "ขออภัย ระบบมีปัญหาในการเชื่อมต่อกับ Azure OpenAI"

        # 🔹 บันทึกข้อความลง `chat-history` เพื่อให้ AI จดจำได้
        save_to_chat_history(user_id, user_message)

        # 🔹 สร้างปุ่ม Quick Reply
        quick_reply_buttons = QuickReply(items=[
            QuickReplyButton(action=MessageAction(label="เริ่มการสนทนาใหม่", text="เริ่มการสนทนาใหม่"))
        ])

        # 🔹 ส่งข้อความกลับไปยัง Line พร้อม Quick Reply
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply_message, quick_reply=quick_reply_buttons)
        )

def connect_search_client(index_name):
    """ 🔹 ฟังก์ชันเชื่อมต่อ Azure AI Search """
    return SearchClient(
        endpoint=AZURE_SEARCH_ENDPOINT,
        index_name=index_name,
        credential=AzureKeyCredential(AZURE_SEARCH_KEY)
    )

# 🔹 สร้าง Clients สำหรับทั้งสอง Index
search_client_1 = connect_search_client(AZURE_SEARCH_INDEX_1)  # ✅ Index เดิม
search_client_2 = connect_search_client(AZURE_SEARCH_INDEX_2)  # ✅ Index ใหม่ (chat-history)

def search_documents(query, top=5):
    """ 🔹 ค้นหาข้อมูลจากทั้งสอง Index """
    try:
        results_1 = search_client_1.search(search_text=query, top=top)
        results_2 = search_client_2.search(search_text=query, top=top)

        all_results = []
        for result in results_1:
            all_results.append(result["content"])
        for result in results_2:
            all_results.append(result["content"])

        return all_results if all_results else ["ไม่พบข้อมูลที่เกี่ยวข้องค่ะ"]
    except Exception as e:
        print(f"Error occurred during Azure Search: {e}")
        return ["ขออภัย ไม่สามารถเรียกข้อมูลได้ค่ะ"]

def save_to_chat_history(user_id, text):
    """ 🔹 แปลงข้อความเป็นเวกเตอร์และบันทึกลง `chat-history` """
    vector = openai.Embedding.create(
        input=text, 
        model="text-embedding-ada-002_680208"
    )["data"][0]["embedding"]

    document = {
        "id": str(uuid.uuid4()),  
        "user_id": user_id,  
        "content": text,  
        "vector": vector  
    }

    # 🔹 บันทึกลง Index `chat-history`
    search_client_2.upload_documents(documents=[document])
    print("✅ บันทึกข้อมูลลง Azure AI Search (chat-history) สำเร็จ!")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
