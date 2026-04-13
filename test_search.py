#!/usr/bin/env python3
"""
本地測試腳本：驗證搜尋邏輯、格式正規化、signal 移除等。
"""
import sys
import os

# 確保可以 import app 中的函數
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 只 import 需要的模組（不啟動 Flask）
import json
import re

# ─── 從 app.py 直接 import 需要測試的函數 ───
from app import (
    SPARE_PARTS,
    find_part_by_number,
    _is_ascii_only,
    _normalize_format,
    keyword_search_spare_parts,
    is_exact_match,
    extract_model_from_spec,
    build_spec_search_link,
    format_found_response,
    format_fuzzy_response,
    format_not_found_response,
    call_with_timeout,
)

passed = 0
failed = 0

def test(name, condition, detail=""):
    global passed, failed
    if condition:
        print(f"  ✅ PASS: {name}")
        passed += 1
    else:
        print(f"  ❌ FAIL: {name} — {detail}")
        failed += 1

# ═══════════════════════════════════════════════════════════════
print("\n═══ 測試 1：signal 相關程式碼完全移除 ═══")
with open('app.py', 'r', encoding='utf-8') as f:
    content = f.read()

# 檢查不能有 import signal（但 InvalidSignatureError 中的 Signature 不算）
lines = content.split('\n')
signal_lines = []
for i, line in enumerate(lines, 1):
    # 排除 InvalidSignatureError（linebot 的）和字串中的 signature
    stripped = line.strip()
    if 'signal' in stripped.lower():
        # 排除 signature、InvalidSignatureError 等
        if 'signature' in stripped.lower() or 'Signature' in stripped:
            continue
        signal_lines.append((i, stripped))

test("app.py 中無 signal 相關程式碼", len(signal_lines) == 0,
     f"找到 signal 相關行：{signal_lines}")

# 確認有 concurrent.futures
test("有 concurrent.futures import",
     "from concurrent.futures import ThreadPoolExecutor" in content)

test("有 call_with_timeout 函數",
     "def call_with_timeout(" in content)

# ═══════════════════════════════════════════════════════════════
print("\n═══ 測試 2：call_with_timeout 函數正常運作 ═══")

# 測試正常呼叫
result = call_with_timeout(lambda: 42, timeout=5)
test("call_with_timeout 正常回傳", result == 42, f"got {result}")

# 測試超時
import time
try:
    call_with_timeout(lambda: time.sleep(10), timeout=1)
    test("call_with_timeout 超時拋出 TimeoutError", False, "沒有拋出例外")
except TimeoutError:
    test("call_with_timeout 超時拋出 TimeoutError", True)
except Exception as e:
    test("call_with_timeout 超時拋出 TimeoutError", False, f"拋出了 {type(e).__name__}: {e}")

# ═══════════════════════════════════════════════════════════════
print("\n═══ 測試 3：料號查詢（精確比對，忽略大小寫） ═══")

# SH5056001 大寫
part = find_part_by_number("SH5056001")
test("料號查詢 SH5056001（大寫）", part is not None and part['part_number'] == 'SH5056001')

# sh5056001 小寫
part = find_part_by_number("sh5056001")
test("料號查詢 sh5056001（小寫）", part is not None and part['part_number'] == 'SH5056001')

# 前後空白
part = find_part_by_number("  SH5056001  ")
test("料號查詢帶空白 '  SH5056001  '", part is not None and part['part_number'] == 'SH5056001')

# 不存在的料號
part = find_part_by_number("NOTEXIST999")
test("不存在的料號回傳 None", part is None)

# ═══════════════════════════════════════════════════════════════
print("\n═══ 測試 4：_is_ascii_only 函數 ═══")

test("_is_ascii_only('FX2N') → True", _is_ascii_only('FX2N') == True)
test("_is_ascii_only('S2-060-9') → True", _is_ascii_only('S2-060-9') == True)
test("_is_ascii_only('ES') → True", _is_ascii_only('ES') == True)
test("_is_ascii_only('加耐力') → False", _is_ascii_only('加耐力') == False)
test("_is_ascii_only('吸嘴') → False", _is_ascii_only('吸嘴') == False)
test("_is_ascii_only('三菱FX2N') → False", _is_ascii_only('三菱FX2N') == False)

# ═══════════════════════════════════════════════════════════════
print("\n═══ 測試 5：格式正規化（_normalize_format） ═══")

test("_normalize_format('S2 060 9') == 's20609'",
     _normalize_format('S2 060 9') == 's20609')
test("_normalize_format('S2-060-9') == 's20609'",
     _normalize_format('S2-060-9') == 's20609')
test("_normalize_format('S2_060_9') == 's20609'",
     _normalize_format('S2_060_9') == 's20609')
test("空格、連字號、底線正規化後相同",
     _normalize_format('S2 060 9') == _normalize_format('S2-060-9') == _normalize_format('S2_060_9'))

# ═══════════════════════════════════════════════════════════════
print("\n═══ 測試 6：關鍵字搜尋 — 格式正規化（S2 060 9 → S2-060-9） ═══")

results = keyword_search_spare_parts(['S2', '060', '9', 'S2 060 9'])
found_sp0661000 = any(p.get('part_number') == 'SP0661000' for p, s, m in results)
test("搜尋 'S2 060 9' 相關關鍵字能找到 SP0661000（S2-060-9）", found_sp0661000,
     f"結果數：{len(results)}, 料號：{[p.get('part_number') for p,s,m in results[:5]]}")

# ═══════════════════════════════════════════════════════════════
print("\n═══ 測試 7：中文品牌名搜尋 ═══")

# 搜尋「加耐力」
results = keyword_search_spare_parts(['加耐力'])
found_jnl = any(p.get('part_number') == 'SP0661000' for p, s, m in results)
test("搜尋「加耐力」能找到 SP0661000", found_jnl,
     f"結果數：{len(results)}")

# 搜尋「吸嘴」
results = keyword_search_spare_parts(['吸嘴'])
found_xz = any('吸嘴' in p.get('specification', '') for p, s, m in results)
test("搜尋「吸嘴」能找到含吸嘴的備品", found_xz,
     f"結果數：{len(results)}")

# 搜尋「吸嘴 加耐力」
results = keyword_search_spare_parts(['吸嘴', '加耐力'])
found_both = any(p.get('part_number') == 'SP0661000' for p, s, m in results)
test("搜尋「吸嘴 加耐力」能找到 SP0661000", found_both,
     f"結果數：{len(results)}")

# ═══════════════════════════════════════════════════════════════
print("\n═══ 測試 8：is_exact_match 精確比對邏輯 ═══")

sh5056001 = find_part_by_number("SH5056001")
test("is_exact_match('FX2N-8EX-ES/UL', SH5056001) → True",
     is_exact_match('FX2N-8EX-ES/UL', sh5056001) == True)
test("is_exact_match('fx2n-8ex-es/ul', SH5056001) → True（忽略大小寫）",
     is_exact_match('fx2n-8ex-es/ul', sh5056001) == True)
test("is_exact_match('FX2N-8ER-ES', SH5056001) → False（不同型號）",
     is_exact_match('FX2N-8ER-ES', sh5056001) == False)
test("is_exact_match('FX2N', SH5056001) → False（子字串不算完全符合）",
     is_exact_match('FX2N', sh5056001) == False)
test("is_exact_match('SH5056001', SH5056001) → True（料號比對）",
     is_exact_match('SH5056001', sh5056001) == True)

# ═══════════════════════════════════════════════════════════════
print("\n═══ 測試 9：extract_model_from_spec 規格型號提取 ═══")

test("extract_model_from_spec('PLC模組 三菱 FX2N-8EX-ES/UL') → 'FX2N-8EX-ES/UL'",
     extract_model_from_spec('PLC模組 三菱 FX2N-8EX-ES/UL') == 'FX2N-8EX-ES/UL')

test("extract_model_from_spec('吸嘴 加耐力 S2-060-9 , 雙層式') 包含 S2-060-9",
     'S2-060-9' in extract_model_from_spec('吸嘴 加耐力 S2-060-9 , 雙層式 , ID:φ3.7 , OD:φ10.9 , H:16.3 , 材質:矽膠 , 透明低印痕'))

# ═══════════════════════════════════════════════════════════════
print("\n═══ 測試 10：搜尋連結格式 ═══")

link = build_spec_search_link('Mitsubishi', 'FX2N-8EX-ES/UL')
test("搜尋連結包含 google.com/search", 'google.com/search' in link)
test("搜尋連結包含 datasheet", 'datasheet' in link)

# ═══════════════════════════════════════════════════════════════
print("\n═══ 測試 11：回覆格式 ═══")

# format_found_response
resp = format_found_response(sh5056001, 'Mitsubishi', 'FX2N-8EX-ES/UL', is_image=False)
test("format_found_response 不附搜尋連結（文字查詢）",
     'google.com' not in resp and '✅' in resp)

resp_img = format_found_response(sh5056001, 'Mitsubishi', 'FX2N-8EX-ES/UL', is_image=True)
test("format_found_response 附搜尋連結（圖片查詢）",
     'google.com' in resp_img and '✅' in resp_img)

# format_fuzzy_response 編號格式
results_for_fmt = [(sh5056001, 10, ['FX2N'])]
resp_fuzzy = format_fuzzy_response(results_for_fmt, 'Mitsubishi', 'FX2N', is_image=False)
test("format_fuzzy_response 有編號 '1.'", '1.' in resp_fuzzy)
test("format_fuzzy_response 顯示找到筆數", '1 筆' in resp_fuzzy)

# format_not_found_response
resp_nf = format_not_found_response('Mitsubishi', 'FX2N-XXXX', is_image=False)
test("format_not_found_response 不附搜尋連結（文字查詢）",
     'google.com' not in resp_nf and '❌' in resp_nf)

resp_nf_img = format_not_found_response('Mitsubishi', 'FX2N-XXXX', is_image=True)
test("format_not_found_response 附搜尋連結（圖片查詢）",
     'google.com' in resp_nf_img and '❌' in resp_nf_img)

# ═══════════════════════════════════════════════════════════════
print("\n═══ 測試 12：備品資料載入 ═══")
test("備品資料已載入", len(SPARE_PARTS) > 0, f"載入 {len(SPARE_PARTS)} 筆")
test("備品資料有正確欄位",
     all(k in SPARE_PARTS[0] for k in ['part_number', 'specification', 'warehouse_location', 'major_category', 'minor_category']))

# ═══════════════════════════════════════════════════════════════
print(f"\n{'='*60}")
print(f"測試結果：{passed} 通過 / {failed} 失敗 / {passed+failed} 總計")
print(f"{'='*60}")

if failed > 0:
    print("\n⚠️ 有測試失敗，請修復後再部署！")
    sys.exit(1)
else:
    print("\n✅ 所有測試通過，可以部署！")
    sys.exit(0)
