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

def call_gemini_with_retry(contents, model='gemini-2.5-flash', max_retries=3, timeout=10):
    """
    呼叫 Gemini API，遇到暫時性錯誤（503/500）時自動重試。
    遇到速率限制（429）時直接拋出 RateLimitError。
    
    Args:
        contents: 傳送給 Gemini 的內容
        model: 使用的模型名稱
        max_retries: 最大重試次數
        timeout: 單次呼叫的 timeout 秒數（預設 10 秒）
    """
    import signal
    
    def timeout_handler(signum, frame):
        raise TimeoutError(f"Gemini API 呼叫超時（{timeout} 秒）")
    
    client = get_gemini_client()
    last_error = None

    for attempt in range(max_retries):
        try:
            # 設定 timeout 保護
            signal.signal(signal.SIGALRM, timeout_handler)
            signal.alarm(timeout)
            
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=contents
                )
                signal.alarm(0)  # 取消 alarm
                return response.text
            finally:
                signal.alarm(0)  # 確保取消 alarm

        except TimeoutError as e:
            print(f"Gemini API 呼叫超時：{str(e)}")
            raise
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

# ─── 料號直接查詢 ────────────────────────────────────────────────────────────────

def find_part_by_number(query):
    """
    以料號直接查詢備品（忽略大小寫、trim 前後空白）。
    若找到則回傳 part dict，否則回傳 None。
    """
    q = query.strip().lower()
    for part in SPARE_PARTS:
        if part.get('part_number', '').strip().lower() == q:
            return part
    return None

# ─── 關鍵字搜尋 ─────────────────────────────────────────────────────────────────

def _is_ascii_only(s):
    """判斷字串是否只含 ASCII 字元（用於區分中文品牌名與英數字型號片段）"""
    try:
        s.encode('ascii')
        return True
    except UnicodeEncodeError:
        return False

def keyword_search_spare_parts(keywords, model=''):
    """
    以關鍵字清單對備品資料庫進行搜尋。

    評分規則：
    - 「有效關鍵字」定義：純 ASCII 字元且長度 ≥ 3（排除中文品牌名及 ES/UL 等過短通用後綴）。
    - 備品必須有 ≥ 1 個「有效關鍵字」符合才列入結果。
      此設計讓 FX2N 這類型號前綴就能觸發相似搜尋，
      同時排除只有 ES/UL 等通用後綴符合的不相關備品。
    - 分數 = 有效符合數 + 所有符合片段總長度 / 100（越長越精準）。
    - 如果 model 不為空，且備品 specification 中有完全符合的型號，則 score += 10。
    - 回傳 (part, score, matched_keywords) tuple 的清單。
    """
    if not keywords:
        return []

    # 全部轉小寫並 trim
    kw_lower = [k.lower().strip() for k in keywords if k.strip()]
    if not kw_lower:
        return []

    print(f"關鍵字搜尋：{kw_lower}")

    # 提取 model 中的所有型號片段（用於完全符合判斷）
    model_patterns = []
    if model:
        # 正則提取符合 [A-Za-z0-9][A-Za-z0-9\-\/\.]{2,} 的型號
        model_patterns = re.findall(r'[A-Za-z0-9][A-Za-z0-9\-\/\.]{2,}', model)
        model_patterns = [p.lower() for p in model_patterns]  # 轉小寫以便比對

    results = []
    for part in SPARE_PARTS:
        spec = part.get('specification', '').lower()
        part_num = part.get('part_number', '').lower()
        combined = spec + ' ' + part_num

        # 所有符合的關鍵字（包含中文品牌名和短片段，用於分數計算）
        all_matched = [kw for kw in kw_lower if kw in combined]

        # 「有效符合」：純 ASCII 且長度 ≥ 3，排除中文品牌名及 ES/UL 等過短通用後綴
        effective_matched = [kw for kw in all_matched if _is_ascii_only(kw) and len(kw) >= 3]

        # 必須有 ≥ 1 個有效關鍵字符合才列入結果
        if len(effective_matched) >= 1:
            # 分數 = 有效符合數 + 所有符合片段總長度 / 100
            score = len(effective_matched) + sum(len(t) for t in all_matched) / 100
            
            # 如果 model 不空，檢查是否有完全符合的型號，有的話 score += 10
            if model_patterns:
                for pattern in model_patterns:
                    if pattern in combined:
                        score += 10
                        break  # 只需計算一次
            
            results.append((part, score, all_matched))

    results.sort(key=lambda x: x[1], reverse=True)
    return results

# ─── 完全符合判斷 ───────────────────────────────────────────────────────────────

def is_exact_match(queried_model, part):
    """
    嚴格判斷查詢型號是否與資料庫備品完全符合。
    queried_model 必須與備品規格中的型號片段或料號「完全相同」（忽略大小寫），
    不允許子字串包含，避免 FX2N-8ER-ES 誤判為 FX2N-8EX-ES/UL 的完全符合。
    """
    if not queried_model or not queried_model.strip():
        return False

    q = queried_model.strip().lower()

    # 與料號嚴格比對
    part_num = part.get('part_number', '').strip().lower()
    if q == part_num:
        return True

    # 從規格欄位提取所有英數字型號片段，逐一嚴格比對
    spec = part.get('specification', '')
    spec_models = re.findall(r'[A-Za-z0-9][A-Za-z0-9\-\/\.]{2,}', spec)
    for sm in spec_models:
        if q == sm.lower():
            return True

    return False

# ─── AI 二次判斷（從搜尋結果中選出最佳匹配）──────────────────────────────────────

def ai_select_best_match(query_model, query_brand, results, timeout=8):
    """
    使用 Gemini 從搜尋結果中選出最佳匹配。
    
    回傳值：
    - 'NONE'：搜尋結果中沒有任何相關備品
    - 'UNCERTAIN'：無法確定哪個最好
    - 料號：明確選中的備品料號
    - None：AI 判斷失敗或異常
    """
    if not results or not query_model:
        return None
    
    # 取前 5 筆結果（簡化以加快處理速度）
    top_results = results[:5]
    
    # 組成備品清單文字
    parts_text = ""
    for i, (part, score, matched) in enumerate(top_results, start=1):
        part_num = part.get('part_number', '')
        spec = part.get('specification', '')
        parts_text += f"{i}. 料號：{part_num}，規格：{spec}\n"
    
    # 組成 prompt
    brand_text = f" {query_brand}" if query_brand else ""
    prompt = f"""用戶查詢的型號是：{brand_text} {query_model}

以下是搜尋到的備品清單：
{parts_text}
請判斷搜尋結果中是否有與用戶查詢的型號相關的備品。

判斷規則：
- 只有當某筆備品確實是用戶查詢的同一產品、同系列產品或同類型產品時，才回傳該料號
- 如果搜尋結果中沒有任何一筆與用戶查詢的型號是同類型或同系列的產品，回傳：NONE
- 如果有多筆可能相關但無法確定哪個最好，回傳：UNCERTAIN

只回傳料號、NONE 或 UNCERTAIN，不要其他說明。"""
    
    try:
        response = call_gemini_with_retry(prompt, timeout=timeout)
        result = response.strip().upper()
        
        # 檢查是否是 NONE（沒有相關備品）
        if result == "NONE":
            print(f"AI 二次判斷：搜尋結果中沒有相關備品")
            return "NONE"
        
        # 檢查是否是 UNCERTAIN（無法確定）
        if result == "UNCERTAIN":
            print(f"AI 二次判斷：無法確定最佳匹配")
            return "UNCERTAIN"
        
        # 嘗試從結果中找到對應的備品
        for part, score, matched in top_results:
            if part.get('part_number', '').upper() == result:
                print(f"AI 二次判斷：選中料號 {result}")
                return part
        
        print(f"AI 二次判斷：回傳的料號 {result} 未在結果中找到")
        return None
        
    except RateLimitError as e:
        print(f"AI 二次判斷：遇到速率限制，等待 {e.wait_seconds} 秒")
        raise
    except Exception as e:
        print(f"AI 二次判斷失敗：{str(e)[:200]}")
        return None

# ─── 規格型號提取（用於搜尋連結）────────────────────────────────────────────────

def extract_model_from_spec(spec):
    """
    從規格字串中提取最長的英數字型號片段（含連字號/斜線），
    作為 Google 搜尋連結的型號關鍵字。
    例如：'PLC模組 三菱 FX2N-8EX-ES/UL' → 'FX2N-8EX-ES/UL'
    """
    candidates = re.findall(r'[A-Za-z0-9][A-Za-z0-9\-\/\.]{2,}', spec)
    if not candidates:
        return ''
    # 回傳最長的候選（通常是完整型號）
    return max(candidates, key=len)

# ─── 搜尋連結建立 ───────────────────────────────────────────────────────────────

def build_spec_search_link(brand, model):
    """
    根據品牌和型號建立 Google 搜尋連結（只用英文，不帶中文）。
    優先使用 brand + model 組合，若兩者都為空則回傳空字串。
    """
    parts_list = []
    if brand and brand.strip():
        parts_list.append(brand.strip())
    if model and model.strip():
        parts_list.append(model.strip())

    if not parts_list:
        return ''

    query_str = ' '.join(parts_list) + ' datasheet specifications'
    # 將空格替換為 +，並對特殊字元進行編碼
    encoded = quote(query_str.replace(' ', '+'), safe='+')
    return f"https://www.google.com/search?q={encoded}"

# ─── 回覆格式化 ─────────────────────────────────────────────────────────────────

def format_found_response(part, brand, model, is_image=False):
    """
    查到備品時的回覆格式（情況 A：完全符合）。
    - is_image=True：附產品規格搜尋連結
    - is_image=False：只顯示備品資訊，不附任何連結
    """
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

    if is_image:
        # 搜尋連結使用規格中的型號，而非 Gemini 辨識的 model（可能不準）
        spec_model = extract_model_from_spec(spec)
        spec_url = build_spec_search_link(brand, spec_model)
        if spec_url:
            lines.append(f"\n📋 產品規格查詢：\n{spec_url}")

    return "\n".join(lines)

def format_fuzzy_response(results, brand, model, is_image=False):
    """
    找到相似備品時的回覆格式（情況 B：相似符合）。
    - is_image=True：附產品規格搜尋連結
    - is_image=False：只顯示備品資訊，不附任何連結
    """
    identified = (f"{brand} {model}".strip()) if (brand or model) else ''
    total_count = len(results)
    
    # 根據結果總數決定標題文字
    if total_count > 3:
        count_text = f"資料庫中找到 {total_count} 筆相似備品，顯示前 3 筆（請確認是否為同一備品）："
    else:
        count_text = f"資料庫中找到 {total_count} 筆相似備品（請確認是否為同一備品）："
    
    header = (
        f"⚠️ 辨識型號：{identified}\n{count_text}"
        if identified else
        f"⚠️ {count_text}"
    )
    lines = [header, ""]

    for i, (part, score, matched) in enumerate(results[:3], start=1):
        lines.append(
            f"{i}.\n"
            f"料號：{part.get('part_number', '')}\n"
            f"規格：{part.get('specification', '')}\n"
            f"倉庫位置：{part.get('warehouse_location', '')}\n"
            f"大分類儲位：{part.get('major_category', '')}\n"
            f"小分類儲位：{part.get('minor_category', '')}"
        )
        # 在每筆之間加空行（除了最後一筆）
        if i < len(results[:3]):
            lines.append("")

    if is_image:
        spec_url = build_spec_search_link(brand, model)
        if spec_url:
            lines.append(f"\n📋 產品規格查詢：\n{spec_url}")

    return "\n".join(lines)

def format_not_found_response(brand, model, is_image=False):
    """
    查無備品時的回覆格式（情況 C：完全找不到）。
    - is_image=True：附產品規格搜尋連結
    - is_image=False：只顯示查無結果訊息，不附任何連結
    """
    identified = (f"{brand} {model}".strip()) if (brand or model) else ''
    header = f"❌ 資料庫中查無此備品（辨識型號：{identified}）" if identified else "❌ 資料庫中查無此備品"
    lines = [header, ""]

    if is_image:
        spec_url = build_spec_search_link(brand, model)
        if spec_url:
            lines.append(f"📋 產品規格查詢：\n{spec_url}")

    return "\n".join(lines)

def image_unreadable_response():
    """圖片完全無法辨識時的回覆"""
    return "無法從照片辨識備品資訊，請改用文字輸入料號或型號。"



# ─── Gemini 呼叫 ──────────────────────────────────────────────────────────────────

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
    """
    文字查詢主流程。
    先 trim 輸入，再嘗試直接料號比對（忽略大小寫）；
    若非料號格式，才呼叫 Gemini 解析型號後進行關鍵字搜尋。
    文字查詢一律不附搜尋連結。
    """
    # Step 1：trim 前後空白
    user_query = user_query.strip()
    if not user_query:
        return "請輸入料號或型號（例如：FX2N-8EX 或 SH5056001）。"

    print(f"文字查詢（trim 後）：'{user_query}'")

    # Step 2：直接料號查詢快速路徑（忽略大小寫）
    part = find_part_by_number(user_query)
    if part:
        print(f"料號直接命中：{part.get('part_number')}")
        return format_found_response(part, brand='', model='', is_image=False)

    # Step 3：呼叫 Gemini 解析型號關鍵字
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

    results = keyword_search_spare_parts(keywords, model)

    if not results:
        return format_not_found_response(brand, model, is_image=False)

    # 找到最高分的結果，子橛判斷是否完全符合
    best_part, best_score, best_matched = results[0]
    if is_exact_match(model, best_part):
        return format_found_response(best_part, brand, model, is_image=False)
    else:
        # 不是 exact_match，用 AI 二次判斷從前 10 筆中選出最佳匹配
        try:
            ai_result = ai_select_best_match(model, brand, results, timeout=8)
            
            # 處理 AI 的回傳值
            if ai_result == "NONE":
                # 搜尋結果中沒有任何相關備品
                return format_not_found_response(brand, model, is_image=False)
            elif ai_result == "UNCERTAIN":
                # 無法確定，顯示相似備品清單
                return format_fuzzy_response(results, brand, model, is_image=False)
            elif ai_result is not None and isinstance(ai_result, dict):
                # AI 選出了明確的最佳匹配
                return format_found_response(ai_result, brand, model, is_image=False)
        except (TimeoutError, RateLimitError) as e:
            # 如果 AI 二次判斷超時或速率限制，直接跳過，顯示相似備品清單
            print(f"AI 二次判斷超時或限制，跳過並顯示相似備品清單")
        except Exception as e:
            # AI 二次判斷失敗，繼續顯示相似備品
            print(f"AI 二次判斷失敗：{str(e)[:100]}，顯示相似備品清單")
        
        # 預設：AI 無法確定或失敗，顯示相似備品清單
        return format_fuzzy_response(results, brand, model, is_image=False)

def query_spare_parts_from_image(image_bytes, mime_type='image/jpeg'):
    """
    圖片查詢主流程。
    圖片查詢會附產品規格搜尋連結（使用規格型號）。
    """
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

    results = keyword_search_spare_parts(keywords, model)

    if not results:
        return format_not_found_response(brand, model, is_image=True)

    # 嚴格判斷是否完全符合：queried model 必須與資料庫型號完全相同
    best_part, best_score, best_matched = results[0]
    if is_exact_match(model, best_part):
        return format_found_response(best_part, brand, model, is_image=True)
    else:
        # 不是 exact_match，用 AI 二次判斷從前 10 筆中選出最佳匹配
        try:
            ai_result = ai_select_best_match(model, brand, results, timeout=8)
            
            # 處理 AI 的回傳值
            if ai_result == "NONE":
                # 搜尋結果中沒有任何相關備品
                return format_not_found_response(brand, model, is_image=True)
            elif ai_result == "UNCERTAIN":
                # 無法確定，顯示相似備品清單
                return format_fuzzy_response(results, brand, model, is_image=True)
            elif ai_result is not None and isinstance(ai_result, dict):
                # AI 選出了明確的最佳匹配
                return format_found_response(ai_result, brand, model, is_image=True)
        except (TimeoutError, RateLimitError) as e:
            # 如果 AI 二次判斷超時或速率限制，直接跳過，顯示相似備品清單
            print(f"AI 二次判斷超時或限制，跳過並顯示相似備品清單")
        except Exception as e:
            # AI 二次判斷失敗，繼續顯示相似備品
            print(f"AI 二次判斷失敗：{str(e)[:100]}，顯示相似備品清單")
        
        # 預設：AI 無法確定或失敗，顯示相似備品清單
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
@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    """處理文字訊息"""
    import time
    start_time = time.time()
    TIMEOUT_LIMIT = 25  # 25 秒超時限制
    
    user_message = event.message.text.strip()
    user_id = event.source.user_id
    
    print(f"收到文字訊息：{user_message}（User ID：{user_id}）")
    
    try:
        # 執行備品查詢
        response_text = query_spare_parts_text(user_message)
        
        # 檢查是否超時
        elapsed = time.time() - start_time
        if elapsed > TIMEOUT_LIMIT:
            response_text = "查詢超時，請稍後再試。"
        
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=response_text)
        )
    except Exception as e:
        print(f"文字查詢錯誤：{str(e)[:200]}")
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="查詢出錯，請稍後再試。")
        )


@handler.add(MessageEvent, message=ImageMessage)
def handle_image_message(event):
    """處理圖片訊息"""
    import time
    start_time = time.time()
    TIMEOUT_LIMIT = 25  # 25 秒超時限制
    
    user_id = event.source.user_id
    print(f"收到圖片訊息，message_id：{event.message.id}（User ID：{user_id}）")
    
    try:
        # 檢查是否已超時
        elapsed = time.time() - start_time
        if elapsed > TIMEOUT_LIMIT - 5:  # 提前 5 秒回覆
            raise TimeoutError("圖片處理超時")
        
        message_content = line_bot_api.get_message_content(event.message.id)
        image_bytes = message_content.content
        content_type = message_content.content_type or 'image/jpeg'
        mime_type = content_type.split(';')[0].strip()
        print(f"圖片 content_type：{content_type}，使用 mime_type：{mime_type}")

        # 檢查是否已超時
        elapsed = time.time() - start_time
        if elapsed > TIMEOUT_LIMIT - 3:  # 提前 3 秒回覆
            raise TimeoutError("圖片查詢超時")

        response_text = query_spare_parts_from_image(image_bytes, mime_type)
        
        # 檢查是否已超時
        elapsed = time.time() - start_time
        if elapsed > TIMEOUT_LIMIT:
            response_text = "查詢超時，請稍後再試。"
        
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=response_text)
        )
            
    except TimeoutError as e:
        print(f"圖片處理超時：{str(e)}")
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="查詢超時，請稍後再試。")
        )
    except Exception as e:
        print(f"圖片處理錯誤：{str(e)[:200]}")
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="無法辨識照片，請改用文字輸入料號或型號。")
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
