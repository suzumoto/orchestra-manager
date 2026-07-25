# sheet.py
from __future__ import annotations

import asyncio
import threading
from datetime import date
from typing import Dict, Tuple, List
import re

import gspread
from google.oauth2.service_account import Credentials

from sheet_design import apply_design, date_header_format_request

# Google Sheets の日付シリアル値の起点
_SHEETS_EPOCH = date(1899, 12, 30)


def _date_iso_to_serial(date_iso: str) -> int:
    """'YYYY/MM/DD' 形式の日付を Sheets のシリアル値に変換する"""
    y, m, d = map(int, date_iso.split("/"))
    return (date(y, m, d) - _SHEETS_EPOCH).days

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


def _extract_late_leave_tokens(text: str) -> Dict[str, str]:
    """
    セル文字列から「遅刻」「早退」のトークンだけを抜き出す。

    入力
    ----
    text : str
        セルの生文字列（例: "遅刻(19:00～) 早退"）

    出力
    ----
    dict[str, str]
        キーは '遅刻' '早退'。値は時刻付きの元トークン
        （例: {'遅刻': '遅刻(19:00～)'}）。無ければキー自体が存在しない。
    """
    tokens: Dict[str, str] = {}
    for tok in _TOKEN_RE.findall(text):
        if tok.startswith("遅刻"):
            tokens["遅刻"] = tok.strip()
        elif tok.startswith("早退"):
            tokens["早退"] = tok.strip()
    return tokens


def _merge_status_with_existing(old_val: str, new_status: str) -> str:
    """
    出欠セルの新ステータスを、既存セルの時刻情報を保持しつつマージする。

    入力
    ----
    old_val : str
        更新前のセル文字列（例: "遅刻(19:00～) 早退"）
    new_status : str
        新しいステータス文字列（例: "遅刻" '出席' '遅刻 早退'）
        時刻無しの単純な状態名を想定（複数は空白区切り）。

    出力
    ----
    str
        書き込むべきセル文字列。
        例: Old="遅刻(19:00～) 早退", New="遅刻" -> "遅刻(19:00～)"
        例: Old="出席", New="遅刻" -> "遅刻"（時刻情報はまだ無い）

    仕様
    ----
    - 新ステータスが「出席」「欠席」なら時刻は関係ないのでそのまま上書き。
    - 「遅刻」「早退」の場合、既存セルに同種の時刻付きトークンがあれば
      それを優先して残す。無ければ単純な新ステータス文字列を採用する。
    """
    if new_status in {"出席", "欠席"}:
        return new_status

    old_tokens = _extract_late_leave_tokens(old_val)
    new_parts = new_status.split()
    merged_parts = [old_tokens.get(part, part) for part in new_parts]
    return " ".join(merged_parts)


class SheetTargetNotFoundError(Exception):
    """message_id または member_id がシートのインデックスに無い場合に送出"""
    def __init__(self, *, message_id: int | None = None, member_id: int | None = None) -> None:
        self.message_id = message_id
        self.member_id = member_id
        if message_id is not None and member_id is not None:
            reason = f"message_id={message_id}, member_id={member_id} ともに未登録"
        elif message_id is not None:
            reason = f"message_id={message_id} が列に登録されていません"
        else:
            reason = f"member_id={member_id} が行に登録されていません"
        super().__init__(reason)


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
        """
        Google Spread Sheet に接続し、ヘッダ行が無ければ作成する。

        入力
        ----
        spreadsheet_id : str
            対象 Spread Sheet の ID
        worksheet_name : str
            対象ワークシート名（例: '全奏'）
        credential_json : str
            サービスアカウントの認証情報 JSON へのパス
        programs : list[str] | None
            プログラム名一覧（ヘッダの "{prog}_パート" 列生成に使う）

        出力
        ----
        なし（インスタンス初期化。gspread クライアント接続と
        内部インデックス構築を行う）
        """
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
    def add_event_column(
        self,
        header_str: str,
        message_id: int,
        event_date_iso: str | None = None,
    ) -> int:
        """
        新しい練習日列を追加し、ヘッダとメッセージ ID を記入する。
        既に message_id が登録済みなら既存列を返すだけ。

        入力
        ----
        header_str : str
            表示用ヘッダ文字列（event_date_iso が無い場合のみ使う。
            例: '10月DD日(未定)'）
        message_id : int
            RSVP 投稿の Discord メッセージ ID（列の同一性のキー）
        event_date_iso : str | None
            練習日が確定している場合の 'YYYY/MM/DD' 文字列。
            指定するとヘッダは実際の日付値（年情報を内部に保持）として
            書き込まれ、表示形式 'M月D日(曜)' が設定される。さらに既存の
            日付列と比較して時系列順の位置に列を挿入する
            （例: 12月25日の列の右に 1月15日の列が入る）。

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

            append_col = len(self._col_to_header) + 1

            if event_date_iso is None:
                # 日付未確定：従来どおりテキストヘッダを右端へ追加
                new_col = append_col
                self.ws.update_cell(_HEADER_DATE_ROW, new_col, header_str)
                self.ws.update_cell(_MESSAGE_ID_ROW, new_col, str(message_id))
                self._build_index()
                return new_col

            # ---- 日付確定：時系列順の挿入位置を決める ----
            new_serial = _date_iso_to_serial(event_date_iso)
            date_start_col = len(_default_headers(self.programs)) + 1

            # ヘッダ行を未加工値で取得（日付セルはシリアル値になる）
            raw = self.ws.get(
                f"{_HEADER_DATE_ROW}:{_HEADER_DATE_ROW}",
                value_render_option="UNFORMATTED_VALUE",
            )
            row1 = raw[0] if raw else []

            insert_at: int | None = None
            for col in range(date_start_col, len(row1) + 1):
                val = row1[col - 1]
                if isinstance(val, (int, float)) and val > new_serial:
                    insert_at = col
                    break

            if insert_at is not None:
                # 途中に空列を挿入（書式は左隣から引き継ぐ）
                self.ws.insert_cols(
                    [[]], col=insert_at, inherit_from_before=True
                )
                new_col = insert_at
            else:
                new_col = append_col

            # ヘッダは実際の日付値として書き込む（年情報を保持）
            a1 = f"{self._col_to_a1(new_col)}{_HEADER_DATE_ROW}"
            self.ws.update(
                values=[[event_date_iso]],
                range_name=a1,
                value_input_option="USER_ENTERED",
            )
            self.ws.update_cell(_MESSAGE_ID_ROW, new_col, str(message_id))

            # 表示形式 'M月D日(曜)' を設定
            self.sh.batch_update({
                "requests": [
                    date_header_format_request(self.ws.id, new_col - 1)
                ]
            })

            self._build_index()
            return new_col

    def registered_member_ids(self) -> set[int]:
        """シートに登録されている全メンバーの Discord ID を返す"""
        with self._lock:
            self._refresh_index()
            return set(self._member_to_row.keys())

    def sort_members_by_part(self, part_order: List[List[str]]) -> int:
        """
        データ行（3 行目以降）をパート順に並べ替える。

        入力
        ----
        part_order : list[list[str]]
            並び順を表すパート名グループのリスト。各グループは別名の集合で、
            例: [["Fl", "Picc"], ["Ob", "EHr"], ...] なら Picc は Fl と
            同じ位置に並ぶ。表記ゆれ対策として大文字小文字・記号は無視して
            比較する（'B.Cl' と 'bcl' は同一視）。

        並べ替えキー
        ------------
        1. パート順（最初に値が入っているプログラムのパート列を使う。
           part_order に無いパートは末尾、パート未記入はさらに後ろ、
           完全な空行は最後尾）
        2. 同一パート内は席次の昇順（数値でなければ後ろ）
        3. discord表示名

        出力
        ----
        int : 並べ替え対象になったデータ行数
        """
        def _norm(s: str) -> str:
            return "".join(ch for ch in s.lower() if ch.isalnum())

        rank_by_alias: Dict[str, int] = {}
        for rank, aliases in enumerate(part_order):
            for alias in aliases:
                rank_by_alias.setdefault(_norm(alias), rank)
        unknown_rank = len(part_order)      # part_order に無いパート
        no_part_rank = len(part_order) + 1  # パート未記入
        empty_rank = len(part_order) + 2    # 完全な空行

        with self._lock:
            self._refresh_index()
            heads = _default_headers(self.programs)
            disp_idx = heads.index("discord表示名")
            part_idxs = [heads.index(f"{p}_パート") for p in self.programs]
            num_idxs = [heads.index(f"{p}_席次") for p in self.programs]

            all_values = self.ws.get_values()
            if len(all_values) < _DATA_START_ROW:
                return 0
            data = all_values[_DATA_START_ROW - 1:]
            width = max(len(heads), max((len(r) for r in data), default=0))
            data = [r + [""] * (width - len(r)) for r in data]

            def sort_key(row: List[str]) -> tuple:
                if not any(cell.strip() for cell in row):
                    return (empty_rank, 0, "")
                part = ""
                num_raw = ""
                for pi, ni in zip(part_idxs, num_idxs):
                    if row[pi].strip():
                        part = row[pi].strip()
                        num_raw = row[ni].strip()
                        break
                if not part:
                    return (no_part_rank, 0, row[disp_idx])
                rank = rank_by_alias.get(_norm(part), unknown_rank)
                try:
                    num = int(num_raw)
                except ValueError:
                    num = 10 ** 9  # 席次未記入・数値以外は同パート内の後ろ
                return (rank, num, row[disp_idx])

            data.sort(key=sort_key)  # 安定ソートなので同キー内の順序は保持

            last_row = _DATA_START_ROW + len(data) - 1
            rng = f"A{_DATA_START_ROW}:{self._col_to_a1(width)}{last_row}"
            self.ws.update(values=data, range_name=rng)
            self._build_index()
            return len(data)

    def set_resolved_bases(
        self,
        message_id: int,
        member_id: int,
        bases: List[str],
    ) -> None:
        """
        現在アクティブな出欠リアクションの解決結果でセルを書き換える
        （Discord 側のリアクション付け外しイベントから呼ばれる）。

        入力
        ----
        message_id, member_id : int, int
            行・列を特定するキー
        bases : list[str]
            [] （未回答/空欄）、['出席']、['欠席']、['遅刻']、['早退']、
            ['遅刻', '早退'] のいずれか

        出力
        ----
        なし。遅刻／早退については、セルに既に残っている時刻情報
        （DM 返信で登録済みの場合）を消さずに保持する。
        """
        with self._lock:
            self._refresh_index()
            if message_id not in self._msgid_to_col or member_id not in self._member_to_row:
                raise SheetTargetNotFoundError(
                    message_id=None if message_id in self._msgid_to_col else message_id,
                    member_id=None if member_id in self._member_to_row else member_id,
                )
            col = self._msgid_to_col[message_id]
            row = self._member_to_row[member_id]

            if not bases:
                new_val = ""
            elif bases[0] in ("出席", "欠席"):
                new_val = bases[0]
            else:
                current_raw = self.ws.cell(row, col).value or ""
                late_leave_tokens = _extract_late_leave_tokens(current_raw)
                parts = []
                if "遅刻" in bases:
                    parts.append(late_leave_tokens.get("遅刻", "遅刻"))
                if "早退" in bases:
                    parts.append(late_leave_tokens.get("早退", "早退"))
                new_val = " ".join(parts)

            self.ws.update_cell(row, col, new_val)

    def update_status(
        self,
        message_id: int,
        member_id: int,
        status_name: str,
    ) -> None:
        """
        1 人・1 練習日分の出欠セルを更新する（DM の時刻返信などから呼ばれる）。

        入力
        ----
        message_id : int
            RSVP 投稿の Discord メッセージ ID（列を特定するキー）
        member_id : int
            更新対象メンバーの Discord ID（行を特定するキー）
        status_name : str
            書き込みたいステータス文字列
            （例: '出席' '欠席' '遅刻(19:00～)' '早退(～20:00)'）

        出力
        ----
        なし（Spread Sheet のセルを直接更新する）

        仕様
        ----
        1. 出席／欠席が来たらそれ 1 個だけをセルに残す。
        2. 遅刻／早退は同時に 1 個ずつまで残す。
           - 同じ種類を押し直したら置き換え
           - 片方だけ来た場合は既存のもう一方を温存
        """
        with self._lock:
            self._refresh_index()
            if message_id not in self._msgid_to_col or member_id not in self._member_to_row:
                raise SheetTargetNotFoundError(
                    message_id=None if message_id in self._msgid_to_col else message_id,
                    member_id=None if member_id in self._member_to_row else member_id,
                )
            col = self._msgid_to_col[message_id]
            row = self._member_to_row[member_id]

            current_raw = self.ws.cell(row, col).value or ""
            late_leave_tokens = _extract_late_leave_tokens(current_raw)

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
                late_leave_tokens[base] = status_name.strip()  # 置き換え or 挿入
                # 片方だけ残っているかもしれないので順序を固定化
                parts = []
                if "遅刻" in late_leave_tokens:
                    parts.append(late_leave_tokens["遅刻"])
                if "早退" in late_leave_tokens:
                    parts.append(late_leave_tokens["早退"])
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
        1 練習日・1 プログラム分の出欠状況をまとめて取得する（draw.py 用）。

        入力
        ----
        message_id : int
            対象の練習日を特定する Discord メッセージ ID
        program : str
            対象プログラム名（例: '前'）

        出力
        ----
        dict[(str, int, str), str]
            {(part, num, name): status_raw} の辞書。
            num がシートに無い場合は 0、status_raw が空欄なら '未回答'。
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
        """
        複数メンバー分の行を一括追加する。

        入力
        ----
        rows : list[list[str]]
            _default_headers() の列順に揃えた行データのリスト

        出力
        ----
        なし（rows が空なら何もしない）
        """
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
        マッピングを列全体として一括更新する（API 呼び出し 1 回）。

        入力
        ----
        message_id : int
            対象の練習日を特定する Discord メッセージ ID
        values_by_member : dict[int, str]
            {Discord ID: 新ステータス文字列('出席'/'欠席'/'遅刻'/'早退'/'遅刻 早退')}

        出力
        ----
        なし（列全体を一括で書き換える）

        注意
        ----
        既存セルに時刻情報（例: "遅刻(19:00～)"）が含まれており、かつ
        新しいステータスと矛盾しない場合は、時刻情報を消さずにマージする
        （マージ本体は _merge_status_with_existing に切り出し）。
        """
        with self._lock:
            self._refresh_index()
            if message_id not in self._msgid_to_col:
                raise KeyError(
                    f"message_id {message_id} が列に登録されていません"
                )

            col = self._msgid_to_col[message_id]
            if not self._member_to_row:
                return

            last_row = max(self._member_to_row.values())
            if last_row < _DATA_START_ROW:
                return

            # 現在の列の値を一括取得（時刻情報の保持用）
            # col_values は 0-index のリスト (row 1 が index 0)
            current_col_values = self.ws.col_values(col)

            write_values = self._build_status_column_values(
                last_row, values_by_member, current_col_values
            )

            col_a1 = self._col_to_a1(col)
            rng = f"{col_a1}{_DATA_START_ROW}:{col_a1}{last_row}"
            self.ws.update(values=write_values, range_name=rng)

    def _build_status_column_values(
        self,
        last_row: int,
        values_by_member: Dict[int, str],
        current_col_values: List[str],
    ) -> List[List[str]]:
        """
        bulk_update_status_column 用：書き込む列データ（2 次元配列）を組み立てる。

        入力
        ----
        last_row : int
            対象範囲の最終行番号
        values_by_member : dict[int, str]
            {Discord ID: 新ステータス文字列}
        current_col_values : list[str]
            更新前の列の値（ws.col_values の戻り値そのまま、0-index）

        出力
        ----
        list[list[str]]
            _DATA_START_ROW 行目から last_row 行目までの、更新後のセル値
            （gspread の update() にそのまま渡せる形）
        """
        write_values: List[List[str]] = [
            [""] for _ in range(_DATA_START_ROW, last_row + 1)
        ]

        for member_id, new_status in values_by_member.items():
            row = self._member_to_row.get(member_id)
            if not row:
                continue

            idx = row - _DATA_START_ROW
            if not (0 <= idx < len(write_values)):
                continue

            old_val = ""
            if (row - 1) < len(current_col_values):
                old_val = str(current_col_values[row - 1]).strip()

            write_values[idx][0] = _merge_status_with_existing(old_val, new_status)

        return write_values

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
        """
        シート内容から 3 種類の検索用インデックスを作り直す。

        入力
        ----
        なし（self.ws の現在の内容を読む）

        出力
        ----
        なし。以下を self に設定する:
        - _col_to_header : {列番号: ヘッダ文字列}
        - _msgid_to_col : {Discord メッセージ ID: 列番号}
        - _member_to_row : {Discord ID: 行番号}
        """
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

        注意: 2 行目は、イベント列が 1 つも無い間は中身が全部空欄になる
        （それ自体が正常な状態）。「2 行目の中身が空かどうか」では
        作成済みかどうかを判定できないため、判定は「1 行目（ヘッダ）が
        まだ無かったかどうか」の 1 回だけで行い、2 行目もそのタイミングで
        まとめて作る（毎回の起動時に誤って空行を追加しないため）。
        """
        heads = _default_headers(self.programs)

        if not any(self.ws.row_values(_HEADER_DATE_ROW)):
            self.ws.insert_row(heads, index=_HEADER_DATE_ROW)
            # ヘッダ長と同数の空セルを用意
            self.ws.insert_row([""] * len(heads), index=_MESSAGE_ID_ROW)
            # 初期化時にシートデザイン（書式）も適用する
            self.apply_design()

    def apply_design(self) -> None:
        """
        シートのデザイン（書式）を適用する。

        セルの値には触れず、色・列幅・非表示・条件付き書式のみを設定する
        （詳細は sheet_design.py を参照）。シート初期化時に自動で呼ばれる
        ほか、デザインを張り直したいときに手動で呼んでもよい（再実行安全）。
        書式は見た目だけの問題なので、失敗しても bot の起動は止めない。
        """
        try:
            apply_design(self.sh, self.ws, len(self.programs))
        except Exception as e:  # noqa: BLE001
            print(f"[sheet_design] デザイン適用に失敗しました: {e}")

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
        乗り番（パート・席次）を 1 人分 Spread Sheet に登録・更新する。

        入力
        ----
        program : str
            対象プログラム名（例: '前'）
        part, num : str, int
            登録するパートと席次
        display_name : str
            Discord 表示名（該当行の discord表示名 列に無条件で反映）
        member_id : int
            Discord ID（行の特定キー）
        overwrite : bool, keyword-only
            既存値と衝突するときに上書きしてよいか

        出力
        ----
        なし。ただし既存値と衝突していて overwrite=False の場合は
        CellOccupiedError を送出する（呼び出し側で確認ダイアログを出す想定）。

        仕様
        ----
        ・discord表示名 は常に最新に上書き
        ・パート／席次に既存値があり、かつ値が変わる場合は
          overwrite=False なら CellOccupiedError を送出
        ・初回登録（欄が空）の場合は衝突ではないので、overwrite の値に
          関わらず常に書き込む
        """
        with self._lock:
            self._refresh_index()
            heads = _default_headers(self.programs)

            # ===== 既存行がある場合 =====
            if member_id in self._member_to_row:
                row = self._member_to_row[member_id]

                disp_col = heads.index("discord表示名") + 1
                part_col = heads.index(f"{program}_パート") + 1
                num_col = heads.index(f"{program}_席次") + 1
                last_col = max(disp_col, part_col, num_col)

                # 対象範囲を 1 回の読み取りでまとめて取得
                row_values = self.ws.get(
                    f"{self._col_to_a1(1)}{row}:{self._col_to_a1(last_col)}{row}"
                )
                row_vals = row_values[0] if row_values else []

                def _cell(col: int) -> str:
                    idx = col - 1
                    return str(row_vals[idx]).strip() if idx < len(row_vals) else ""

                prev_disp = _cell(disp_col)
                prev_part = _cell(part_col)
                prev_num = _cell(num_col)

                has_existing_value = bool(prev_part) or bool(prev_num)
                conflicts = has_existing_value and (
                    prev_part != part or prev_num != str(num)
                )

                if conflicts and not overwrite:
                    if prev_disp != display_name:
                        self.ws.update_cell(row, disp_col, display_name)
                    raise CellOccupiedError(
                        row, program, prev_part, prev_num, part, num
                    )

                # discord表示名は無条件更新、パート・席次は初回登録・衝突なし・overwrite指定のいずれでも書き込む
                updates = [
                    {"range": f"{self._col_to_a1(part_col)}{row}", "values": [[part]]},
                    {"range": f"{self._col_to_a1(num_col)}{row}", "values": [[str(num)]]},
                ]
                if prev_disp != display_name:
                    updates.append(
                        {"range": f"{self._col_to_a1(disp_col)}{row}", "values": [[display_name]]}
                    )
                self.ws.batch_update(updates)

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
    """
    同期 gspread 呼び出しを asyncio から使いやすく包むブリッジ。
    各 *_async メソッドは対応する GoogleSheetsManager の同名メソッドを
    別スレッドで実行するだけで、入力・出力・仕様は元メソッドと同一。
    """

    def __init__(self, gs: GoogleSheetsManager) -> None:
        self.gs = gs

    async def update_status_async(
        self,
        message_id: int,
        member_id: int,
        status_name: str,
    ) -> None:
        """GoogleSheetsManager.update_status を別スレッドで実行する"""
        await asyncio.to_thread(
            self.gs.update_status, message_id, member_id, status_name
        )

    async def attendance_dict_async(
        self,
        message_id: int,
        program: str,
    ) -> Dict[Tuple[str, int, str], str]:
        """GoogleSheetsManager.attendance_dict を別スレッドで実行する"""
        return await asyncio.to_thread(
            self.gs.attendance_dict, message_id, program
        )

    async def add_event_column_async(
        self,
        header_str: str,
        message_id: int,
        event_date_iso: str | None = None,
    ) -> int:
        """GoogleSheetsManager.add_event_column を別スレッドで実行する"""
        return await asyncio.to_thread(
            self.gs.add_event_column, header_str, message_id, event_date_iso
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
        """GoogleSheetsManager.append_member を別スレッドで実行する"""
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
        """GoogleSheetsManager.bulk_update_status_column を別スレッドで実行する"""
        await asyncio.to_thread(
            self.gs.bulk_update_status_column,
            message_id,
            values_by_member,
        )

    async def registered_member_ids_async(self) -> set[int]:
        """GoogleSheetsManager.registered_member_ids を別スレッドで実行する"""
        return await asyncio.to_thread(self.gs.registered_member_ids)

    async def sort_members_by_part_async(
        self, part_order: List[List[str]]
    ) -> int:
        """GoogleSheetsManager.sort_members_by_part を別スレッドで実行する"""
        return await asyncio.to_thread(self.gs.sort_members_by_part, part_order)

    async def set_resolved_bases_async(
        self,
        message_id: int,
        member_id: int,
        bases: List[str],
    ) -> None:
        """GoogleSheetsManager.set_resolved_bases を別スレッドで実行する"""
        await asyncio.to_thread(
            self.gs.set_resolved_bases, message_id, member_id, bases
        )
