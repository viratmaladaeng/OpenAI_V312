import os
from fastapi import FastAPI, Request, HTTPException
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, QuickReply, QuickReplyButton, MessageAction
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

# ตั้งค่า Line Messaging API
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

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

    if user_message == "เริ่มการสนทนาใหม่":
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

        # เรียกใช้งาน OpenAI API ผ่าน client SDK
        completion = client.chat.completions.create(
            model=AZURE_DEPLOYMENT_NAME,
            messages=[
                {"role": "system", "content": system_message},
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
                        "role_information": system_message,  # ใช้ system_message จากไฟล์
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

         # สร้างปุ่ม Quick Reply
        quick_reply_buttons = QuickReply(items=[
            QuickReplyButton(action=MessageAction(label="🔄 เริ่มใหม่", text="เริ่มการสนทนาใหม่")),
            QuickReplyButton(action=MessageAction(label="🔍 ค้นหาสินค้า", text="ค้นหาสินค้าใหม่")),
            QuickReplyButton(action=MessageAction(label="📞 ติดต่อเจ้าหน้าที่", text="ติดต่อเจ้าหน้าที่"))
        ])
    
    
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
