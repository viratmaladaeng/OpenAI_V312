import os
from fastapi import FastAPI, Request, HTTPException
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, QuickReply, QuickReplyButton, MessageAction, FollowEvent
import requests
from dotenv import load_dotenv
import openai
from azure.search.documents import SearchClient
from azure.core.credentials import AzureKeyCredential
from openai import AzureOpenAI  

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
    AZURE_SEARCH_ENDPOINT, AZURE_SEARCH_KEY, AZURE_SEARCH_INDEX,AZURE_OAI_DEPLOYMENT
]):
    raise ValueError("Environment variables not set properly")

"""
# Initialize Azure OpenAI
openai.api_type = "azure"
openai.api_base = AZURE_OPENAI_ENDPOINT
openai.api_key = AZURE_OPENAI_API_KEY
openai.api_version = "2024-08-01-preview"
"""

# Initialize Azure OpenAI Service client with key-based authentication    
client = AzureOpenAI(  
    azure_endpoint=AZURE_OPENAI_ENDPOINT,  
    api_key=AZURE_OPENAI_API_KEY,  
    api_version="2024-08-01-preview",
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
    user_message = event.message.text

    if user_message == "เริ่มการสนทนาใหม่":
        reply_message = "💬 ยินดีต้อนรับลูกค้า แจ้งว่าต้องการทราบข้อมูลสินค้า หรือบริการใดเพิ่มเติมค่ะ"
    else:
        chat_prompt = [
            {"role": "system", "content": [{"type": "text", "text": system_message}]},
            {"role": "user", "content": [{"type": "text", "text": user_message}]}
        ]

        messages = chat_prompt
        completion = client.chat.completions.create(
            model=AZURE_OAI_DEPLOYMENT,
            messages=messages,
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
                        "index_name": "vector-1740486054179",
                        "semantic_configuration": "vector-1740486054179-semantic-configuration",
                        "query_type": "vector_semantic_hybrid",
                        "fields_mapping": {},
                        "in_scope": True,
                        "role_information": "You are an AI assistant that helps people find information.",
                        "filter": None,
                        "strictness": 3,
                        "top_n_documents": 5,
                        "authentication": {
                            "type": "api_key",
                            "key": AZURE_SEARCH_KEY
                        }
                    }
                }],
                "embedding_dependency": {
                    "type": "gpt-4o-mini",
                    "deployment_name": "text-embedding-ada-002 680208"
                }
            }
        )

        if hasattr(completion, "choices") and len(completion.choices) > 0:
            reply_message = completion.choices[0].message.content
        else:
            reply_message = "ขออภัย ระบบมีปัญหาในการเชื่อมต่อกับ Azure OpenAI"

    quick_reply_buttons = QuickReply(items=[
        QuickReplyButton(action=MessageAction(label="🔄 เริ่มใหม่", text="เริ่มการสนทนาใหม่")),
        QuickReplyButton(action=MessageAction(label="🔍 ค้นหาสินค้า", text="ค้นหาสินค้าใหม่")),
        QuickReplyButton(action=MessageAction(label="📞 ติดต่อเจ้าหน้าที่", text="ติดต่อเจ้าหน้าที่"))
    ])

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
