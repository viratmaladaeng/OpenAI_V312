import os
from fastapi import FastAPI, Request, HTTPException
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, QuickReply, QuickReplyButton, MessageAction, FollowEvent
import openai
from azure.search.documents import SearchClient
from azure.core.credentials import AzureKeyCredential
from dotenv import load_dotenv

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
AZURE_DEPLOYMENT_NAME = os.getenv("AZURE_DEPLOYMENT_NAME")


# ตรวจสอบว่าค่าถูกตั้งไว้
if not all([
    LINE_CHANNEL_SECRET, LINE_CHANNEL_ACCESS_TOKEN, 
    AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY,
    AZURE_SEARCH_ENDPOINT, AZURE_SEARCH_KEY, AZURE_SEARCH_INDEX, AZURE_DEPLOYMENT_NAME
]):
    raise ValueError("Environment variables not set properly")

# ตั้งค่า OpenAI Client
client = openai.AzureOpenAI(
    api_key=AZURE_OPENAI_API_KEY,
    api_version="2024-05-01-preview",
    azure_endpoint=AZURE_OPENAI_ENDPOINT
)

# ฟังก์ชันอ่านไฟล์ข้อความ
def read_file(filename):
    if os.path.exists(filename):
        with open(filename, "r", encoding="utf-8") as file:
            return file.read().strip()
    return ""

# โหลดข้อความจาก system.txt และ grounding.txt
system_message = read_file("system.txt")
grounding_text = read_file("grounding.txt")

# Session Memory (เก็บค่า SKU ล่าสุดของแต่ละผู้ใช้)
session_memory = {}

def update_sku_code(user_id, sku_code):
    """ อัปเดต SKU ล่าสุดของผู้ใช้ """
    session_memory[user_id] = sku_code

def get_last_sku_code(user_id):
    """ ดึง SKU ล่าสุดของผู้ใช้ ถ้าไม่มี ให้คืนค่าเป็น None """
    return session_memory.get(user_id, None)

def extract_sku_from_text(text):
    """ ดึง sku_code จากข้อความที่ค้นหา """
    lines = text.split("\n")
    for line in lines:
        if "sku_code" in line:
            return line.split(":")[1].strip()
    return None

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

@handler.add(FollowEvent)
def handle_follow(event):
    """ ตอบกลับเมื่อผู้ใช้เพิ่ม Bot ใหม่ หลังจากลบการสนทนา """
    welcome_message = (
        "ขอบคุณที่เพิ่มเราเป็นเพื่อนอีกครั้ง! 😊\n"
        "หากต้องการสอบถามข้อมูลหรือเริ่มต้นสนทนาใหม่ พิมพ์ 'เริ่มการสนทนาใหม่' ได้เลยค่ะ"
    )

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=welcome_message)
    )


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    user_message = event.message.text

    if user_message == "เริ่มการสนทนาใหม่":
        session_memory.pop(user_id, None)
        reply_message = "รบกวนคุณลูกค้าแจ้งว่าต้องการทราบข้อมูลสินค้า หรือบริการใดเพิ่มเติมค่ะ"
    else:
        search_results = search_documents(user_message)

        if not search_results or "Error" in search_results[0]:
            if "สินค้า" in user_message:
                grounding_message = "ไม่พบข้อมูลสินค้า กรุณาลองใหม่ หรือแจ้งชื่อสินค้าพร้อมรหัส"
            elif "ปัญหา" in user_message or "แก้ไข" in user_message:
                grounding_message = "ยังไม่มีข้อมูลวิธีแก้ไข กรุณาติดต่อเจ้าหน้าที่ที่ศูนย์บริการลูกค้า"
            else:
                grounding_message = "ไม่พบข้อมูลที่เกี่ยวข้องค่ะ"
        else:
            grounding_message = "\n\n".join(search_results)
            found_sku_code = extract_sku_from_text(grounding_message)
            if found_sku_code:
                update_sku_code(user_id, found_sku_code)

        last_sku_code = get_last_sku_code(user_id)
        sku_context = f"\n\nข้อมูลสินค้า SKU ล่าสุดที่ค้นหา: {last_sku_code}" if last_sku_code else ""

        # เรียกใช้งาน OpenAI API ผ่าน client SDK
        completion = client.chat.completions.create(
        model=AZURE_DEPLOYMENT_NAME,
        messages=[
            {"role": "system", "content": system_message + sku_context},  # ใช้ system_message ที่อ่านจากไฟล์
            {"role": "user", "content": user_message},
            {"role": "assistant", "content": grounding_message}
        ],
        max_tokens=800,
        temperature=0.7,
        top_p=0.95,
        frequency_penalty=0,
        presence_penalty=0,
        stop=None,
        stream=False,
        extra_body={
            "data_sources": [{
                "type": "azure_search",
                "parameters": {
                    "endpoint": AZURE_SEARCH_ENDPOINT,
                    "index_name": AZURE_SEARCH_INDEX,
                    "semantic_configuration": f"{AZURE_SEARCH_INDEX}-semantic-configuration",
                    "query_type": "semantic",
                    "fields_mapping": {},
                    "in_scope": True,
                    "role_information": system_message,  # เพิ่ม system_message ที่อ่านมาจากไฟล์
                    "filter": None,
                    "strictness": 3,
                    "top_n_documents": 5,
                    "authentication": {
                        "type": "api_key",
                        "key": AZURE_SEARCH_KEY
                    }
                }
            }]
        }
    )

        reply_message = completion.choices[0].message.content if completion else "ขออภัย ระบบมีปัญหาในการเชื่อมต่อกับ Azure OpenAI"

    # ส่งข้อความกลับไปยัง Line
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_message)
    )


def search_documents(query, top=5):
    """ ค้นหาข้อมูลจาก Azure Cognitive Search """
    try:
        search_client = SearchClient(
            endpoint=AZURE_SEARCH_ENDPOINT,
            index_name=AZURE_SEARCH_INDEX,
            credential=AzureKeyCredential(AZURE_SEARCH_KEY)
        )
        results = search_client.search(search_text=query, top=top)
        
        documents = []
        for result in results:
            document_type = result.get("document_type", "Unknown")
            title = result.get("title", "No Title")
            chunk = result.get("chunk", "No Content")
            
            if document_type == "Product":
                documents.append(f"🔹 [สินค้า] {title}\n{chunk}")
            elif document_type == "HelpDesk":
                documents.append(f"❓ [คำถามที่พบบ่อย] {title}\n{chunk}")
            else:
                documents.append(f"{title}\n{chunk}")
        
        return documents if documents else ["ไม่พบข้อมูลที่เกี่ยวข้องค่ะ"]
    
    except Exception as e:
        return [f"ขออภัย ไม่สามารถเรียกข้อมูลได้ค่ะ: {e}"]

    
    
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)


