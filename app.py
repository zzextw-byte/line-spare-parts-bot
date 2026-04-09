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
    match = re.search(r'retryDelay[\s"]*[:\s]+[\s"]*(\d+)s?', error_str, re.IGNORECASE)
    if match:
        return int(match.group(1))
    match = re.search(r'retry[_-]delay[\s"]*[=:]+[\s"]*(\d+)', error_str, re.IGNORECASE)
    if match:
        return int(match.group(1))
    match = re.search(r'retry after (\d+)', error_str, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return 60

class RateLimitError(Exception):
    """速率限制例外，攜帶建議等待秒數"""
    def __init__(self, wait_seconds):
        self.wait_seconds = wait_seconds
        super().__init__(f"Rate limit exceeded, retry after {wait_seconds}s")

def call_gemini_with_retry(contents, model='gemini-2.5-flash', max_retries=3):
    """
    呼叫 Gemini API，遇到暫時性錯誤（503/500）時自動重試。
    遇到速率限制（429）時直接拋出 RateLimitError。
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

# ─── 備品資料載入 ───────────────────────────────────────────────────────────────

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

SPARE_PARTS = load_spare_parts_data()
GOOGLE_LENS_URL = "https://lens.google.com/"

# ─── 關鍵字搜尋 ─────────────────────────────────────────────────────────────────

def keyword_search_spare_parts(keywords):
    """
    以關鍵字清單對備品資料庫進行搜尋。
    對每筆備品的規格和料號計算符合的關鍵字數量，
    至少有 1 個符合才列出，按符合分數由高到低排列。
    回傳 (part, score, matched_keywords) tuple 的清單。
    """
    if not keywords:
        return []

    # 全部轉小寫
    kw_lower = [k.lower().strip() for k in keywords if k.strip()]
    if not kw_lower:
        return []

    print(f"關鍵字搜尋：{kw_lower}")

    results = []
    for part in SPARE_PARTS:
        spec = part.get('specification', '').lower()
        part_num = part.get('part_number', '').lower()
        combined = spec + ' ' + part_num

        matched = [kw for kw in kw_lower if kw in combined]

        if matched:
            # 分數 = 符合數量 + 符合片段總長度（越長越精準）
            score = len(matched) + sum(len(t) for t in matched) / 100
            results.append((part, score, matched))

    results.sort(key=lambda x: x[1], reverse=True)
    return results

# ─── 搜尋連結建立 ───────────────────────────────────────────────────────────────

def build_spec_search_link(brand, model):
    """
    根據品牌和型號建立 Google 搜尋連結（只用英文，不帶中文）。
    優先使用 brand + model 組合，若兩者都為空則回傳空字串。
    """
    parts = []
    if brand and brand.strip():
        parts.append(brand.strip())
    if model and model.strip():
        parts.append(model.strip())

    if not parts:
        return ''

    query_str = ' '.join(parts) + ' datasheet specifications'
    # 將空格替換為 +，並對特殊字元進行編碼
    encoded = quote(query_str.replace(' ', '+'), safe='+')
    return f"https://www.google.com/search?q={encoded}"

# ─── 回覆格式化 ─────────────────────────────────────────────────────────────────

def format_found_response(part, brand, model, is_image=False):
    """查到備品時的回覆格式（情況 A：完全符合）"""
    spec = part.get('specification', '')
    part_num = part.get('part_number', '')
    warehouse = part.get('warehouse_location', '')
    major = part.get('major_category', '')
    minor = part.get('minor_category', '')

    lines = [
        "✅ 查詢到備品：",
        f"料號：{part_num}",
        f"規格：{spec}",
        f"倉庫位置：{warehouse}",
        f"大分類儲位：{major}",
        f"小分類儲位：{minor}",
    ]

    spec_url = build_spec_search_link(brand, model)
    if spec_url:
        lines.append(f"\n📋 產品規格查詢：\n{spec_url}")

    lines.append(f"🔍 以圖搜尋更多資訊：{GOOGLE_LENS_URL}")

    return "\n".join(lines)

def format_fuzzy_response(results, brand, model, is_image=False):
    """找到相似備品時的回覆格式（情況 B：相似符合）"""
    # 顯示辨識到的型號
    identified = (f"{brand} {model}".strip()) if (brand or model) else ''
    header = f"⚠️ 辨識型號：{identified}\n資料庫中找到相似備品（請確認是否為同一備品）：" if identified else "⚠️ 資料庫中找到相似備品（請確認是否為同一備品）："
    lines = [header, ""]

    for i, (part, score, matched) in enumerate(results[:3]):
        lines.append(
            f"料號：{part.get('part_number', '')}\n"
            f"規格：{part.get('specification', '')}\n"
            f"倉庫位置：{part.get('warehouse_location', '')}\n"
            f"大分類儲位：{part.get('major_category', '')}\n"
            f"小分類儲位：{part.get('minor_category', '')}"
        )

    spec_url = build_spec_search_link(brand, model)
    if spec_url:
        lines.append(f"\n📋 產品規格查詢：\n{spec_url}")

    lines.append(f"🔍 以圖搜尋更多資訊：{GOOGLE_LENS_URL}")

    return "\n".join(lines)

def format_not_found_response(brand, model, is_image=False):
    """查無備品時的回覆格式（情況 C：完全找不到）"""
    identified = (f"{brand} {model}".strip()) if (brand or model) else ''
    header = f"❌ 資料庫中查無此備品（辨識型號：{identified}）" if identified else "❌ 資料庫中查無此備品"
    lines = [header, ""]

    spec_url = build_spec_search_link(brand, model)
    if spec_url:
        lines.append(f"📋 產品規格查詢：\n{spec_url}")

    lines.append(f"🔍 以圖搜尋更多資訊：{GOOGLE_LENS_URL}")

    return "\n".join(lines)

def image_unreadable_response():
    """圖片完全無法辨識時的回覆"""
    return (
        "無法從照片辨識備品資訊，您可以使用 Google Lens 以圖搜尋：\n\n"
        f"🔍 Google Lens（以圖搜圖）：{GOOGLE_LENS_URL}"
    )

# ─── Gemini 呼叫 ────────────────────────────────────────────────────────────────

def extract_product_info_from_text(user_query):
    """
    使用 Gemini 從用戶文字輸入中提取產品型號資訊。
    回傳 dict：{"brand": "...", "model": "...", "keywords": [...]}
    若無法解析則回傳 {"brand": "", "model": "", "keywords": []}
    """
    prompt = (
        "請從以下用戶輸入中提取產品的品牌名稱和型號資訊。\n"
        "只提取有意義的型號資訊（品牌名、產品型號、系列號），"
        "不要提取生產日期、產地、序號、條碼、CE認證等無關資訊。\n"
        "請以 JSON 格式回傳：\n"
        "{\"brand\": \"品牌名\", \"model\": \"型號\", \"keywords\": [\"關鍵字1\", \"關鍵字2\"]}\n"
        "keywords 應包含品牌名、型號、以及型號的各個片段（如 FX2N-8EX-ES/UL 應包含 FX2N、8EX、ES、FX2N-8EX-ES/UL）。\n"
        "如果完全無法識別，回傳：{\"brand\": \"\", \"model\": \"\", \"keywords\": []}\n"
        "只回傳 JSON，不要其他說明。\n\n"
        f"用戶輸入：{user_query}"
    )

    try:
        result = call_gemini_with_retry(prompt)
        result = result.strip()
        # 移除 markdown code block（如果有）
        result = re.sub(r'^```(?:json)?\s*', '', result, flags=re.MULTILINE)
        result = re.sub(r'\s*```$', '', result, flags=re.MULTILINE)
        data = json.loads(result.strip())
        brand = data.get('brand', '') or ''
        model = data.get('model', '') or ''
        keywords = data.get('keywords', []) or []
        # 確保 keywords 是字串清單
        keywords = [str(k) for k in keywords if k]
        return {"brand": brand, "model": model, "keywords": keywords}
    except Exception as e:
        print(f"Gemini 文字解析失敗：{str(e)[:200]}")
        # fallback：直接用用戶輸入作為關鍵字
        return {"brand": "", "model": user_query.strip(), "keywords": [user_query.strip()]}

def extract_product_info_from_image(image_bytes, mime_type='image/jpeg'):
    """
    使用 Gemini Vision 從圖片中提取產品型號資訊。
    回傳 dict：{"brand": "...", "model": "...", "keywords": [...]}
    若完全無法辨識則回傳 None。
    """
    from google.genai import types

    print(f"圖片大小：{len(image_bytes)} bytes，格式：{mime_type}")

    supported_types = ['image/jpeg', 'image/png', 'image/gif', 'image/webp']
    if mime_type not in supported_types:
        mime_type = 'image/jpeg'

    image_part = types.Part.from_bytes(data=image_bytes, mime_type=mime_type)

    prompt_text = (
        "請仔細觀察這張工業設備或電子元件的照片，提取產品的品牌名稱和型號資訊。\n"
        "只提取有意義的型號資訊（品牌名、產品型號、系列號），"
        "不要提取生產日期、產地、序號、條碼、CE認證等無關資訊。\n"
        "即使照片角度不完美或部分文字模糊，也請盡力辨識。\n"
        "請以 JSON 格式回傳：\n"
        "{\"brand\": \"品牌名\", \"model\": \"型號\", \"keywords\": [\"關鍵字1\", \"關鍵字2\"]}\n"
        "keywords 應包含品牌名、型號、以及型號的各個片段（如 FX2N-8EX-ES/UL 應包含 FX2N、8EX、ES、FX2N-8EX-ES/UL）。\n"
        "如果完全無法辨識任何型號資訊，回傳：{\"brand\": \"\", \"model\": \"\", \"keywords\": []}\n"
        "只回傳 JSON，不要其他說明。"
    )

    result = call_gemini_with_retry([prompt_text, image_part])
    result = result.strip()
    print(f"Gemini 圖片辨識原始回應：{result[:300]}")

    # 移除 markdown code block
    result = re.sub(r'^```(?:json)?\s*', '', result, flags=re.MULTILINE)
    result = re.sub(r'\s*```$', '', result, flags=re.MULTILINE)

    data = json.loads(result.strip())
    brand = data.get('brand', '') or ''
    model = data.get('model', '') or ''
    keywords = data.get('keywords', []) or []
    keywords = [str(k) for k in keywords if k]

    print(f"提取結果：brand={brand}, model={model}, keywords={keywords}")
    return {"brand": brand, "model": model, "keywords": keywords}

# ─── 主要查詢邏輯 ────────────────────────────────────────────────────────────────

def query_spare_parts_text(user_query):
    """文字查詢主流程"""
    try:
        info = extract_product_info_from_text(user_query)
    except RateLimitError as e:
        return f"目前查詢請求太頻繁，請等待約 {e.wait_seconds} 秒後再試一次。"
    except Exception as e:
        print(f"文字解析失敗：{str(e)[:200]}")
        return "抱歉，查詢過程中發生錯誤，請稍後再試。"

    brand = info.get('brand', '')
    model = info.get('model', '')
    keywords = info.get('keywords', [])

    print(f"文字查詢 → brand={brand}, model={model}, keywords={keywords}")

    if not keywords:
        return "無法識別查詢的備品型號，請輸入料號或型號（例如：FX2N-8EX 或 SH5056001）。"

    results = keyword_search_spare_parts(keywords)

    if not results:
        return format_not_found_response(brand, model, is_image=False)

    # 找到最高分的結果
    best_part, best_score, best_matched = results[0]

    # 判斷是否為精確符合（最長關鍵字符合 = 完整型號符合）
    longest_kw = max(keywords, key=len) if keywords else ''
    spec_lower = best_part.get('specification', '').lower()
    part_num_lower = best_part.get('part_number', '').lower()
    is_exact = (model.lower() in spec_lower or
                model.lower() in part_num_lower or
                longest_kw.lower() in spec_lower or
                longest_kw.lower() in part_num_lower)

    if is_exact or best_score >= 2.0:
        return format_found_response(best_part, brand, model, is_image=False)
    else:
        return format_fuzzy_response(results, brand, model, is_image=False)

def query_spare_parts_from_image(image_bytes, mime_type='image/jpeg'):
    """圖片查詢主流程"""
    try:
        info = extract_product_info_from_image(image_bytes, mime_type)
    except RateLimitError as e:
        return f"目前查詢請求太頻繁，請等待約 {e.wait_seconds} 秒後再試一次。"
    except Exception as e:
        print(f"圖片辨識失敗：{str(e)[:200]}")
        return image_unreadable_response()

    brand = info.get('brand', '')
    model = info.get('model', '')
    keywords = info.get('keywords', [])

    print(f"圖片查詢 → brand={brand}, model={model}, keywords={keywords}")

    # 若 brand 和 model 都為空，視為無法辨識
    if not brand and not model and not keywords:
        return image_unreadable_response()

    if not keywords:
        return format_not_found_response(brand, model, is_image=True)

    results = keyword_search_spare_parts(keywords)

    if not results:
        return format_not_found_response(brand, model, is_image=True)

    # 判斷是否為精確符合
    longest_kw = max(keywords, key=len) if keywords else ''
    best_part, best_score, best_matched = results[0]
    spec_lower = best_part.get('specification', '').lower()
    part_num_lower = best_part.get('part_number', '').lower()
    is_exact = (model.lower() in spec_lower or
                model.lower() in part_num_lower or
                longest_kw.lower() in spec_lower or
                longest_kw.lower() in part_num_lower)

    if is_exact or best_score >= 2.0:
        return format_found_response(best_part, brand, model, is_image=True)
    else:
        return format_fuzzy_response(results, brand, model, is_image=True)

# ─── Flask 路由 ─────────────────────────────────────────────────────────────────

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
