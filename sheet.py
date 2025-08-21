# sheet.py
from __future__ import annotations

import asyncio
import threading
from typing import Dict, Tuple, List
import re

import gspread
from google.oauth2.service_account import Credentials

_STATUS_PREFIXES = ("出席", "欠席", "遅刻", "早退")
_TOKEN_RE = re.compile(
    r"(出席|欠席|遅刻(?:\([^)]*\))?|早退(?:\([^)]*\))?)"
)


def _base_status(cell: str) -> str:
    """セル値から先頭の基本ステータスを取り出す"""
    for p in _STATUS_PREFIXES:
        if cell.startswith(p):
            return p
    return "未回答"


class CellOccupiedError(Exception):
    """Spread Sheetへの書き込み時に、上書きが必要な時に送出"""
    def __init__(
        self,
        row: int,
        program: str,
        prev_part: str,
        prev_num: str,
        new_part: str,
        new_num: int,
    ) -> None:
        self.row = row
        self.program = program
        self.prev_part = prev_part
        self.prev_num = prev_num
        self.new_part = new_part
        self.new_num = new_num
        super().__init__(
            f"row:{row} {program}({prev_part}-{prev_num}) → "
            f"{new_part}-{new_num}"
        )


_EMOJI_TO_STATUS = {
    "shusseki": "出席",
    "kesseki": "欠席",
    "chikoku": "遅刻",
    "soutai": "早退",
}

# 1 行目 … ヘッダ（Part / Num / … / <日付> / <日付> / …）
# 2 行目 … メッセージ ID を格納
_HEADER_DATE_ROW = 1
_MESSAGE_ID_ROW = 2
_DATA_START_ROW = 3


# -------------------------------------------------------------
# 動的ヘッダビルド：プログラムごとに「_パート / _席次」を並べる
# -------------------------------------------------------------
def _default_headers(programs: List[str]) -> List[str]:
    heads = ["discord表示名", "氏名", "Discord ID"]
    for prog in programs:
        heads.extend([f"{prog}_パート", f"{prog}_席次"])
    return heads


class GoogleSheetsManager:
    """Google Spread Sheet ラッパー（同期 I/O）
    行＝奏者、列＝練習日
    """

    def __init__(
        self,
        spreadsheet_id: str,
        worksheet_name: str,
        credential_json: str = "credentials.json",
        programs: List[str] | None = None,
    ) -> None:
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive.readonly",
        ]
        creds = Credentials.from_service_account_file(credential_json,
                                                      scopes=scopes)
        client = gspread.authorize(creds)

        self.sh = client.open_by_key(spreadsheet_id)
        self.ws = self.sh.worksheet(worksheet_name)
        self.programs: List[str] = programs or []

        self._lock = threading.Lock()

        # 空Sheetならヘッダ・メッセージ行を用意
        self._ensure_headers()
        self._build_index()

    def _refresh_index(self) -> None:
        """外部で行／列が動いた可能性を考慮し、毎回 index を再構築"""
        self._build_index()

    # ------------------------------------------------------------------
    # public
    # ------------------------------------------------------------------

    # ----------------------------------------------------------
    # 列追加：練習日（ヘッダ=日付, 2 行目=message_id）
    # ----------------------------------------------------------
    def add_event_column(self, header_str: str, message_id: int) -> int:
        """
        新しい練習日列を一番右に追加し、ヘッダとメッセージ ID を記入
        既に message_id が登録済みなら既存列を返すだけ
        Returns
        -------
        col : int
            1-index の列番号
        """
        with self._lock:
            self._refresh_index()
            # 既存チェック
            if message_id in self._msgid_to_col:
                return self._msgid_to_col[message_id]

            # 右端の次列へ書き込み
            new_col = len(self._col_to_header) + 1
            self.ws.update_cell(_HEADER_DATE_ROW, new_col, header_str)
            self.ws.update_cell(_MESSAGE_ID_ROW, new_col, str(message_id))

            # インデックスを更新
            self._build_index()
            return new_col

    def update_status(  # noqa: C901, PLR0915
        self,
        message_id: int,
        member_id: int,
        status_name: str,
    ) -> None:
        """
        期待仕様
        --------
        1. 出席／欠席が来たらそれ 1 個だけをセルに残す。
        2. 遅刻／早退は同時に 1 個ずつまで残す。
           - 同じ種類を押し直したら置き換え
           - 片方だけ来た場合は既存のもう一方を温存
        """

        def _tokenize(text: str) -> Dict[str, str]:
            """
            セル文字列を基本 4 ステータス単位で分解。
            戻り値キーは『出席』『欠席』『遅刻』『早退』
            値は元文字列（括弧付きの場合も含む）。
            """
            mapping: Dict[str, str] = {}
            for tok in _TOKEN_RE.findall(text):
                if tok.startswith("遅刻"):
                    mapping["遅刻"] = tok.strip()
                elif tok.startswith("早退"):
                    mapping["早退"] = tok.strip()
                else:
                    mapping[tok] = tok.strip()
            return mapping

        # -------- 行・列の特定 --------------------------------
        self._refresh_index()  # ← Lock とインデックス再構築
        col = self._msgid_to_col[message_id]
        row = self._member_to_row[member_id]

        current_raw = self.ws.cell(row, col).value or ""
        cur_map = _tokenize(current_raw)

        # -------- 新ステータスを反映 ----------------------------
        base = (
            "遅刻" if status_name.startswith("遅刻") else
            "早退" if status_name.startswith("早退") else
            status_name  # 出席 or 欠席
        )

        if base in {"出席", "欠席"}:
            # 1. 出席／欠席は単独で確定
            new_val = base
        else:
            # 2. 遅刻／早退は共存可
            cur_map[base] = status_name.strip()  # 置き換え or 挿入
            # 片方だけ残っているかもしれないので順序を固定化
            parts = []
            if "遅刻" in cur_map:
                parts.append(cur_map["遅刻"])
            if "早退" in cur_map:
                parts.append(cur_map["早退"])
            new_val = " ".join(parts).strip()

        # -------- シートへ書き込み ------------------------------
        self.ws.update_cell(row, col, new_val)

    # ----------------------------------------------------------
    # draw.py 用：1 日の出欠を dict で取得
    # ----------------------------------------------------------
    def attendance_dict(  # noqa: C901
        self,
        message_id: int,
        program: str,
    ) -> Dict[Tuple[str, int, str], str]:
        """
        {(part, num, name): status_raw}
        num がシートに無い場合は 0
        """
        with self._lock:
            self._refresh_index()
            if message_id not in self._msgid_to_col:
                raise KeyError(f"message_id {message_id} が列に登録されていません")

            col = self._msgid_to_col[message_id]

            # ---------- ヘッダ行をユニーク化 ---------- #
            raw_header = self.ws.row_values(_HEADER_DATE_ROW)
            expected_headers: list[str] = []
            for idx, cell in enumerate(raw_header, start=1):
                if str(cell).strip():
                    expected_headers.append(str(cell).strip())
                else:
                    expected_headers.append(f"__col{idx}")

            records = self.ws.get_all_records(
                head=_HEADER_DATE_ROW,
                expected_headers=expected_headers,
            )

            att: Dict[Tuple[str, int, str], str] = {}
            header_key = self._col_to_header[col]

            for rec in records:
                # ---- パート・席次・氏名 ---------------------------
                part = str(rec.get(f"{program}_パート", "")).strip()
                try:
                    num = int(rec.get(f"{program}_席次", 0) or 0)
                except (ValueError, TypeError):
                    num = 0
                name = (
                    str(rec.get("氏名") or "").strip()
                    or str(rec.get("discord表示名") or "").strip()
                    or "???"
                )

                # ---- ステータス文字列 -----------------------------
                status_raw = str(rec.get(header_key, "")).strip()
                att[(part, num, name)] = status_raw if status_raw else "未回答"

            return att

    # ------------------------------------------------------
    # 複数行をまとめて追加
    # ------------------------------------------------------
    def append_rows_bulk(self, rows: List[List[str]]) -> None:
        if not rows:
            return
        with self._lock:
            self._refresh_index()
            start_row = self._next_data_row()
            self.ws.insert_rows(rows, row=start_row)
            self._build_index()

    def bulk_update_status_column(
        self,
        message_id: int,
        values_by_member: Dict[int, str],
    ) -> None:
        """
        出欠列（message_id に対応する列）に対し、Discord ID → ステータスの
        マッピングを列全体として一括更新する。
        列は空である前提。未反応メンバーは空文字を書き込む。
        """
        with self._lock:
            self._refresh_index()
            if message_id not in self._msgid_to_col:
                raise KeyError(
                    f"message_id {message_id} が列に登録されていません"
                )

            col = self._msgid_to_col[message_id]
            if not self._member_to_row:
                # メンバー行が無ければ書くものが無い
                return

            last_row = max(self._member_to_row.values())
            if last_row < _DATA_START_ROW:
                return

            # 列全体の配列を作る（未反応は空）
            values: list[list[str]] = [
                [""] for _ in range(_DATA_START_ROW, last_row + 1)
            ]

            for member_id, status in values_by_member.items():
                row = self._member_to_row.get(member_id)
                if not row:
                    continue
                idx = row - _DATA_START_ROW
                if 0 <= idx < len(values):
                    values[idx][0] = status

            col_a1 = self._col_to_a1(col)
            rng = f"{col_a1}{_DATA_START_ROW}:{col_a1}{last_row}"
            self.ws.update(rng, values)

    # ------------------------------------------------------------------
    # private
    # ------------------------------------------------------------------
    # --------------------------------------------------
    # 内部ユーティリティ：次に挿入すべき行を返す
    # --------------------------------------------------
    def _next_data_row(self) -> int:
        """
        3 行目 (_DATA_START_ROW) 以降で最後にデータが
        入っている行の次の行番号を返す。
        まだ 1 件も無ければ 3 を返す。
        """
        self._refresh_index()
        if not self._member_to_row:
            return _DATA_START_ROW
        return max(self._member_to_row.values()) + 1

    @staticmethod
    def _col_to_a1(col: int) -> str:
        """1-indexed 列番号を A1 形式の列名に変換（1 -> A, 27 -> AA）"""
        if col <= 0:
            raise ValueError("col must be >= 1")
        s = ""
        n = col
        while n:
            n, rem = divmod(n - 1, 26)
            s = chr(65 + rem) + s
        return s

    # ----------------------------------------------------------
    # 行・列インデックスを構築
    # ----------------------------------------------------------
    def _build_index(self) -> None:
        header = self.ws.row_values(_HEADER_DATE_ROW)
        msg_ids = self.ws.row_values(_MESSAGE_ID_ROW)

        # ----- 列番号 → ヘッダ文字列 -------------------------
        self._col_to_header: dict[int, str] = {
            i: header[i - 1] for i in range(1, len(header) + 1)
        }

        # ----- メッセージ ID → 列番号 ------------------------
        self._msgid_to_col: dict[int, int] = {}
        for col, raw in enumerate(msg_ids, start=1):
            val = str(raw).strip()
            if not val:
                continue
            try:
                self._msgid_to_col[int(val)] = col                 # 整数文字列
            except ValueError:
                try:
                    self._msgid_to_col[int(float(val))] = col      # 1.23E+17 形式
                except ValueError:
                    # 変換できなければ無視（手入力ミス等）
                    continue

        # ----- Discord ID → 行番号 ---------------------------
        id_col = header.index("Discord ID") + 1
        self._member_to_row: dict[int, int] = {}
        for row_idx, raw in enumerate(
            self.ws.col_values(id_col)[_DATA_START_ROW - 1:],
            start=_DATA_START_ROW,
        ):
            val = str(raw).strip()
            if not val:
                continue
            try:
                self._member_to_row[int(val)] = row_idx            # 整数文字列
            except ValueError:
                try:
                    self._member_to_row[int(float(val))] = row_idx  # 1.23E+17 形式
                except ValueError:
                    continue

    # ------------------------------------------------------------------
    # header / 初期行の自動作成
    # ------------------------------------------------------------------
    def _ensure_headers(self) -> None:
        """
        1 行目（ヘッダ）と 2 行目（メッセージ ID 行）が無ければ作成。
        ヘッダはプログラム数に応じて動的に生成する。
        """
        heads = _default_headers(self.programs)

        if not any(self.ws.row_values(_HEADER_DATE_ROW)):
            self.ws.insert_row(heads, index=_HEADER_DATE_ROW)

        if not any(self.ws.row_values(_MESSAGE_ID_ROW)):
            # ヘッダ長と同数の空セルを用意
            self.ws.insert_row([""] * len(heads), index=_MESSAGE_ID_ROW)

    # ------------------------------------------------------------------
    # Row append
    # ------------------------------------------------------------------
    def append_member(
        self,
        program: str,
        part: str,
        num: int,
        display_name: str,
        member_id: int,
        *,
        overwrite: bool = False,
    ) -> None:
        """
        ・discord表示名 は常に最新に上書き
        ・パート／席次に既存値があり、かつ値が変わる場合は
          overwrite=False なら CellOccupiedError を送出
        """
        with self._lock:
            self._refresh_index()
            heads = _default_headers(self.programs)

            # ===== 既存行がある場合 =====
            if member_id in self._member_to_row:
                row = self._member_to_row[member_id]

                # discord表示名は無条件更新
                disp_col = heads.index("discord表示名") + 1
                if self.ws.cell(row, disp_col).value != display_name:
                    self.ws.update_cell(row, disp_col, display_name)

                part_col = heads.index(f"{program}_パート") + 1
                num_col = heads.index(f"{program}_席次") + 1

                prev_part = self.ws.cell(row, part_col).value or ""
                prev_num = self.ws.cell(row, num_col).value or ""

                need_overwrite = (
                    (prev_part and prev_part != part)
                    or (prev_num and prev_num != str(num))
                )

                if need_overwrite and not overwrite:
                    raise CellOccupiedError(
                        row, program, prev_part, prev_num, part, num
                    )

                if need_overwrite or overwrite:
                    self.ws.update_cell(row, part_col, part)
                    self.ws.update_cell(row, num_col, str(num))

                self._build_index()
                return

            # ===== 新規行を追加 =====
            new_row = [""] * len(heads)
            new_row[heads.index("discord表示名")] = display_name
            new_row[heads.index("氏名")] = ""
            new_row[heads.index("Discord ID")] = str(member_id)
            new_row[heads.index(f"{program}_パート")] = part
            new_row[heads.index(f"{program}_席次")] = str(num)
            self.ws.insert_row(new_row, index=self._next_data_row())
            self._build_index()
            return


# ------------------------------------------------------------
# 非同期ラッパ（discord.py から呼びやすくする）
# ------------------------------------------------------------
class SheetAsyncBridge:
    """同期 gspread 呼び出しを asyncio から使いやすく包む"""

    def __init__(self, gs: GoogleSheetsManager) -> None:
        self.gs = gs

    async def update_status_async(
        self,
        message_id: int,
        member_id: int,
        status_name: str,
    ) -> None:
        await asyncio.to_thread(
            self.gs.update_status, message_id, member_id, status_name
        )

    async def attendance_dict_async(
        self,
        message_id: int,
        program: str,
    ) -> Dict[Tuple[str, int, str], str]:
        return await asyncio.to_thread(
            self.gs.attendance_dict, message_id, program
        )

    async def add_event_column_async(
        self,
        header_str: str,
        message_id: int,
    ) -> int:
        return await asyncio.to_thread(
            self.gs.add_event_column, header_str, message_id
        )

    async def append_member_async(
        self,
        program: str,
        part: str,
        num: int,
        display_name: str,
        member_id: int,
        *,
        overwrite: bool = False,
    ) -> None:
        await asyncio.to_thread(
            self.gs.append_member,
            program,
            part,
            num,
            display_name,
            member_id,
            overwrite=overwrite,
        )

    async def bulk_update_status_column_async(
        self,
        message_id: int,
        values_by_member: Dict[int, str],
    ) -> None:
        await asyncio.to_thread(
            self.gs.bulk_update_status_column,
            message_id,
            values_by_member,
        )
