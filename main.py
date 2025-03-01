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

env_vars = ["LINE_CHANNEL_SECRET", "LINE_CHANNEL_ACCESS_TOKEN", 
            "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY",
            "AZURE_SEARCH_ENDPOINT", "AZURE_SEARCH_KEY", "AZURE_SEARCH_INDEX", "AZURE_DEPLOYMENT_NAME"]

config = {var: os.getenv(var) for var in env_vars}
if None in config.values():
    raise ValueError("Environment variables not set properly")


# ตั้งค่า Line Messaging API
line_bot_api = LineBotApi(config["LINE_CHANNEL_ACCESS_TOKEN"])
handler = WebhookHandler(config["LINE_CHANNEL_SECRET"])

# ตั้งค่า OpenAI Client
client = openai.AzureOpenAI(
    api_key=config["AZURE_OPENAI_API_KEY"],
    api_version="2024-05-01-preview",
    azure_endpoint=config["AZURE_OPENAI_ENDPOINT"]
)

# โหลด system message
def read_file(filename):
    return open(filename, "r", encoding="utf-8").read().strip() if os.path.exists(filename) else ""

system_message = read_file("system.txt")


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
    user_message = event.message.text

    grounding_message = search_documents(user_message)
    completion = client.chat.completions.create(
        model=config["AZURE_DEPLOYMENT_NAME"],
        messages=[
            {"role": "system", "content": system_message},
            {"role": "user", "content": user_message},
            {"role": "assistant", "content": grounding_message}
        ],
        max_tokens=800, temperature=0.7, top_p=0.95,
        frequency_penalty=0, presence_penalty=0,
        extra_body={
            "data_sources": [{
                "type": "azure_search",
                "parameters": {
                    "endpoint": config["AZURE_SEARCH_ENDPOINT"],
                    "index_name": config["AZURE_SEARCH_INDEX"],
                    "semantic_configuration": f"{config['AZURE_SEARCH_INDEX']}-semantic-configuration",
                    "query_type": "semantic",
                    "strictness": 3,
                    "top_n_documents": 5,
                    "authentication": {
                        "type": "api_key",
                        "key": config["AZURE_SEARCH_KEY"]
                    }
                }
            }]
        }
    )

    reply_message = completion.choices[0].message.content if completion else "ขออภัย ระบบมีปัญหาในการเชื่อมต่อ"
    quick_reply_buttons = QuickReply(items=[
        QuickReplyButton(action=MessageAction(label="🔄 เริ่มใหม่", text="เริ่มการสนทนาใหม่")),
        QuickReplyButton(action=MessageAction(label="🔍 ค้นหาสินค้า", text="ค้นหาสินค้าใหม่")),
        QuickReplyButton(action=MessageAction(label="📞 ติดต่อเจ้าหน้าที่", text="ติดต่อเจ้าหน้าที่"))
    ])

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_message, quick_reply=quick_reply_buttons))

def search_documents(query, top=5):
    try:
        search_client = SearchClient(
            endpoint=config["AZURE_SEARCH_ENDPOINT"],
            index_name=config["AZURE_SEARCH_INDEX"],
            credential=AzureKeyCredential(config["AZURE_SEARCH_KEY"])
        )
        results = search_client.search(search_text=query, top=top)
        documents = [f"🔹 [สินค้า] {r.get('title', 'No Title')}\n{r.get('chunk', 'No Content')}" for r in results]
        return "\n\n".join(documents) if documents else "ไม่พบข้อมูลที่เกี่ยวข้องค่ะ"
    except Exception as e:
        return f"ขออภัย ไม่สามารถเรียกข้อมูลได้ค่ะ: {e}"

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)