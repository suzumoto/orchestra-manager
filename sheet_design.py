# sheet_design.py
"""
出欠管理スプレッドシートのデザイン（書式）適用モジュール。

セルの値・行列の構造には一切触れず、書式のみを batchUpdate で設定する。
sheet.py はヘッダ文字列と行番号でセルにアクセスするため、
このモジュールの適用で bot の動作が変わることはない。

適用内容
--------
- bot 専用データの非表示（2 行目のメッセージ ID 行 / Discord ID 列）
- ヘッダ行の配色・行固定（ヘッダ 2 行 + 名前列）
- 出欠ステータスの色分け（出席/欠席/遅刻/早退/遅刻+早退/未回答）
- パート列の色分け（弦=暖色、木管=緑、金管=青、打楽器ほか=紫系）
- 交互の縞模様・列幅・フォント統一

条件付き書式は行・列とも無制限の範囲に張るため、bot が練習日列や
メンバー行を追加しても自動で追従する。
再実行しても安全（既存の条件付き書式・縞模様を消してから張り直す）。
"""
from __future__ import annotations

from typing import List

import gspread

# 列レイアウト（0-index）。sheet.py の _default_headers() と対応させること。
_COL_DISCORD_ID = 2  # C: Discord ID（bot 専用 → 非表示）
_FIXED_COLS = 3      # discord表示名 / 氏名 / Discord ID


# 日付ヘッダの表示形式（内部値は年込みの日付、表示は 'M月D日(曜)'）
# シートのロケールが ja_JP のため ddd は日本語の曜日 1 文字になる
DATE_HEADER_PATTERN = 'm"月"d"日"(ddd)'


def date_header_format_request(sheet_id: int, col_index: int) -> dict:
    """
    日付ヘッダセル（1 行目・col_index 列、0-index）に
    'M月D日(曜)' の表示形式を設定する batchUpdate リクエストを返す。
    """
    return {
        "repeatCell": {
            "range": {"sheetId": sheet_id,
                      "startRowIndex": 0, "endRowIndex": 1,
                      "startColumnIndex": col_index,
                      "endColumnIndex": col_index + 1},
            "cell": {"userEnteredFormat": {
                "numberFormat": {"type": "DATE",
                                 "pattern": DATE_HEADER_PATTERN},
            }},
            "fields": "userEnteredFormat.numberFormat",
        }
    }


def _rgb(hexcode: str) -> dict:
    h = hexcode.lstrip("#")
    return {
        "red": int(h[0:2], 16) / 255,
        "green": int(h[2:4], 16) / 255,
        "blue": int(h[4:6], 16) / 255,
    }


# ---- カラーパレット ----------------------------------------------
_HEADER_BG = "#37474F"   # ブルーグレー（ヘッダ）
_HEADER_FG = "#FFFFFF"
_BAND_MAIN = "#FFFFFF"
_BAND_ALT = "#F6F8FA"
_GRID_LINE = "#E3E7EB"
_SEPARATOR = "#90A4AE"   # 固定列と日付列の境界線

# 出欠ステータス: (背景, 文字色)
_STATUS_COLORS = {
    "出席":     ("#CEEAD6", "#137333"),
    "欠席":     ("#FAD2CF", "#B3261E"),
    "遅刻":     ("#FEEFC3", "#9A6700"),
    "早退":     ("#D3E3FD", "#0B57D0"),
    "遅刻早退": ("#E9DDFD", "#6B3FA0"),
    "未回答":   ("#F1F3F4", "#80868B"),
}

# パート: グループごとに色系統をそろえたパステル
_PART_COLORS = {
    # 弦楽器（暖色系）
    "Vn1st": "#FFCC80",
    "Vn2nd": "#FFE0B2",
    "Va":    "#FFE082",
    "Vc":    "#FFECB3",
    "Cb":    "#FFF8E1",
    # 木管（緑系）
    "Fl":    "#A5D6A7",
    "Ob":    "#C8E6C9",
    "Cl":    "#C5E1A5",
    "Fg":    "#DCEDC8",
    # 金管（青系）
    "Hr":    "#90CAF9",
    "Tp":    "#BBDEFB",
    "Tb":    "#81D4FA",
    "Tuba":  "#B3E5FC",
    # 打楽器ほか（紫〜グレー系）
    "Perc":  "#E1BEE7",
    "Timp":  "#D1C4E9",
    "Pf":    "#CFD8DC",
    "Hp":    "#F8BBD0",
}


def build_design_requests(
    sheet_id: int,
    n_cols: int,
    n_rows: int,
    n_programs: int,
    existing_cf_count: int = 0,
    existing_banding_ids: List[int] | None = None,
) -> list:
    """
    1 ワークシート分の書式設定 batchUpdate リクエスト一覧を組み立てる。

    入力
    ----
    sheet_id : int
        対象ワークシートの sheetId
    n_cols, n_rows : int
        グリッドの列数・行数（列幅や罫線の適用範囲に使う）
    n_programs : int
        プログラム数（パート/席次の列位置と日付列の開始位置を決める）
    existing_cf_count : int
        既存の条件付き書式ルール数（再実行時に削除するため）
    existing_banding_ids : list[int] | None
        既存の縞模様 ID 一覧（同上）

    出力
    ----
    list
        spreadsheets.batchUpdate に渡す requests のリスト
    """
    part_cols = [_FIXED_COLS + 2 * i for i in range(n_programs)]
    num_cols = [c + 1 for c in part_cols]
    date_start = _FIXED_COLS + 2 * n_programs

    req: list = []

    # --- 既存の条件付き書式・縞模様を掃除（再実行できるように） ---
    for _ in range(existing_cf_count):
        req.append({"deleteConditionalFormatRule": {"sheetId": sheet_id,
                                                    "index": 0}})
    for bid in existing_banding_ids or []:
        req.append({"deleteBanding": {"bandedRangeId": bid}})

    # --- シート全体の基本書式（フォント・縦中央） ---
    req.append({
        "repeatCell": {
            "range": {"sheetId": sheet_id},
            "cell": {"userEnteredFormat": {
                "textFormat": {"fontFamily": "Noto Sans JP", "fontSize": 10},
                "verticalAlignment": "MIDDLE",
                "wrapStrategy": "CLIP",
            }},
            "fields": ("userEnteredFormat(textFormat.fontFamily,"
                       "textFormat.fontSize,verticalAlignment,wrapStrategy)"),
        }
    })

    # --- ヘッダ行（1 行目） ---
    req.append({
        "repeatCell": {
            "range": {"sheetId": sheet_id,
                      "startRowIndex": 0, "endRowIndex": 1},
            "cell": {"userEnteredFormat": {
                "backgroundColor": _rgb(_HEADER_BG),
                "textFormat": {
                    "fontFamily": "Noto Sans JP",
                    "fontSize": 10,
                    "bold": True,
                    "foregroundColor": _rgb(_HEADER_FG),
                },
                "horizontalAlignment": "CENTER",
                "verticalAlignment": "MIDDLE",
                "wrapStrategy": "WRAP",
            }},
            "fields": ("userEnteredFormat(backgroundColor,textFormat,"
                       "horizontalAlignment,verticalAlignment,wrapStrategy)"),
        }
    })
    req.append({
        "updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "ROWS",
                      "startIndex": 0, "endIndex": 1},
            "properties": {"pixelSize": 36},
            "fields": "pixelSize",
        }
    })

    # --- bot 専用データを非表示：メッセージ ID 行 / Discord ID 列 ---
    req.append({
        "updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "ROWS",
                      "startIndex": 1, "endIndex": 2},
            "properties": {"hiddenByUser": True},
            "fields": "hiddenByUser",
        }
    })
    req.append({
        "updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                      "startIndex": _COL_DISCORD_ID,
                      "endIndex": _COL_DISCORD_ID + 1},
            "properties": {"hiddenByUser": True},
            "fields": "hiddenByUser",
        }
    })

    # --- 固定表示とグリッド線非表示 ---
    req.append({
        "updateSheetProperties": {
            "properties": {
                "sheetId": sheet_id,
                "gridProperties": {
                    "frozenRowCount": 2,     # ヘッダ + 非表示の ID 行
                    "frozenColumnCount": _FIXED_COLS,
                    "hideGridlines": True,
                },
            },
            "fields": ("gridProperties.frozenRowCount,"
                       "gridProperties.frozenColumnCount,"
                       "gridProperties.hideGridlines"),
        }
    })

    # --- 列幅 ---
    widths = {(0, 1): 150, (1, 2): 110}
    for c in part_cols:
        widths[(c, c + 1)] = 72
    for c in num_cols:
        widths[(c, c + 1)] = 48
    if date_start < n_cols:
        widths[(date_start, n_cols)] = 150
    for (s, e), px in widths.items():
        req.append({
            "updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                          "startIndex": s, "endIndex": e},
                "properties": {"pixelSize": px},
                "fields": "pixelSize",
            }
        })

    # --- パート・席次は中央揃え、日付列は中央揃え + 折り返し ---
    if part_cols:
        req.append({
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 2,
                          "startColumnIndex": part_cols[0],
                          "endColumnIndex": date_start},
                "cell": {"userEnteredFormat": {
                    "horizontalAlignment": "CENTER"}},
                "fields": "userEnteredFormat.horizontalAlignment",
            }
        })
    req.append({
        "repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 2,
                      "startColumnIndex": date_start},
            "cell": {"userEnteredFormat": {
                "horizontalAlignment": "CENTER",
                "wrapStrategy": "WRAP",
            }},
            "fields": "userEnteredFormat(horizontalAlignment,wrapStrategy)",
        }
    })

    # --- 交互の縞模様（データ行） ---
    req.append({
        "addBanding": {
            "bandedRange": {
                "range": {"sheetId": sheet_id, "startRowIndex": 2,
                          "endRowIndex": n_rows,
                          "startColumnIndex": 0, "endColumnIndex": n_cols},
                "rowProperties": {
                    "firstBandColor": _rgb(_BAND_MAIN),
                    "secondBandColor": _rgb(_BAND_ALT),
                },
            }
        }
    })

    # --- 薄い罫線 + 固定列と日付列の境界線 ---
    req.append({
        "updateBorders": {
            "range": {"sheetId": sheet_id,
                      "startRowIndex": 0, "endRowIndex": n_rows,
                      "startColumnIndex": 0, "endColumnIndex": n_cols},
            "innerHorizontal": {"style": "SOLID", "color": _rgb(_GRID_LINE)},
            "innerVertical": {"style": "SOLID", "color": _rgb(_GRID_LINE)},
        }
    })
    if date_start >= 1:
        req.append({
            "updateBorders": {
                "range": {"sheetId": sheet_id,
                          "startRowIndex": 0, "endRowIndex": n_rows,
                          "startColumnIndex": date_start - 1,
                          "endColumnIndex": date_start},
                "right": {"style": "SOLID_MEDIUM", "color": _rgb(_SEPARATOR)},
            }
        })

    # --- 条件付き書式 ---------------------------------------------
    cf_index = 0

    def add_rule(ranges, condition, bg, fg=None, bold=False):
        nonlocal cf_index
        fmt = {"backgroundColor": _rgb(bg)}
        if fg:
            fmt["textFormat"] = {"foregroundColor": _rgb(fg), "bold": bold}
        req.append({
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": ranges,
                    "booleanRule": {"condition": condition, "format": fmt},
                },
                "index": cf_index,
            }
        })
        cf_index += 1

    def text_cond(ctype, value):
        return {"type": ctype, "values": [{"userEnteredValue": value}]}

    # 日付列（行・列とも無制限 → bot の列・行追加に自動追従）
    status_range = [{"sheetId": sheet_id, "startRowIndex": 2,
                     "startColumnIndex": date_start}]
    # 条件付き書式のカスタム数式は範囲の左上セルを基準にする
    tl = f"{gspread.utils.rowcol_to_a1(3, date_start + 1)}"       # 例: J3
    tl_col = "".join(ch for ch in tl if ch.isalpha())             # 例: J

    # 優先度順：遅刻+早退 → 出席 → 欠席 → 遅刻 → 早退 → 未回答(空欄)
    bg, fg = _STATUS_COLORS["遅刻早退"]
    add_rule(status_range,
             text_cond("CUSTOM_FORMULA",
                       f'=AND(ISNUMBER(FIND("遅刻",{tl})),'
                       f'ISNUMBER(FIND("早退",{tl})))'),
             bg, fg, bold=True)
    for key, ctype in (("出席", "TEXT_STARTS_WITH"),
                       ("欠席", "TEXT_STARTS_WITH"),
                       ("遅刻", "TEXT_CONTAINS"),
                       ("早退", "TEXT_CONTAINS")):
        bg, fg = _STATUS_COLORS[key]
        add_rule(status_range, text_cond(ctype, key), bg, fg, bold=True)
    # 未回答：メンバー行かつイベント列なのに空欄
    bg, fg = _STATUS_COLORS["未回答"]
    add_rule(status_range,
             text_cond("CUSTOM_FORMULA",
                       f'=AND($A3<>"",{tl_col}$1<>"",{tl}="")'),
             bg, fg)

    # パート列の色分け（全プログラムのパート列に同じルールを適用）
    if part_cols:
        part_ranges = [{"sheetId": sheet_id, "startRowIndex": 2,
                        "startColumnIndex": c, "endColumnIndex": c + 1}
                       for c in part_cols]
        for part, color in _PART_COLORS.items():
            add_rule(part_ranges, text_cond("TEXT_EQ", part), color)

    return req


def apply_design(
    spreadsheet: gspread.Spreadsheet,
    worksheet: gspread.Worksheet,
    n_programs: int,
) -> None:
    """
    1 ワークシートにデザイン（書式）を適用する。

    入力
    ----
    spreadsheet : gspread.Spreadsheet
        対象のスプレッドシート（batchUpdate の実行に使う）
    worksheet : gspread.Worksheet
        デザインを適用するワークシート
    n_programs : int
        プログラム数（settings.ini の [PROGRAM] の個数）

    出力
    ----
    なし（Sheets API の batchUpdate を 1 回呼ぶ）
    """
    meta = spreadsheet.fetch_sheet_metadata({
        "fields": ("sheets(properties(sheetId,gridProperties),"
                   "conditionalFormats,bandedRanges)")
    })
    cf_count = 0
    banding_ids: List[int] = []
    for s in meta["sheets"]:
        if s["properties"]["sheetId"] != worksheet.id:
            continue
        cf_count = len(s.get("conditionalFormats", []))
        banding_ids = [b["bandedRangeId"]
                       for b in s.get("bandedRanges", [])]
        break

    requests = build_design_requests(
        sheet_id=worksheet.id,
        n_cols=worksheet.col_count,
        n_rows=worksheet.row_count,
        n_programs=n_programs,
        existing_cf_count=cf_count,
        existing_banding_ids=banding_ids,
    )
    spreadsheet.batch_update({"requests": requests})
