import os
import json
import re
import time
from urllib.parse import quote
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

def parse_retry_after(error_str):
    """從錯誤訊息中解析 retry-after 秒數，若無法解析預設回傳 60 秒。"""
    # 格式1：JSON 中的 "retryDelay": "30s" 或 retryDelay: 45s（含空格）
    match = re.search(r'retryDelay["\s]*:\s*["\s]*(\d+)s?', error_str, re.IGNORECASE)
    if match:
        return int(match.group(1))
    # 格式2：retry_delay 下劃線格式
    match = re.search(r'retry[_-]delay[\s"]*[=:]+[\s"]*(\d+)', error_str, re.IGNORECASE)
    if match:
        return int(match.group(1))
    # 格式3："retry after X seconds"
    match = re.search(r'retry after (\d+)', error_str, re.IGNORECASE)
    if match:
        return int(match.group(1))
    # 預設 60 秒
    return 60

class RateLimitError(Exception):
    """速率限制例外，攜帶建議等待秒數"""
    def __init__(self, wait_seconds):
        self.wait_seconds = wait_seconds
        super().__init__(f"Rate limit exceeded, retry after {wait_seconds}s")

def call_gemini_with_retry(contents, model='gemini-2.5-flash', max_retries=3):
    """
    呼叫 Gemini API，遇到暫時性錯誤（503/500）時自動重試。
    遇到速率限制（429）時直接拋出 RateLimitError，讓呼叫端決定如何回應用戶。
    """
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

            if '429' in error_str:
                wait_seconds = parse_retry_after(error_str)
                print(f"Gemini API 429 速率限制，建議等待 {wait_seconds} 秒")
                raise RateLimitError(wait_seconds)

            elif '503' in error_str or '500' in error_str:
                if attempt < max_retries - 1:
                    wait_time = (attempt + 1) * 5
                    print(f"Gemini API 服務暫時不可用，{wait_time} 秒後重試（第 {attempt + 1}/{max_retries} 次）")
                    time.sleep(wait_time)
                else:
                    print(f"Gemini API 服務暫時不可用，已達最大重試次數")
                    raise

            else:
                print(f"Gemini API 錯誤：{error_str[:200]}")
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

def build_spec_search_link(keyword):
    """根據關鍵字建立產品規格 Google 搜尋連結"""
    encoded = quote(keyword)
    return f"https://www.google.com/search?q={encoded}+規格+datasheet"

GOOGLE_LENS_URL = "https://lens.google.com/"

# 已知品牌名稱集合（純英文，不含連字號），用於排除搜尋關鍵字選取
KNOWN_BRANDS = {
    'panasonic', 'mitsubishi', 'omron', 'siemens', 'schneider', 'keyence',
    'fanuc', 'yaskawa', 'allen', 'bradley', 'rockwell', 'automation',
    'fuji', 'hitachi', 'toshiba', 'yokogawa', 'idec', 'autonics',
    'delta', 'weintek', 'proface', 'advantech', 'phoenix', 'contact',
    'wago', 'beckhoff', 'pilz', 'sick', 'banner', 'pepperl', 'fuchs',
    'turck', 'balluff', 'ifm', 'leuze', 'datalogic', 'cognex'
}

def extract_keyword_from_query(user_query):
    """
    從用戶輸入中提取最適合作為搜尋關鍵字的型號或料號。
    優先選取含連字號/斜線的型號（如 PM-T45、FX2N-8EX-ES/UL），
    其次選取含數字的英數混合字串（如 E3Z61），
    再次排除已知品牌名稱選其他字串，
    最後才使用最長字串（可能是品牌名）。
    """
    # 先將「PM - T45」這類帶空格的型號合併（去除空格後再處理）
    normalized = re.sub(r'([A-Za-z]+)\s*-\s*([A-Za-z0-9])', r'\1-\2', user_query)
    matches = re.findall(r'[A-Za-z0-9][A-Za-z0-9\-/\.]{2,}', normalized)
    if not matches:
        return user_query.strip()[:50]

    # 優先選取含連字號或斜線且含數字的型號（如 PM-T45、FX2N-8EX-ES/UL）
    model_with_hyphen = [m for m in matches if ('-' in m or '/' in m) and any(c.isdigit() for c in m)]
    if model_with_hyphen:
        return max(model_with_hyphen, key=len)

    # 其次選取含數字的英數混合字串（如 E3Z61、6ES7）
    model_with_digit = [m for m in matches if any(c.isdigit() for c in m)]
    if model_with_digit:
        return max(model_with_digit, key=len)

    # 過濾掉已知品牌名稱，選其他字串
    non_brand = [m for m in matches if m.lower() not in KNOWN_BRANDS]
    if non_brand:
        return max(non_brand, key=len)

    # 最後才用最長的字串（可能是品牌名）
    return max(matches, key=len)

def query_spare_parts_text(user_query):
    """使用 Gemini 進行文字查詢，查到備品附規格連結，查無備品附搜尋連結"""
    spare_parts_info = format_spare_parts_for_prompt()
    keyword = extract_keyword_from_query(user_query)

    prompt = f"""你是一個備品查詢助手。你的職責是幫助使用者查詢備品的位置資訊。

備品資料庫規則：
1. 使用者可能會輸入料號或規格關鍵字來查詢備品
2. 如果查詢到相符的備品，請以以下格式回覆（每個欄位一行）：

查詢到以下備品資訊：
料號：[料號]
規格：[規格]
倉庫位置：[倉庫位置]
大分類儲位：[大分類儲位]
小分類儲位：[小分類儲位]

3. 如果查詢到多筆相符的備品，每筆之間用空行分隔，並在最前面加上「共找到 X 筆備品資訊：」
4. 如果查無此備品資料，只需回答：「NOT_FOUND」（不需要其他說明）
5. 如果使用者的問題超出備品查詢範圍，回答：「我只能回答備品位置相關的查詢，其他問題無法回答」
6. 回答要簡潔清楚，適合在 LINE 上閱讀，不要加入多餘的說明文字

{spare_parts_info}

使用者查詢：{user_query}"""

    try:
        result = call_gemini_with_retry(prompt)
        result = result.strip()

        if result == 'NOT_FOUND' or '查無此備品' in result:
            spec_url = build_spec_search_link(keyword)
            return (
                f"資料庫中查無此備品，為您提供以下搜尋連結：\n\n"
                f"📋 產品規格查詢：\n{spec_url}"
            )
        else:
            spec_match = re.search(r'規格：(.+)', result)
            search_keyword = spec_match.group(1).strip() if spec_match else keyword
            spec_url = build_spec_search_link(search_keyword)
            return f"{result}\n\n📋 產品規格查詢：\n{spec_url}"

    except RateLimitError as e:
        return f"目前查詢請求太頻繁，請等待約 {e.wait_seconds} 秒後再試一次。"
    except Exception as e:
        print(f"Gemini 文字查詢失敗：{str(e)[:200]}")
        return "抱歉，查詢過程中發生錯誤，請稍後再試"

def extract_text_from_image(image_bytes, mime_type='image/jpeg'):
    """使用 Gemini Vision 從圖片中提取文字（支援動態 mime_type）"""
    from google.genai import types

    print(f"圖片大小：{len(image_bytes)} bytes，格式：{mime_type}")

    supported_types = ['image/jpeg', 'image/png', 'image/gif', 'image/webp']
    if mime_type not in supported_types:
        mime_type = 'image/jpeg'

    image_part = types.Part.from_bytes(data=image_bytes, mime_type=mime_type)

    # 改善 prompt：更積極辨識，即使角度不完美也要盡力提取
    prompt_text = (
        "請仔細觀察這張工業設備或電子元件的照片，盡力辨識並提取所有可見的文字資訊。\n"
        "特別注意：品牌名稱（如 Panasonic、OMRON、Mitsubishi）、型號（如 PM-T45、FX2N-8EX）、"
        "料號、規格標籤上的英數字組合。\n"
        "即使照片角度不完美或部分文字模糊，也請盡力辨識並回傳所有能看到的文字。\n"
        "只需回傳辨識到的文字內容，不需要其他說明或解釋。\n"
        "如果完全無法辨識任何文字，才回傳：UNREADABLE"
    )

    result = call_gemini_with_retry([prompt_text, image_part])
    print(f"從圖片提取的文字：{result[:200]}")
    return result

def query_spare_parts_from_image(image_bytes, mime_type='image/jpeg'):
    """從圖片中提取資訊並查詢備品，查到備品附規格連結，查無備品附搜尋連結，辨識失敗附 Google Lens"""
    # 圖片辨識失敗時的通用回覆（含 Google Lens）
    def image_fail_response(extra_keyword=None):
        lines = ["無法從照片辨識備品資訊，您可以使用 Google Lens 以圖搜尋：",
                 f"\n🔍 Google Lens（以圖搜圖）：{GOOGLE_LENS_URL}"]
        if extra_keyword:
            spec_url = build_spec_search_link(extra_keyword)
            lines.append(f"\n📋 產品規格查詢：\n{spec_url}")
        return "".join(lines)

    try:
        extracted_text = extract_text_from_image(image_bytes, mime_type)
    except RateLimitError as e:
        return f"目前查詢請求太頻繁，請等待約 {e.wait_seconds} 秒後再試一次。"
    except Exception as e:
        print(f"圖片文字提取失敗：{str(e)[:200]}")
        return image_fail_response()

    # Gemini 完全無法辨識
    if not extracted_text or not extracted_text.strip() or extracted_text.strip() == 'UNREADABLE':
        return image_fail_response()

    spare_parts_info = format_spare_parts_for_prompt()
    keyword = extract_keyword_from_query(extracted_text)

    prompt = f"""你是一個備品查詢助手。根據從圖片中辨識到的文字，查詢備品資訊。

備品資料庫規則：
1. 根據辨識文字中的料號、型號或規格來查詢備品（允許模糊比對，例如 PM-T45 可比對到含 PM-T45 的規格）
2. 如果查詢到相符的備品，請以以下格式回覆（每個欄位一行）：

查詢到以下備品資訊：
料號：[料號]
規格：[規格]
倉庫位置：[倉庫位置]
大分類儲位：[大分類儲位]
小分類儲位：[小分類儲位]

3. 如果查詢到多筆相符的備品，每筆之間用空行分隔，並在最前面加上「共找到 X 筆備品資訊：」
4. 如果查無此備品資料，只需回答：「NOT_FOUND」（不需要其他說明）
5. 回答要簡潔清楚，適合在 LINE 上閱讀，不要加入多餘的說明文字

{spare_parts_info}

從圖片辨識到的文字如下：
{extracted_text}

請根據這些文字查詢對應的備品資訊。"""

    try:
        result = call_gemini_with_retry(prompt)
        result = result.strip()

        if result == 'NOT_FOUND' or '查無此備品' in result:
            spec_url = build_spec_search_link(keyword)
            return (
                f"資料庫中查無此備品，為您提供以下搜尋連結：\n\n"
                f"📋 產品規格查詢：\n{spec_url}\n\n"
                f"🔍 以圖搜尋更多資訊：{GOOGLE_LENS_URL}"
            )
        else:
            spec_match = re.search(r'規格：(.+)', result)
            search_keyword = spec_match.group(1).strip() if spec_match else keyword
            spec_url = build_spec_search_link(search_keyword)
            return (
                f"{result}\n\n"
                f"📋 產品規格查詢：\n{spec_url}\n\n"
                f"🔍 以圖搜尋更多資訊：{GOOGLE_LENS_URL}"
            )

    except RateLimitError as e:
        return f"目前查詢請求太頻繁，請等待約 {e.wait_seconds} 秒後再試一次。"
    except Exception as e:
        print(f"備品查詢失敗：{str(e)[:200]}")
        return image_fail_response(keyword)

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
        message_content = line_bot_api.get_message_content(event.message.id)
        image_bytes = message_content.content
        content_type = message_content.content_type or 'image/jpeg'
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
            TextSendMessage(text=f"無法辨識照片，您可以使用 Google Lens 以圖搜尋：\n\n🔍 Google Lens（以圖搜圖）：{GOOGLE_LENS_URL}")
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
