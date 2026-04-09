import os
import json
import base64
import io
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, ImageMessage, TextSendMessage

# 初始化 Flask 應用
app = Flask(__name__)

# LINE API 設定
CHANNEL_SECRET = os.getenv('LINE_CHANNEL_SECRET', '')
CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN', '')

# 初始化 LINE Bot
line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# Gemini API Key
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY', '')

# 延遲初始化 Gemini Client（避免啟動時因 API Key 問題崩潰）
_gemini_client = None

def get_gemini_client():
    """取得 Gemini Client（延遲初始化）"""
    global _gemini_client
    if _gemini_client is None:
        from google import genai
        _gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    return _gemini_client

# 載入備品資料
def load_spare_parts_data():
    """載入備品資料 JSON 檔案"""
    try:
        with open('spare_parts_data.json', 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        print("警告：找不到 spare_parts_data.json 檔案")
        return []

# 全域備品資料
SPARE_PARTS = load_spare_parts_data()

def format_spare_parts_for_prompt():
    """將備品資料格式化為提示詞"""
    formatted = "以下是備品資料庫中的所有備品：\n\n"
    for part in SPARE_PARTS:
        formatted += f"- 料號：{part.get('part_number', '')}\n"
        formatted += f"  規格：{part.get('specification', '')}\n"
        formatted += f"  倉庫位置：{part.get('warehouse_location', '')}\n"
        formatted += f"  大分類儲位：{part.get('major_category', '')}\n"
        formatted += f"  小分類儲位：{part.get('minor_category', '')}\n\n"
    return formatted

def query_spare_parts_text(user_query):
    """使用 Gemini 進行文字查詢"""
    from google.genai import types

    spare_parts_info = format_spare_parts_for_prompt()

    system_prompt = """你是一個備品查詢助手。你的職責是幫助使用者查詢備品的位置資訊。

備品資料庫規則：
1. 使用者可能會輸入料號或規格關鍵字來查詢備品
2. 如果查詢到相符的備品，請回傳：倉庫位置、大分類儲位、小分類儲位
3. 如果查無此備品資料，回答：「查無此備品資料，請確認料號或規格是否正確」
4. 如果使用者的問題超出備品查詢範圍，回答：「我只能回答備品位置相關的查詢，其他問題無法回答」
5. 回答要簡潔清楚，適合在 LINE 上閱讀

""" + spare_parts_info

    try:
        client = get_gemini_client()
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=system_prompt + "\n\n使用者查詢：" + user_query
        )
        return response.text
    except Exception as e:
        print(f"Gemini API 錯誤：{str(e)}")
        return "抱歉，查詢過程中發生錯誤，請稍後再試"

def extract_text_from_image(image_data):
    """使用 Gemini Vision 從圖片中提取文字"""
    from google.genai import types

    try:
        client = get_gemini_client()
        image_part = types.Part.from_bytes(data=image_data, mime_type='image/jpeg')

        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[
                "請從這張圖片中提取所有可見的文字，特別是料號、規格、型號等備品相關資訊。只需要回傳提取到的文字，不需要其他說明。",
                image_part
            ]
        )

        extracted_text = response.text
        print(f"從圖片提取的文字：{extracted_text}")
        return extracted_text
    except Exception as e:
        print(f"圖片處理錯誤：{str(e)}")
        return None

def query_spare_parts_from_image(image_data):
    """從圖片中提取資訊並查詢備品"""

    extracted_text = extract_text_from_image(image_data)

    if not extracted_text:
        return "無法辨識照片中的備品資訊，請確認照片清晰度或改用文字輸入料號"

    spare_parts_info = format_spare_parts_for_prompt()

    system_prompt = """你是一個備品查詢助手。根據從圖片中提取的文字，查詢備品資訊。

備品資料庫規則：
1. 根據提取的文字中的料號或規格來查詢備品
2. 如果查詢到相符的備品，請回傳：倉庫位置、大分類儲位、小分類儲位
3. 如果查無此備品資料，回答：「查無此備品資料，請確認照片中的料號或規格是否正確」
4. 如果無法從照片中辨識出有效的備品資訊，回答：「無法辨識照片中的備品資訊，請確認照片清晰度或改用文字輸入料號」
5. 回答要簡潔清楚，適合在 LINE 上閱讀

""" + spare_parts_info

    try:
        client = get_gemini_client()
        query_prompt = system_prompt + f"\n\n從圖片提取的文字如下：\n{extracted_text}\n\n請根據這些文字查詢對應的備品資訊。"
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=query_prompt
        )
        return response.text
    except Exception as e:
        print(f"Gemini API 錯誤：{str(e)}")
        return "抱歉，查詢過程中發生錯誤，請稍後再試"

@app.route("/callback", methods=['POST'])
def callback():
    """LINE Webhook 回調處理"""

    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        print("簽名驗證失敗")
        abort(400)
    except Exception as e:
        print(f"錯誤：{str(e)}")
        abort(500)

    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    """處理文字訊息"""
    user_message = event.message.text.strip()
    response_text = query_spare_parts_text(user_message)
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=response_text)
    )

@handler.add(MessageEvent, message=ImageMessage)
def handle_image_message(event):
    """處理圖片訊息"""
    try:
        message_content = line_bot_api.get_message_content(event.message.id)
        image_data = message_content.content
        response_text = query_spare_parts_from_image(image_data)
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=response_text)
        )
    except Exception as e:
        print(f"圖片處理錯誤：{str(e)}")
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="無法辨識照片中的備品資訊，請確認照片清晰度或改用文字輸入料號")
        )

@app.route("/health", methods=['GET'])
def health_check():
    """健康檢查端點"""
    return {'status': 'healthy'}, 200

@app.route("/", methods=['GET'])
def index():
    """根路由"""
    return {'message': 'LINE 備品查詢機器人已啟動'}, 200

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
