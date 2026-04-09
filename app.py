import os
import json
import time
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

def call_gemini_with_retry(contents, model='gemini-2.5-flash', max_retries=3):
    """呼叫 Gemini API，遇到速率限制或暫時錯誤時自動重試"""
    client = get_gemini_client()
    last_error = None

    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model=model,
                contents=contents
            )
            return response.text
        except Exception as e:
            last_error = e
            error_str = str(e)
            error_code = ''

            # 解析錯誤碼
            if '429' in error_str:
                error_code = '429'
            elif '503' in error_str:
                error_code = '503'
            elif '500' in error_str:
                error_code = '500'

            if error_code in ('429', '503', '500') and attempt < max_retries - 1:
                # 速率限制：等待時間隨重試次數增加
                wait_time = (attempt + 1) * 5  # 5s, 10s, 15s
                print(f"Gemini API {error_code} 錯誤，{wait_time} 秒後重試（第 {attempt + 1}/{max_retries} 次）")
                time.sleep(wait_time)
            else:
                # 非暫時性錯誤或已達最大重試次數，直接拋出
                print(f"Gemini API 錯誤（嘗試 {attempt + 1}/{max_retries}）: {error_str[:200]}")
                if attempt == max_retries - 1:
                    break
                raise

    raise last_error

# 載入備品資料
def load_spare_parts_data():
    """載入備品資料 JSON 檔案"""
    try:
        with open('spare_parts_data.json', 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        print("警告：找不到 spare_parts_data.json 檔案")
        return []
    except Exception as e:
        print(f"載入備品資料錯誤：{e}")
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
    spare_parts_info = format_spare_parts_for_prompt()

    prompt = f"""你是一個備品查詢助手。你的職責是幫助使用者查詢備品的位置資訊。

備品資料庫規則：
1. 使用者可能會輸入料號或規格關鍵字來查詢備品
2. 如果查詢到相符的備品，請回傳：倉庫位置、大分類儲位、小分類儲位
3. 如果查無此備品資料，回答：「查無此備品資料，請確認料號或規格是否正確」
4. 如果使用者的問題超出備品查詢範圍，回答：「我只能回答備品位置相關的查詢，其他問題無法回答」
5. 回答要簡潔清楚，適合在 LINE 上閱讀

{spare_parts_info}

使用者查詢：{user_query}"""

    try:
        result = call_gemini_with_retry(prompt)
        return result
    except Exception as e:
        error_str = str(e)
        print(f"Gemini 文字查詢最終失敗：{error_str[:200]}")
        if '429' in error_str:
            return "系統目前使用量較高，請稍候約 1 分鐘後再試"
        return "抱歉，查詢過程中發生錯誤，請稍後再試"

def extract_text_from_image(image_bytes, mime_type='image/jpeg'):
    """使用 Gemini Vision 從圖片中提取文字（支援動態 mime_type）"""
    from google.genai import types

    print(f"圖片大小：{len(image_bytes)} bytes，格式：{mime_type}")

    # 確保 mime_type 是支援的格式
    supported_types = ['image/jpeg', 'image/png', 'image/gif', 'image/webp']
    if mime_type not in supported_types:
        mime_type = 'image/jpeg'  # 預設為 JPEG

    image_part = types.Part.from_bytes(data=image_bytes, mime_type=mime_type)

    prompt_text = "請從這張圖片中提取所有可見的文字，特別是料號、規格、型號等備品相關資訊。只需要回傳提取到的文字，不需要其他說明。"

    try:
        result = call_gemini_with_retry([prompt_text, image_part])
        print(f"從圖片提取的文字：{result[:200]}")
        return result
    except Exception as e:
        error_str = str(e)
        print(f"圖片文字提取失敗：{error_str[:200]}")
        return None

def query_spare_parts_from_image(image_bytes, mime_type='image/jpeg'):
    """從圖片中提取資訊並查詢備品"""

    extracted_text = extract_text_from_image(image_bytes, mime_type)

    if not extracted_text:
        return "無法辨識照片中的備品資訊，請確認照片清晰度或改用文字輸入料號"

    spare_parts_info = format_spare_parts_for_prompt()

    prompt = f"""你是一個備品查詢助手。根據從圖片中提取的文字，查詢備品資訊。

備品資料庫規則：
1. 根據提取的文字中的料號或規格來查詢備品
2. 如果查詢到相符的備品，請回傳：倉庫位置、大分類儲位、小分類儲位
3. 如果查無此備品資料，回答：「查無此備品資料，請確認照片中的料號或規格是否正確」
4. 如果無法從照片中辨識出有效的備品資訊，回答：「無法辨識照片中的備品資訊，請確認照片清晰度或改用文字輸入料號」
5. 回答要簡潔清楚，適合在 LINE 上閱讀

{spare_parts_info}

從圖片提取的文字如下：
{extracted_text}

請根據這些文字查詢對應的備品資訊。"""

    try:
        result = call_gemini_with_retry(prompt)
        return result
    except Exception as e:
        error_str = str(e)
        print(f"備品查詢最終失敗：{error_str[:200]}")
        if '429' in error_str:
            return "系統目前使用量較高，請稍候約 1 分鐘後再試"
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
        print(f"Webhook 處理錯誤：{str(e)}")
        abort(500)

    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    """處理文字訊息"""
    user_message = event.message.text.strip()
    print(f"收到文字訊息：{user_message}")
    response_text = query_spare_parts_text(user_message)
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=response_text)
    )

@handler.add(MessageEvent, message=ImageMessage)
def handle_image_message(event):
    """處理圖片訊息"""
    print(f"收到圖片訊息，message_id：{event.message.id}")
    try:
        # 取得圖片內容和 content_type
        message_content = line_bot_api.get_message_content(event.message.id)
        image_bytes = message_content.content
        # 動態取得 mime_type（不寫死為 image/jpeg）
        content_type = message_content.content_type or 'image/jpeg'
        # 只保留 mime_type 的主要部分（去掉 charset 等附加資訊）
        mime_type = content_type.split(';')[0].strip()
        print(f"圖片 content_type：{content_type}，使用 mime_type：{mime_type}")

        response_text = query_spare_parts_from_image(image_bytes, mime_type)
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=response_text)
        )
    except Exception as e:
        print(f"圖片處理錯誤：{str(e)}")
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="無法辨識照片，請確認照片清晰度或改用文字輸入料號")
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
