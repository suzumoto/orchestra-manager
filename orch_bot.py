# orch_bot.py
from __future__ import annotations

import sys

# Windows コンソール(cp932等)では絵文字・一部の日本語で print が落ちるため、
# 標準出力/エラーは常に UTF-8 として扱う。
sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace", line_buffering=True)
sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace", line_buffering=True)

import os
import asyncio
import re
import json
from pathlib import Path
from datetime import timezone, timedelta, datetime, date, time as dtime
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
from gspread.exceptions import GSpreadException
from gspread.exceptions import APIError

load_dotenv()

from draw import PlayerBoxDrawer, BLACK
from seat_layout_slides import SeatLayoutSlides
from sheet import (
    GoogleSheetsManager,
    SheetAsyncBridge,
    CellOccupiedError,
    SheetTargetNotFoundError,
    _base_status,
    _default_headers,
)

# ------------------------------------------------------------
# 設定読み込み
# ------------------------------------------------------------
import configparser

config = configparser.ConfigParser()
config.read("settings.ini", encoding="utf-8")

BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("環境変数 DISCORD_BOT_TOKEN が未設定です。")

COMMAND_CHANNELS = {v for v in config["COMMAND_CHANNEL"].values()}
OUTPUT_CHANNEL = config["OUTPUT_CHANNEL"]["output_channel"]

# RSVP チャンネル → シートのマッピングを構築
RSVP_CHANNELS_ENSOU: set[str] = set()
RSVP_CHANNELS_BUNSOU: set[str] = set()
for k, v in config["RSVP_CHANNEL"].items():
    if k.endswith("_1"):
        RSVP_CHANNELS_ENSOU.add(v)
    elif k.endswith("_2"):
        RSVP_CHANNELS_BUNSOU.add(v)
RSVP_CHANNELS: set[str] = RSVP_CHANNELS_ENSOU | RSVP_CHANNELS_BUNSOU

EMOJI_NAME = {
    "出席": config["EMOJI"]["shusseki"],
    "欠席": config["EMOJI"]["kesseki"],
    "遅刻": config["EMOJI"]["chikoku"],
    "早退": config["EMOJI"]["soutai"],
    "出力": config["EMOJI"]["output"],
    "DM": config["EMOJI"]["dm"],
}

_ROLE_PATTERNS: list[tuple[re.Pattern[str], str]] = []

if "PART_ROLE" not in config:
    raise RuntimeError(
        "settings.ini に [PART_ROLE] セクションが見つかりません。"
    )

for part, regex_str in config["PART_ROLE"].items():
    for pat in regex_str.split("|"):
        pat = pat.strip()
        if not pat:
            continue
        _ROLE_PATTERNS.append((re.compile(pat, re.I), part))

OUTPUT_ROLES = {v for v in config["ROLE"].values()}

PROGRAMS = [v for _, v in config["PROGRAM"].items()]

CHECKMARK_EMOJI = "✅"
CANCEL_EMOJI = "🆖"
_WEEKDAYS_JP = "月火水木金土日"

# JST (UTC+9)
JST = timezone(timedelta(hours=9))

# ---------------- リマインダー設定 -------------------------
def _parse_hhmm(s: str) -> dtime:
    h, m = s.strip().split(":")
    return dtime(int(h), int(m), tzinfo=JST)

_rem_cfg = config["REMINDER"] if "REMINDER" in config else {}
REMIND_DAYS_BEFORE = [
    int(x) for x in _rem_cfg.get("remind_days_before", "3,1").split(",")
]
REMIND_TIME = _parse_hhmm(_rem_cfg.get("remind_time", "19:00"))
OUTPUT_TIME = _parse_hhmm(_rem_cfg.get("output_time", "07:00"))
REMINDER_MENTION_ROLE = _rem_cfg.get("mention_role", "運営")
REMINDER_SCAN_LIMIT = int(_rem_cfg.get("scan_limit", "30"))

# ---------------- メンバー同期設定 -------------------------
# sync_role が設定されていれば、そのロール保持者のみをシートへ登録する
# （空なら従来どおりサーバーの全メンバーが対象）
_member_cfg = config["MEMBER"] if "MEMBER" in config else {}
MEMBER_SYNC_ROLE = _member_cfg.get("sync_role", "").strip()


def _parse_part_order(raw: str) -> list[list[str]]:
    """'Fl/Picc, Ob/EHr, ...' 形式を別名グループのリストに変換する"""
    groups: list[list[str]] = []
    for grp in raw.split(","):
        aliases = [a.strip() for a in grp.split("/") if a.strip()]
        if aliases:
            groups.append(aliases)
    return groups


# シートの行並べ替えに使うパート順（'/' 区切りは同順位の別名）
PART_SORT_ORDER = _parse_part_order(_member_cfg.get(
    "part_order",
    "Fl/Picc, Ob/EHr, Cl/B.Cl, Fg/C.Fg, Hr, Tp, Tb/Tuba, "
    "Timp, Perc, Vn/Vn1st/Vn2nd, Va, Vc, Cb",
))

# ---------------- Sheets / Slides ---------------------------
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
if not SPREADSHEET_ID:
    raise RuntimeError("環境変数 SPREADSHEET_ID が未設定です。")

# シート名の設定（デフォルト: 全奏 / 分奏）
ENSOU_SHEET_NAME = config["SPREADSHEET"].get(
    "zensou_worksheet_name",
    config["SPREADSHEET"].get("worksheet_name", "全奏"),
)
BUNSOU_SHEET_NAME = config["SPREADSHEET"].get("bunsou_worksheet_name", "分奏")

# 全奏 / 分奏 それぞれのマネージャとブリッジ
gs_manager_ensou = GoogleSheetsManager(
    spreadsheet_id=SPREADSHEET_ID,
    worksheet_name=ENSOU_SHEET_NAME,
    credential_json=config["SPREADSHEET"].get("credential_json", "credentials.json"),
    programs=PROGRAMS,
)
gs_manager_bunsou = GoogleSheetsManager(
    spreadsheet_id=SPREADSHEET_ID,
    worksheet_name=BUNSOU_SHEET_NAME,
    credential_json=config["SPREADSHEET"].get("credential_json", "credentials.json"),
    programs=PROGRAMS,
)
sheet_bridge_ensou = SheetAsyncBridge(gs_manager_ensou)
sheet_bridge_bunsou = SheetAsyncBridge(gs_manager_bunsou)

# シートキーの定義
SHEET_KEY_ENSOU = "ensou"
SHEET_KEY_BUNSOU = "bunsou"


def _bridge_for_sheet_key(sheet_key: str) -> SheetAsyncBridge:
    if sheet_key == SHEET_KEY_ENSOU:
        return sheet_bridge_ensou
    if sheet_key == SHEET_KEY_BUNSOU:
        return sheet_bridge_bunsou
    raise ValueError(f"unknown sheet_key: {sheet_key}")


def _sheet_key_for_message(msg: discord.Message) -> str | None:
    """
    投稿内容から対応シート（全奏/分奏）を決める。

    1 つのカレンダーチャンネルに全奏/分奏の投稿が混在する運用
    （例: ザムスターク管弦楽団の #📅カレンダー）に対応するため、
    日付行に『分奏』『全奏』のキーワードがあればそれを優先する。
    キーワードが無い場合は、チャンネル名と全奏/分奏の対応が一意に
    決まる場合のみ従来どおりチャンネル名で振り分ける
    （settings.ini で同じチャンネルを両方に登録している場合は
    振り分け不能 = None とし、トップ練・連絡事項などは対象外にする）。
    """
    line = _find_date_line(msg.content or "")
    if "分奏" in line:
        return SHEET_KEY_BUNSOU
    if "全奏" in line:
        return SHEET_KEY_ENSOU

    ch_name = str(msg.channel)
    in_ensou = ch_name in RSVP_CHANNELS_ENSOU
    in_bunsou = ch_name in RSVP_CHANNELS_BUNSOU
    if in_ensou and not in_bunsou:
        return SHEET_KEY_ENSOU
    if in_bunsou and not in_ensou:
        return SHEET_KEY_BUNSOU
    return None


PRESENTATION_ID = os.getenv("SLIDES_PRESENTATION_ID")
if not PRESENTATION_ID:
    raise RuntimeError("環境変数 SLIDES_PRESENTATION_ID が未設定です。")

layout = SeatLayoutSlides(
    presentation_id=PRESENTATION_ID,
    credential_json=config["SLIDES"].get("credential_json", "credentials.json"),
    slide_index=int(config["SLIDES"].get("slide_index", 1)),
)
LEGEND_COLOR = {
    label: (info["fill"], info["font"])
    for label, info in layout.legends.items()
}

# ------------------------------------------------------------
# Discord Bot
# ------------------------------------------------------------
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="$", intents=intents)


@bot.check
async def _restrict_commands_to_command_channel(ctx: commands.Context) -> bool:
    """
    コマンドは COMMAND_CHANNELS（例: 出欠管理システム）以外では受け付けない。
    DM では ctx.channel は DMChannel になり str() が想定と異なる形になるため、
    guild 内のテキストチャンネルでの実行に限定する。
    """
    return ctx.guild is not None and str(ctx.channel) in COMMAND_CHANNELS


# ------------------------------------------------------------
# [修正] 絵文字判定ロジック
# ------------------------------------------------------------

def _extract_emoji_name_from_tag(text: str) -> str:
    """
    設定ファイル等の文字列が <:name:id> 形式なら name を返す。
    それ以外（Unicode絵文字や単純な文字列）ならそのまま返す。
    """
    # <a:name:id> (アニメーション) または <:name:id> (通常) に対応
    m = re.match(r"<a?:([^:]+):\d+>", text)
    if m:
        return m.group(1)
    return text


def status_from_emoji(emoji_name: str) -> str | None:
    """
    リアクションの絵文字名が、設定ファイルのどのステータスに該当するか判定する。
    設定値が <:shusseki:123...> の形式でも、名前(shusseki)で比較を行う。
    """
    for status, ename in EMOJI_NAME.items():
        # 設定値(ename)から名前部分だけを抽出
        config_name = _extract_emoji_name_from_tag(ename)
        
        # emoji_name は _norm_emoji 済み前提 (VS16除去済み)
        if emoji_name == config_name and status in {"出席", "欠席", "遅刻", "早退"}:
            return status
    return None


# ------------------------------------------------------------
# ユーティリティ群
# ------------------------------------------------------------
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")  # HH:MM 24h


def _norm_emoji(txt: str) -> str:
    """絵文字文字列から VS-16 (U+FE0F) を除去して正規化"""
    return txt.replace("\uFE0F", "")


def _valid_time(text: str) -> bool:
    """入力文字列が HH:MM (24時間制) 形式かどうかを判定する"""
    return bool(_TIME_RE.match(text.strip()))


# ------------------------------------------------------------
# Part 名の正規化ユーティリティ
# ------------------------------------------------------------
def _canon_part(part: str | None) -> str | None:
    """
    パート表記の正規化。
    - 英字の連続セグメントごとに「先頭大文字＋残り小文字」
    - 英数字以外（例: &, -, /, 空白）でセグメントを区切る（記号自体は保持）
    例:
      'vn1st'   -> 'Vn1st'
      'PF&cel'  -> 'Pf&Cel'
    """
    if part is None:
        return None
    s = part.strip()
    out: list[str] = []
    new_seg = True
    for ch in s:
        if ch.isalpha():
            out.append(ch.upper() if new_seg else ch.lower())
            new_seg = False
        else:
            out.append(ch)
            new_seg = not ch.isalnum()
    return "".join(out)


async def _notify_sheet_target_missing(
    guild: discord.Guild | None,
    *,
    member_display: str,
    message_id: int,
    exc: Exception,
) -> None:
    """
    シート未登録（SheetTargetNotFoundError）を検知した際の共通処理。
    print ログ + コマンドチャンネルへの 1 行通知（見つからなければログのみ）。
    """
    print(
        f"[sheet] シート未登録のためリアクションを無視しました: "
        f"{member_display} (msg={message_id}) detail={exc}"
    )
    if guild is None:
        return
    ch_name = next(iter(COMMAND_CHANNELS), None)
    if ch_name is None:
        return
    channel = discord.utils.get(guild.text_channels, name=ch_name)
    if channel is None:
        return
    try:
        await channel.send(
            f"⚠️ シート未登録のためリアクションを無視しました: "
            f"{member_display} (msg={message_id})"
        )
    except discord.HTTPException as send_exc:
        print(f"[sheet] コマンドチャンネルへの通知に失敗: {send_exc}")


_STATUS_KEY_TO_LABEL = {"late": "遅刻", "leave": "早退"}
_STATUS_LABEL_TO_KEY = {"遅刻": "late", "早退": "leave"}


class TimeInputModal(discord.ui.Modal):
    """遅刻／早退の時刻を入力させる Modal"""

    def __init__(
        self,
        *,
        sheet_key: str,
        server_message_id: int,
        member_id: int,
        status: str,
    ) -> None:
        super().__init__(title=f"{status}時刻の入力")
        self.sheet_key = sheet_key
        self.server_message_id = server_message_id
        self.member_id = member_id
        self.status = status

        verb = "到着" if status == "遅刻" else "退出"
        self.time_input = discord.ui.TextInput(
            label=f"{verb}予定時刻",
            placeholder="19:30",
            max_length=5,
        )
        self.add_item(self.time_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        time_str = self.time_input.value.strip()
        if not _valid_time(time_str):
            await interaction.response.send_message(
                "❌ 形式が正しくありませんでした。HH:MM（24時間制）で入力し直してください。",
                ephemeral=True,
            )
            return

        cell_value = (
            f"遅刻({time_str}～)" if self.status == "遅刻" else f"早退(～{time_str})"
        )

        await interaction.response.defer(ephemeral=True)

        bridge = _bridge_for_sheet_key(self.sheet_key)
        try:
            await bridge.update_status_async(
                self.server_message_id, self.member_id, cell_value
            )
        except SheetTargetNotFoundError as exc:
            member_display = (
                interaction.guild.get_member(self.member_id).display_name
                if interaction.guild and interaction.guild.get_member(self.member_id)
                else str(self.member_id)
            )
            await _notify_sheet_target_missing(
                interaction.guild,
                member_display=member_display,
                message_id=self.server_message_id,
                exc=exc,
            )
            await interaction.followup.send(
                "❌ シート未登録のため登録できませんでした。", ephemeral=True
            )
            return

        await interaction.followup.send(
            f"✅ {time_str} で登録しました", ephemeral=True
        )


class TimeInputButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"rsvp_time:(?P<sheet_key>ensou|bunsou):(?P<message_id>[0-9]+):(?P<member_id>[0-9]+):(?P<status_key>late|leave)",
):
    """
    再起動をまたいで機能する、遅刻／早退の時刻入力ボタン。
    custom_id にシート・メッセージ・対象メンバー・ステータスを埋め込むことで
    prompt_context.json のような別途永続化ファイルを不要にする。
    """

    def __init__(
        self, sheet_key: str, message_id: int, member_id: int, status_key: str
    ) -> None:
        super().__init__(
            discord.ui.Button(
                label="時刻を入力",
                emoji="⏰",
                style=discord.ButtonStyle.primary,
                custom_id=f"rsvp_time:{sheet_key}:{message_id}:{member_id}:{status_key}",
            )
        )
        self.sheet_key = sheet_key
        self.message_id = message_id
        self.member_id = member_id
        self.status_key = status_key

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
        /,
    ) -> "TimeInputButton":
        return cls(
            match["sheet_key"],
            int(match["message_id"]),
            int(match["member_id"]),
            match["status_key"],
        )

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        if interaction.user.id != self.member_id:
            target_display = (
                interaction.guild.get_member(self.member_id).display_name
                if interaction.guild and interaction.guild.get_member(self.member_id)
                else str(self.member_id)
            )
            await interaction.response.send_message(
                f"このボタンは {target_display} さん専用です", ephemeral=True
            )
            return

        status = _STATUS_KEY_TO_LABEL[self.status_key]
        await interaction.response.send_modal(
            TimeInputModal(
                sheet_key=self.sheet_key,
                server_message_id=self.message_id,
                member_id=self.member_id,
                status=status,
            )
        )


async def _post_time_input_button(
    *,
    msg: discord.Message,
    member: discord.Member,
    server_message_id: int,
    status: str,
    sheet_key: str,
) -> None:
    """
    遅刻／早退した member 宛に、RSVP 投稿に紐づくスレッドへ
    時刻入力ボタン付きメッセージを投稿する。

    入力
    ----
    msg : discord.Message
        元の RSVP 投稿（スレッドの取得・作成に使う）
    member : discord.Member
        時刻入力対象のメンバー
    server_message_id : int
        元の RSVP メッセージ ID（Sheets 更新のキー）
    status : str
        '遅刻' または '早退'
    sheet_key : str
        書き込み先シートの選択キー

    出力
    ----
    なし（スレッド投稿失敗時は print ログのみ。Bot は落とさない）
    """
    practice_date = _parse_date_from_msg(msg)
    practice_date_d = (
        practice_date.date() if practice_date else msg.created_at.astimezone(JST).date()
    )

    try:
        thread = await _get_or_create_reminder_thread(msg, practice_date_d)
    except discord.HTTPException as exc:
        print(f"[time-input] スレッド取得/作成に失敗: {exc}")
        return

    verb = "到着" if status == "遅刻" else "退出"
    date_str_jp = _format_date_jp(msg, with_weekday=False)
    content = (
        f"⏰ {member.mention} さん、{date_str_jp}の練習に{status}の予定ですね。"
        f"下のボタンから{verb}予定時刻を入力してください。"
    )
    view = discord.ui.View(timeout=None)
    view.add_item(
        TimeInputButton(
            sheet_key,
            server_message_id,
            member.id,
            _STATUS_LABEL_TO_KEY[status],
        )
    )

    try:
        await thread.send(content, view=view)
    except discord.HTTPException as exc:
        print(f"[time-input] スレッドへのボタン投稿に失敗: {exc}")


async def _setup_hook() -> None:
    """
    Bot 起動時に一度だけ実行される setup_hook。
    再起動をまたいで機能する DynamicItem をここで登録する
    （on_ready は複数回発火しうるため、登録場所として不適）。
    """
    bot.add_dynamic_items(TimeInputButton)


bot.setup_hook = _setup_hook


# ------------------------------------------------------------
# 出欠リアクションの「付けた順」スタックの永続化
# ------------------------------------------------------------
# {(message_id, member_id): [現在有効なステータスを追加順に並べたリスト]}
# Bot 稼働中に観測した add/remove イベントのみを反映する。再起動を挟むと
# 該当メッセージ・メンバーの順序情報は失われるため、その場合は
# _reconcile_and_resolve() が固定優先順位ルールにフォールバックする。
_REACTION_ORDER_FILE = Path("reaction_order.json")


def _load_reaction_order() -> dict[tuple[int, int], list[str]]:
    """JSON から復元。存在しない・壊れていれば空 dict"""
    try:
        data = json.loads(_REACTION_ORDER_FILE.read_text(encoding="utf-8"))
        order: dict[tuple[int, int], list[str]] = {}
        for key, statuses in data.items():
            msg_id_str, member_id_str = key.split(":")
            order[(int(msg_id_str), int(member_id_str))] = list(statuses)
        return order
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def _save_reaction_order(order: dict[tuple[int, int], list[str]]) -> None:
    """dict を JSON へ保存"""
    tmp = {
        f"{msg_id}:{member_id}": statuses
        for (msg_id, member_id), statuses in order.items()
    }
    _REACTION_ORDER_FILE.write_text(
        json.dumps(tmp, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ============================================================
# ロール名からパート推定
# ============================================================
def _detect_part_from_roles(member: discord.Member) -> str | None:
    """Discord Member のロールからパート名を推定。該当無しなら None"""
    for pattern, part in _ROLE_PATTERNS:
        if any(pattern.match(role.name) for role in member.roles):
            return _canon_part(part)
    return None


# ============================================================
# ギルドメンバー → SpreadSheet 同期（呼び出し側が指定したシート1つ分）
# ============================================================
async def _sync_members_to_sheet(
    guild: discord.Guild,
    gs: GoogleSheetsManager,
) -> tuple[int, int]:
    """
    既存行は変更せず、新規メンバーだけを追加する。
    Returns
    -------
    added_with_part : int  … パート取得成功で追加した人数
    added_no_part   : int  … パート不明で追加した人数
    """
    added_with_part = 0
    added_no_part = 0
    rows_to_append: list[list[str]] = []

    heads = _default_headers(gs.programs)

    def _build_row(member: discord.Member, part_norm: str | None) -> list[str]:
        row = [""] * len(heads)
        row[heads.index("discord表示名")] = member.display_name
        row[heads.index("氏名")] = ""
        row[heads.index("Discord ID")] = str(member.id)
        if part_norm:
            for prog in gs.programs:
                row[heads.index(f"{prog}_パート")] = part_norm
        return row

    # sync_role が設定されていればロール保持者のみを対象にする
    if MEMBER_SYNC_ROLE:
        sync_role = discord.utils.get(guild.roles, name=MEMBER_SYNC_ROLE)
        if sync_role is None:
            print(
                f"[member-sync] ロール『{MEMBER_SYNC_ROLE}』が "
                f"{guild.name} に見つからないため、メンバー同期をスキップします"
            )
            return 0, 0
        target_members = sync_role.members
    else:
        target_members = guild.members

    for m in target_members:
        if m.bot or m.id in gs._member_to_row:
            continue
        part_norm = _detect_part_from_roles(m)
        rows_to_append.append(_build_row(m, part_norm))
        if part_norm:
            added_with_part += 1
        else:
            added_no_part += 1

    await asyncio.to_thread(gs.append_rows_bulk, rows_to_append)
    return added_with_part, added_no_part


async def _sort_and_realign_sheets() -> tuple[int, int]:
    """
    全奏シートをパート順に並べ替え、分奏シートのイベント列を
    全奏の新しい行順に追従させる。

    分奏の固定列 A〜I は ARRAYFORMULA で全奏を参照しているため、
    メンバー情報の並びは全奏の並べ替えに自動追従する。一方で
    分奏のイベント列（出欠データ）は静的な値なので、Discord ID で
    対応づけて同じ並びに書き直す（realign_event_rows）。

    Returns
    -------
    (全奏で並べ替えた行数, 分奏で追従させたイベント行数)
    """
    n_sorted = await sheet_bridge_ensou.sort_members_by_part_async(PART_SORT_ORDER)
    id_order = await sheet_bridge_ensou.member_id_order_async()
    n_realigned = await sheet_bridge_bunsou.realign_event_rows_async(id_order)
    return n_sorted, n_realigned


# (message_id, member_id) → 現在有効な出欠リアクションの追加順リスト
_REACTION_ORDER: dict[tuple[int, int], list[str]] = _load_reaction_order()


# ============================================================
# Bot 起動時に自動同期
# ============================================================
_SYNC_DONE_ON_STARTUP = False

# 1 メッセージの同期処理結果を表す 3 状態
_SYNC_STOP = "stop"      # これより古い投稿はもう見なくてよい
_SYNC_SYNCED = "synced"  # シートへの反映が完了した
_SYNC_SKIP = "skip"      # 対象外だった、またはエラーで今回はスキップ


async def _sync_message_if_recent(
    msg: discord.Message,
    today: date,
) -> str:
    """
    1 件の RSVP 投稿を判定し、必要ならシートへ同期する。

    入力
    ----
    msg : discord.Message
        判定対象の投稿（RSVP チャンネルの履歴から 1 件）
    today : date
        JST での「今日」の日付（過去投稿の判定基準）

    書き込み先シートは投稿内容（全奏/分奏キーワード）から決定する。
    振り分けられない投稿（トップ練・連絡事項など）はスキップする。

    出力
    ----
    str
        _SYNC_STOP  : この投稿が過去の練習日だった
                      （呼び出し側はこれより古い履歴の走査を打ち切ってよい）
        _SYNC_SYNCED: シートへの反映が完了した
        _SYNC_SKIP  : Bot 投稿／リアクション無し／API エラーで今回は何もしなかった
    """
    if msg.author.bot:
        return _SYNC_SKIP

    if len(msg.reactions) == 0:  # ただの連絡事項とみなす
        return _SYNC_SKIP

    sheet_key = _sheet_key_for_message(msg)
    if sheet_key is None:  # 全奏/分奏いずれでもない投稿は対象外
        return _SYNC_SKIP
    bridge = _bridge_for_sheet_key(sheet_key)

    dt = _parse_date_from_msg(msg)
    if dt and not _is_undecided(msg) and dt.date() < today:
        print(f"    [SKIP] Found past event {dt.date()} (msg {msg.id}). Stopping history scan.")
        return _SYNC_STOP

    header_str = _get_header_from_msg(msg)
    print(f"    Syncing msg {msg.id} -> Header: {header_str}")

    try:
        await bridge.add_event_column_async(
            header_str, msg.id, _get_event_date_iso(msg)
        )
        values_by_member = await _collect_attendance_from_reactions(msg)
        await bridge.bulk_update_status_column_async(msg.id, values_by_member)

        # API制限(429 Quota exceeded)回避のため、1件処理するごとに5秒待機
        print("        ...Waiting 5s for API limits...")
        await asyncio.sleep(5.0)
        return _SYNC_SYNCED

    except APIError as e:
        # 万が一制限に達してもBotごと落ちないようにキャッチしてスキップ/待機
        print(f"    ⚠️ API Error on msg {msg.id}: {e}")
        print("    Waiting 30s before retrying next message...")
        await asyncio.sleep(30.0)
        return _SYNC_SKIP


async def _sync_latest_rsvp_in_guild(guild: discord.Guild) -> int:
    """
    RSVPチャンネルの履歴(最新20件)から、1行目に日付がある投稿を同期する。

    入力
    ----
    guild : discord.Guild
        走査対象のギルド

    出力
    ----
    int
        シートへ反映した投稿の件数
    """
    synced_count = 0

    JST = timezone(timedelta(hours=9))
    today = datetime.now(JST).date()

    for ch_name in RSVP_CHANNELS:
        channel = discord.utils.get(guild.text_channels, name=ch_name)
        if not channel:
            continue

        print(f"--- Scanning channel: {ch_name} ---")

        async for msg in channel.history(limit=20):
            result = await _sync_message_if_recent(msg, today)
            if result == _SYNC_STOP:
                break
            if result == _SYNC_SYNCED:
                synced_count += 1

    return synced_count

async def _startup_sync() -> None:
    """
    ギルドごとにメンバー同期（全奏のみ）→ RSVP 同期を直列に実行する。
    API クォータ保護のため並行実行はしない。on_ready をブロックしないよう
    バックグラウンドタスクとして呼び出される想定。
    """
    for g in bot.guilds:
        print(f"Syncing members for guild: {g.name}...")
        # メンバー情報のマスターは全奏シート。分奏の固定列は
        # ARRAYFORMULA による全奏参照なので直接書き込まない。
        try:
            added = await _sync_members_to_sheet(g, gs_manager_ensou)
            if any(added):
                await _sort_and_realign_sheets()
        except Exception as exc:
            print(f"[startup-sync] member sync error ({g.name}): {exc}")

        print(f"Syncing latest RSVP for guild: {g.name}...")
        try:
            await _sync_latest_rsvp_in_guild(g)
        except Exception as exc:
            print(f"[startup-sync] RSVP sync error ({g.name}): {exc}")


# ============================================================
# イベントハンドラ
# ============================================================
@bot.event
async def on_ready() -> None:  # type: ignore[override]
    """
    Bot 起動完了時に一度だけ、参加中の各ギルドについて
    メンバー同期と最新 RSVP 同期のバックグラウンドタスクを起動する。
    """
    global _SYNC_DONE_ON_STARTUP

    print(f"Logged in as {bot.user} (id={bot.user.id})")

    if not _SYNC_DONE_ON_STARTUP:
        asyncio.create_task(_startup_sync())
        _SYNC_DONE_ON_STARTUP = True
        print("Startup sync initiated.")

    # 定期ループの起動（再接続時の多重起動を防ぐ）
    if not reminder_loop.is_running():
        reminder_loop.start()
    if not daily_output_loop.is_running():
        daily_output_loop.start()


async def _handle_rsvp_post(message: discord.Message) -> None:
    """
    RSVP チャンネルへの新規投稿を処理する：出欠絵文字を自動で付与し、
    Sheets 側にまだ列が無ければ練習日列を追加する。

    入力
    ----
    message : discord.Message
        RSVP チャンネルに投稿されたメッセージ

    出力
    ----
    なし（Discord へのリアクション付与・Sheets への列追加を行う）。
    全奏/分奏いずれにも振り分けられない投稿（トップ練・連絡事項など）は
    出欠管理の対象外なので、リアクションも付けずに何もしない。
    """
    sheet_key = _sheet_key_for_message(message)
    if sheet_key is None:
        return

    for key in ("出席", "欠席", "遅刻", "早退"):
        emoji = discord.utils.get(message.guild.emojis, name=EMOJI_NAME[key])
        if emoji:
            await message.add_reaction(emoji)

    # まだ Sheets にメッセージ ID 未登録なら列を追加して登録
    header_str = _get_header_from_msg(message)
    bridge = _bridge_for_sheet_key(sheet_key)
    await bridge.add_event_column_async(
        header_str, message.id, _get_event_date_iso(message)
    )


# ============================================================
# 未回答リマインド
# ============================================================
def _is_undecided(msg: discord.Message) -> bool:
    """日付行が『mm月DD日(未定)』形式かどうか"""
    return bool(_DATE_RE_JP_UNDECIDED.search(_find_date_line(msg.content or "")))


async def _find_rsvp_posts_for_date(
    guild: discord.Guild, target: date
) -> list[tuple[discord.Message, str]]:
    """
    全 RSVP チャンネルを走査し、練習日が target と一致する投稿を返す。
    戻り値は (message, sheet_key) のリスト。
    """
    results: list[tuple[discord.Message, str]] = []
    for ch_name in RSVP_CHANNELS:
        channel = discord.utils.get(guild.text_channels, name=ch_name)
        if channel is None:
            continue
        try:
            async for msg in channel.history(limit=REMINDER_SCAN_LIMIT):
                if msg.author.bot:
                    continue
                if _is_undecided(msg):
                    continue
                sheet_key = _sheet_key_for_message(msg)
                if sheet_key is None:  # トップ練・連絡事項などは対象外
                    continue
                dt = _parse_date_from_msg(msg)
                if dt is not None and dt.date() == target:
                    results.append((msg, sheet_key))
        except discord.HTTPException as exc:
            print(f"[reminder] history scan failed in {ch_name}: {exc}")
    return results


async def _compute_non_responders(
    msg: discord.Message, sheet_key: str
) -> list[discord.Member]:
    """シート登録メンバーのうち、msg に出欠リアクションをしていない人を返す"""
    bridge = _bridge_for_sheet_key(sheet_key)
    registered = await bridge.registered_member_ids_async()
    reacted = set(await _collect_attendance_from_reactions(msg))
    members: list[discord.Member] = []
    for uid in registered - reacted:
        m = msg.guild.get_member(uid)
        if m is None:
            try:
                m = await msg.guild.fetch_member(uid)
            except discord.HTTPException:
                continue  # 退会済み等は無視
        if not m.bot:
            members.append(m)
    return members


def _reminder_thread_name(practice_date: date) -> str:
    return f"出欠リマインド {practice_date.month}月{practice_date.day}日"


def _legacy_reminder_thread_name(practice_date: date) -> str:
    """旧フォーマット（ゼロ埋め）のスレッド名。既存スレッドの検索用"""
    return f"出欠リマインド {practice_date.month:02d}月{practice_date.day:02d}日"


async def _get_or_create_reminder_thread(
    msg: discord.Message, practice_date: date
) -> discord.Thread:
    channel = msg.channel
    name = _reminder_thread_name(practice_date)
    names = {name, _legacy_reminder_thread_name(practice_date)}

    # 1) アクティブスレッドから名前で検索
    for th in channel.threads:
        if th.name in names:
            return th
    # 2) アーカイブ済みプライベートスレッドも検索（ベストエフォート）
    try:
        async for th in channel.archived_threads(private=True, limit=50):
            if th.name in names:
                return th
    except discord.HTTPException:
        pass
    # 3) 新規作成（プライベート → 不可ならメッセージ上のパブリックにフォールバック）
    try:
        return await channel.create_thread(
            name=name,
            type=discord.ChannelType.private_thread,
            invitable=True,
            auto_archive_duration=4320,
        )
    except discord.HTTPException as exc:
        print(f"[reminder] private thread failed ({exc}); falling back to public")
        return await msg.create_thread(name=name, auto_archive_duration=4320)


async def _already_reminded_today(thread: discord.Thread) -> bool:
    """今日(JST)すでに bot がこのスレッドにリマインドを投稿済みか"""
    today = datetime.now(JST).date()
    try:
        async for m in thread.history(limit=10):
            if m.author.id == bot.user.id and m.created_at.astimezone(JST).date() == today:
                return True
    except discord.HTTPException:
        pass
    return False


async def _send_reminder_for_post(
    msg: discord.Message, sheet_key: str, practice_date: date, days_before: int
) -> None:
    # シートで列が非表示 = 練習中止のマーカー。リマインドしない
    if await _bridge_for_sheet_key(sheet_key).is_event_hidden_async(msg.id):
        print(f"[reminder] {practice_date} は列非表示（中止扱い）のためスキップ (msg={msg.id})")
        return

    non_responders = await _compute_non_responders(msg, sheet_key)
    if not non_responders:
        print(f"[reminder] no non-responders for {practice_date} in #{msg.channel}")
        return

    thread = await _get_or_create_reminder_thread(msg, practice_date)
    if await _already_reminded_today(thread):
        print(f"[reminder] already sent today in {thread.name}; skip")
        return

    role = discord.utils.get(msg.guild.roles, name=REMINDER_MENTION_ROLE)
    role_mention = role.mention if role else f"@{REMINDER_MENTION_ROLE}"
    mentions = " ".join(m.mention for m in non_responders)
    label = "前日" if days_before == 1 else f"{days_before}日前"

    content = (
        f"📢 **出欠リマインド（練習{label}）**\n"
        f"{practice_date.month}月{practice_date.day}日の練習の出欠が未回答です。\n"
        f"こちらの投稿にリアクションで回答してください → {msg.jump_url}\n"
        f"(Cc: {role_mention})\n\n"
        f"{mentions}"
    )
    await thread.send(
        content,
        allowed_mentions=discord.AllowedMentions(
            users=True, roles=True, everyone=False
        ),
    )
    print(f"[reminder] sent to {thread.name} ({len(non_responders)} members)")


async def _run_reminder_job(days_list: list[int] | None = None) -> None:
    """days_list の各オフセット（例 [3,1]）についてリマインドを実行"""
    days_list = days_list or REMIND_DAYS_BEFORE
    today = datetime.now(JST).date()
    for guild in bot.guilds:
        for days in days_list:
            target = today + timedelta(days=days)
            try:
                posts = await _find_rsvp_posts_for_date(guild, target)
            except Exception as exc:
                print(f"[reminder] scan error ({guild.name}, +{days}d): {exc}")
                continue
            for msg, sheet_key in posts:
                try:
                    await _send_reminder_for_post(msg, sheet_key, target, days)
                except Exception as exc:
                    print(f"[reminder] send error (msg={msg.id}): {exc}")


async def _run_daily_output_job() -> None:
    """今日が練習日の投稿すべてについて出欠表を出力チャンネルへ送る"""
    today = datetime.now(JST).date()
    for guild in bot.guilds:
        try:
            posts = await _find_rsvp_posts_for_date(guild, today)
        except Exception as exc:
            print(f"[daily-output] scan error ({guild.name}): {exc}")
            continue
        for msg, sheet_key in posts:
            try:
                bridge = _bridge_for_sheet_key(sheet_key)
                # シートで列が非表示 = 練習中止のマーカー。出力しない
                if await bridge.is_event_hidden_async(msg.id):
                    print(f"[daily-output] msg={msg.id} は列非表示（中止扱い）のためスキップ")
                    continue
                await _send_attendance_charts(
                    guild=guild,
                    channel=msg.channel,
                    message_id=msg.id,
                    bridge=bridge,
                    send_mode="channel",
                    member=None,
                )
                print(f"[daily-output] charts sent for msg={msg.id}")
            except Exception as exc:
                print(f"[daily-output] output error (msg={msg.id}): {exc}")


@tasks.loop(time=REMIND_TIME)
async def reminder_loop() -> None:
    try:
        await _run_reminder_job()
    except Exception as exc:
        # 例外を漏らすと loop 自体が止まるため必ず握りつぶしてログ
        print(f"[reminder] loop error: {exc}")


@reminder_loop.before_loop
async def _before_reminder_loop() -> None:
    await bot.wait_until_ready()


@tasks.loop(time=OUTPUT_TIME)
async def daily_output_loop() -> None:
    try:
        await _run_daily_output_job()
    except Exception as exc:
        print(f"[daily-output] loop error: {exc}")


@daily_output_loop.before_loop
async def _before_daily_output_loop() -> None:
    await bot.wait_until_ready()


async def _handle_dm_message(message: discord.Message) -> None:
    """
    DM チャンネルで届いたメッセージを処理する。
    Bot 自身の DM 送信は無視し、それ以外には一律で案内を返す
    （遅刻／早退の時刻登録は RSVP スレッド上のボタン／Modal 経由に一本化された）。

    入力
    ----
    message : discord.Message
        DM チャンネル（message.guild is None）で受信したメッセージ

    出力
    ----
    なし
    """
    if message.author.bot:  # Bot が送った DM には反応しない（無限ループ防止）
        return

    await message.channel.send(
        "このDMには自動対応していません。出欠はカレンダーチャンネルのリアクションで回答してください。"
    )


@bot.event
async def on_message(message: discord.Message) -> None:
    """
    全メッセージ受信時のエントリポイント。
    RSVP 投稿への自動リアクション付与、コマンド処理、DM 対応の
    3 つの関心事をそれぞれ専用関数に委譲するだけの orchestrator。
    """
    ch_name = str(message.channel)
    if ch_name in RSVP_CHANNELS and not message.author.bot:
        await _handle_rsvp_post(message)

    await bot.process_commands(message)  # これを忘れるとコマンドが動かない

    if message.guild is None:
        await _handle_dm_message(message)


async def _resolve_reacting_member(
    guild: discord.Guild,
    payload: discord.RawReactionActionEvent,
) -> discord.Member | None:
    """
    リアクションを押したメンバーを解決する。

    入力
    ----
    guild : discord.Guild
        リアクションが発生したギルド
    payload : discord.RawReactionActionEvent
        リアクション追加イベントのペイロード

    出力
    ----
    discord.Member | None
        解決できたメンバー。Bot 自身のリアクション、
        またはメンバーが見つからない場合は None。
    """
    member: discord.Member | None = payload.member
    if member is None:  # キャッシュに居ない場合は取得を試みる
        member = guild.get_member(payload.user_id)
        if member is None:
            try:
                member = await guild.fetch_member(payload.user_id)
            except discord.NotFound:
                return None

    if member.bot:
        return None
    return member


def _format_date_jp(msg: discord.Message, *, with_weekday: bool) -> str:
    """
    メッセージから練習日を推定し、日本語の日付文字列を作る。

    入力
    ----
    msg : discord.Message
        練習日を推定する元になる RSVP 投稿
    with_weekday : bool
        True なら "7月31日(木)" 形式、False なら "7月31日" 形式

    出力
    ----
    str
        日付文字列。本文から日付が読み取れなければ投稿日時（JST）を使う。
    """
    dt = _parse_date_from_msg(msg)
    if dt is None:
        dt = msg.created_at.replace(tzinfo=timezone.utc) + timedelta(hours=9)

    if with_weekday:
        weekday_jp = _WEEKDAYS_JP[dt.weekday()]
        return f"{dt.month}月{dt.day}日({weekday_jp})"
    return f"{dt.month}月{dt.day}日"


# ------------------------------------------------------------
# 出欠リアクションの「付けた順」解決ロジック
# ------------------------------------------------------------
def _resolve_stack_to_bases(stack: list[str]) -> list[str]:
    """
    追加順スタックから、書き込むべきステータスの組を返す。
    - 空なら []（未回答）
    - 末尾2つが {遅刻, 早退} ならその2つ
    - それ以外は末尾1つだけ（＝最後に付けたリアクション）
    """
    if not stack:
        return []
    if len(stack) >= 2 and set(stack[-2:]) == {"遅刻", "早退"}:
        return ["遅刻", "早退"]
    return [stack[-1]]


def _fixed_priority_bases(active: set[str]) -> list[str]:
    """
    順序情報が信頼できない場合のフォールバック。
    出席 > 欠席 > 遅刻/早退併記、の固定優先順位で解決する
    （_collect_attendance_from_reactions と同じ考え方）。
    """
    if "出席" in active:
        return ["出席"]
    if "欠席" in active:
        return ["欠席"]
    parts = []
    if "遅刻" in active:
        parts.append("遅刻")
    if "早退" in active:
        parts.append("早退")
    return parts


async def _live_active_statuses_for_member(
    msg: discord.Message, member_id: int
) -> set[str]:
    """メッセージの現在のリアクションから、member_id が付けている出欠ステータス集合を返す"""
    active: set[str] = set()
    for reaction in msg.reactions:
        emoji_name = _norm_emoji(
            getattr(reaction.emoji, "name", str(reaction.emoji))
        )
        status = status_from_emoji(emoji_name)
        if status is None:
            continue
        async for user in reaction.users():
            if user.id == member_id:
                active.add(status)
                break
    return active


async def _reconcile_and_resolve(
    msg: discord.Message, member_id: int
) -> list[str]:
    """
    自前管理の追加順スタックと、Discord 上の実際のリアクション状況を突き合わせる。

    一致していれば（＝Bot が稼働し続けていて順序を正しく追えている）
    追加順ルールで解決する。ズレていれば（再起動でスタックが失われた等）
    固定優先順位にフォールバックし、スタック自体も現状に合わせて修復する。
    """
    key = (msg.id, member_id)
    stack = _REACTION_ORDER.get(key, [])
    live_active = await _live_active_statuses_for_member(msg, member_id)

    if set(stack) == live_active:
        resolved = _resolve_stack_to_bases(stack)
    else:
        resolved = _fixed_priority_bases(live_active)
        if live_active:
            _REACTION_ORDER[key] = [
                s for s in ("出席", "欠席", "遅刻", "早退") if s in live_active
            ]
        else:
            _REACTION_ORDER.pop(key, None)
        await asyncio.to_thread(_save_reaction_order, _REACTION_ORDER)

    return resolved


async def _handle_status_reaction(
    *,
    status: str,
    channel: discord.TextChannel,
    message_id: int,
    member: discord.Member,
    sheet_key: str,
    bridge: SheetAsyncBridge,
) -> None:
    """
    出席／欠席／遅刻／早退のリアクションを処理する。

    入力
    ----
    status : str
        '出席' '欠席' '遅刻' '早退' のいずれか
    channel, message_id : discord.TextChannel, int
        対象の RSVP メッセージを特定する情報
    member : discord.Member
        リアクションを押したメンバー
    sheet_key, bridge : str, SheetAsyncBridge
        書き込み先シートの選択情報

    出力
    ----
    なし。付けた順スタックを更新し、解決したステータスで Sheets を更新する。
    遅刻／早退なら RSVP スレッドに時刻入力ボタンも投稿する。
    """
    msg = await channel.fetch_message(message_id)

    key = (message_id, member.id)
    stack = _REACTION_ORDER.setdefault(key, [])
    if status not in stack:
        stack.append(status)
    await asyncio.to_thread(_save_reaction_order, _REACTION_ORDER)

    resolved = await _reconcile_and_resolve(msg, member.id)
    try:
        await bridge.set_resolved_bases_async(message_id, member.id, resolved)
    except SheetTargetNotFoundError as exc:
        await _notify_sheet_target_missing(
            channel.guild,
            member_display=member.display_name,
            message_id=message_id,
            exc=exc,
        )
        return

    if status not in {"遅刻", "早退"}:
        return

    await _post_time_input_button(
        msg=msg,
        member=member,
        server_message_id=message_id,
        status=status,
        sheet_key=sheet_key,
    )


async def _handle_status_reaction_remove(
    *,
    status: str,
    channel: discord.TextChannel,
    message_id: int,
    member: discord.Member,
    bridge: SheetAsyncBridge,
) -> None:
    """
    出席／欠席／遅刻／早退のリアクションが外された時の処理。

    入力
    ----
    status : str
        外された側のステータス（'出席' '欠席' '遅刻' '早退' のいずれか）
    channel, message_id : discord.TextChannel, int
        対象の RSVP メッセージを特定する情報
    member : discord.Member
        リアクションを外したメンバー
    bridge : SheetAsyncBridge
        書き込み先シート

    出力
    ----
    なし。付けた順スタックから該当ステータスを外し、残ったリアクションから
    再解決したステータスで Sheets を更新する（全部外れていれば未回答＝空欄）。
    """
    msg = await channel.fetch_message(message_id)

    key = (message_id, member.id)
    stack = _REACTION_ORDER.get(key, [])
    if status in stack:
        stack.remove(status)
    if not stack:
        _REACTION_ORDER.pop(key, None)
    await asyncio.to_thread(_save_reaction_order, _REACTION_ORDER)

    resolved = await _reconcile_and_resolve(msg, member.id)
    try:
        await bridge.set_resolved_bases_async(message_id, member.id, resolved)
    except SheetTargetNotFoundError as exc:
        await _notify_sheet_target_missing(
            channel.guild,
            member_display=member.display_name,
            message_id=message_id,
            exc=exc,
        )


async def _handle_checkmark_reaction(
    *,
    channel: discord.TextChannel,
    message_id: int,
    member: discord.Member,
    pushed_emoji: discord.PartialEmoji,
    bridge: SheetAsyncBridge,
) -> None:
    """
    ✅ リアクション（練習日列の手動同期）を処理する。

    入力
    ----
    channel, message_id : discord.TextChannel, int
        対象の RSVP メッセージを特定する情報
    member : discord.Member
        ✅ を押したメンバー（処理後にリアクションを外される）
    pushed_emoji : discord.PartialEmoji
        押された絵文字そのもの（remove_reaction に使う）
    bridge : SheetAsyncBridge
        書き込み先シート

    出力
    ----
    なし。列を確保し、現在のリアクション集計で一括更新したうえで
    ✅ を消す。
    """
    msg = await channel.fetch_message(message_id)

    header_str = _get_header_from_msg(msg)
    await bridge.add_event_column_async(
        header_str, message_id, _get_event_date_iso(msg)
    )

    values_by_member = await _collect_attendance_from_reactions(msg)
    await bridge.bulk_update_status_column_async(message_id, values_by_member)

    await msg.remove_reaction(pushed_emoji, member)


async def _send_attendance_charts(
    *,
    guild: discord.Guild,
    channel: discord.TextChannel,
    message_id: int,
    bridge: SheetAsyncBridge,
    send_mode: str,
    member: discord.Member | None = None,
) -> None:
    """
    対象メッセージの出欠表画像を各プログラム分生成して送信する。
    send_mode='channel' なら出力チャンネルへ、'dm' なら member へ DM。
    member はエラー通知先（None ならログ出力のみ）。
    """
    msg = await channel.fetch_message(message_id)
    date_str_jp = _format_date_jp(msg, with_weekday=True)

    out_ch: discord.TextChannel | None = None
    if send_mode == "channel":
        out_ch = discord.utils.get(guild.channels, name=OUTPUT_CHANNEL) or channel

    for prog in PROGRAMS:
        try:
            attendance = await bridge.attendance_dict_async(message_id, prog)
        except GSpreadException as exc:
            if "contains duplicates" in str(exc):
                msg_text = (
                    "❌ 画像を作成できませんでした。\n"
                    "Spread Sheet 1 行目に同じ日付が複数あります。\n"
                    "ヘッダの重複を削除してから再度お試しください。"
                )
            else:
                msg_text = (
                    "❌ 画像を作成中にエラーが発生しました。\n"
                    f"詳細: {exc}"
                )
            if member is not None:
                await member.send(msg_text)
            else:
                print(f"[daily-output] {msg_text}")
            return

        img_path = await asyncio.to_thread(
            _draw_attendance_chart, attendance, date_str_jp, prog
        )
        file = discord.File(img_path)
        if send_mode == "channel":
            await out_ch.send(file=file)
        else:
            await member.send(file=file)


async def _handle_output_reaction(
    *,
    send_mode: str,
    guild: discord.Guild,
    channel: discord.TextChannel,
    message_id: int,
    member: discord.Member,
    bridge: SheetAsyncBridge,
) -> None:
    """
    出力／DM リアクションを処理し、各プログラムの出欠画像を送信する。

    入力
    ----
    send_mode : str
        'channel'（出力チャンネルへ送信）または 'dm'（押した人へ DM）
    guild, channel, message_id : 対象の RSVP メッセージを特定する情報
    member : discord.Member
        リアクションを押したメンバー（DM 送信先、エラー通知先）
    bridge : SheetAsyncBridge
        出欠データの取得元シート

    出力
    ----
    なし。プログラムごとに画像を生成し、出力チャンネルまたは DM へ送る。
    Sheets 側のヘッダ重複などでエラーが起きた場合は member に通知して打ち切る。
    """
    await _send_attendance_charts(
        guild=guild,
        channel=channel,
        message_id=message_id,
        bridge=bridge,
        send_mode=send_mode,
        member=member,
    )


@bot.event
async def on_raw_reaction_add(
    payload: discord.RawReactionActionEvent,
) -> None:
    """
    リアクション追加ハンドラ（orchestrator）。
    RSVP チャンネル内の対象リアクションを種類ごとに判定し、
    _handle_status_reaction / _handle_checkmark_reaction /
    _handle_output_reaction のいずれかへ委譲する。
    """
    guild = bot.get_guild(payload.guild_id)
    if guild is None:  # DM など
        return

    member = await _resolve_reacting_member(guild, payload)
    if member is None:
        return

    channel = guild.get_channel(payload.channel_id)
    if channel is None:
        return
    ch_name = str(channel)
    if ch_name not in RSVP_CHANNELS:
        return

    try:
        msg = await channel.fetch_message(payload.message_id)
    except discord.HTTPException:
        return
    sheet_key = _sheet_key_for_message(msg)
    if sheet_key is None:  # 全奏/分奏いずれでもない投稿へのリアクションは無視
        return
    bridge = _bridge_for_sheet_key(sheet_key)

    emoji_name = _norm_emoji(payload.emoji.name)
    message_id = payload.message_id

    status = status_from_emoji(emoji_name)
    if status:
        await _handle_status_reaction(
            status=status,
            channel=channel,
            message_id=message_id,
            member=member,
            sheet_key=sheet_key,
            bridge=bridge,
        )
        return

    if emoji_name == CHECKMARK_EMOJI:
        await _handle_checkmark_reaction(
            channel=channel,
            message_id=message_id,
            member=member,
            pushed_emoji=payload.emoji,
            bridge=bridge,
        )
        return

    if _norm_emoji(emoji_name) == _norm_emoji(EMOJI_NAME["出力"]):
        send_mode = "channel"
    elif _norm_emoji(emoji_name) == _norm_emoji(EMOJI_NAME["DM"]):
        send_mode = "dm"
    else:
        return

    await _handle_output_reaction(
        send_mode=send_mode,
        guild=guild,
        channel=channel,
        message_id=message_id,
        member=member,
        bridge=bridge,
    )


@bot.event
async def on_raw_reaction_remove(
    payload: discord.RawReactionActionEvent,
) -> None:
    """
    リアクション削除ハンドラ。
    出欠系リアクション（出席／欠席／遅刻／早退）が外された場合のみ処理し、
    _handle_status_reaction_remove へ委譲する。それ以外の絵文字（✅・出力・DM）は
    外す操作に意味を持たないため何もしない。
    """
    guild = bot.get_guild(payload.guild_id)
    if guild is None:  # DM など
        return

    member = await _resolve_reacting_member(guild, payload)
    if member is None:
        return

    channel = guild.get_channel(payload.channel_id)
    if channel is None:
        return
    ch_name = str(channel)
    if ch_name not in RSVP_CHANNELS:
        return

    try:
        msg = await channel.fetch_message(payload.message_id)
    except discord.HTTPException:
        return
    sheet_key = _sheet_key_for_message(msg)
    if sheet_key is None:  # 全奏/分奏いずれでもない投稿へのリアクションは無視
        return
    bridge = _bridge_for_sheet_key(sheet_key)

    emoji_name = _norm_emoji(payload.emoji.name)
    status = status_from_emoji(emoji_name)
    if status is None:
        return

    await _handle_status_reaction_remove(
        status=status,
        channel=channel,
        message_id=payload.message_id,
        member=member,
        bridge=bridge,
    )


# ============================================================
# コマンド
# ============================================================
# -----------------------------------------------------------
# $ append @member プログラム パート 席次
# -----------------------------------------------------------
async def _confirm_overwrite(
    ctx: commands.Context, member: discord.Member, error: CellOccupiedError
) -> bool:
    """
    乗り番の上書き確認ダイアログを出し、ユーザーの反応を待つ。

    入力
    ----
    ctx : commands.Context
        確認メッセージを送るコマンドコンテキスト
    member : discord.Member
        上書き対象のメンバー（メッセージ表示用）
    error : CellOccupiedError
        既存値と新しい値の情報を持つ例外

    出力
    ----
    bool
        ✅ が押されて上書き承認された場合 True。
        タイムアウト／キャンセルの場合は False
        （その場合、案内メッセージの編集まで済ませてある）。
    """
    warn_msg = (
        f"{member.mention} さんは既に "
        f"{error.program} で {error.prev_part}-{error.prev_num} として登録されています。\n"
        f"新しく {error.new_part}-{error.new_num} で上書きしてもよろしいですか？\n"
        f"{CHECKMARK_EMOJI}：上書きする  {CANCEL_EMOJI}：キャンセル（30 秒以内）"
    )
    warn = await ctx.send(warn_msg)
    await warn.add_reaction("✅")
    await warn.add_reaction(CANCEL_EMOJI)

    def check(reaction: discord.Reaction, user: discord.User) -> bool:
        return (
            reaction.message.id == warn.id
            and str(reaction.emoji) in {CHECKMARK_EMOJI, CANCEL_EMOJI}
            and user.id == ctx.author.id
        )

    try:
        reaction, _ = await bot.wait_for("reaction_add", timeout=30.0, check=check)
    except asyncio.TimeoutError:
        await warn.edit(content="タイムアウトしました。上書きは行われませんでした。")
        return False

    if str(reaction.emoji) == CANCEL_EMOJI:
        await warn.edit(content="キャンセルしました。上書きは行われませんでした。")
        return False

    await warn.delete()  # ダイアログを片付ける
    return True


@bot.command(
    name="append",
    help="$ append @member プログラム名 パート 席次",
)
@commands.has_any_role(*OUTPUT_ROLES)
async def append_prefix_cmd(
    ctx: commands.Context,
    member: discord.Member,
    program: str,
    part: str,
    num: int,
) -> None:
    """乗り番（パート・席次）を SpreadSheet に登録する（全奏のみ）"""
    if program not in PROGRAMS:
        await ctx.send(
            f"プログラム名 '{program}' は無効です。\n"
            f"有効値: {', '.join(PROGRAMS)}"
        )
        return

    try:
        await sheet_bridge_ensou.append_member_async(
            program=program,
            part=part,
            num=num,
            display_name=member.display_name,
            member_id=member.id,
            overwrite=False,
        )
    except CellOccupiedError as e:
        approved = await _confirm_overwrite(ctx, member, e)
        if not approved:
            return
        await sheet_bridge_ensou.append_member_async(
            program=program,
            part=part,
            num=num,
            display_name=member.display_name,
            member_id=member.id,
            overwrite=True,
        )

    # ---------- 成功：コマンド発言に ✅ ---------------
    await ctx.message.add_reaction("✅")


# ============================================================
# コマンド: サーバーメンバー → SpreadSheet 同期（全奏・分奏の両方）
# ============================================================
@bot.command(
    name="syncmembers",
    help="$ syncmembers : サーバーメンバーの Discord 表示名 / ID を "
         "SpreadSheet（全奏・分奏の両方）に追加または上書き",
)
@commands.has_any_role(*OUTPUT_ROLES)
async def sync_members_cmd(ctx: commands.Context) -> None:
    """
    ギルドメンバーのうち Sheets 未登録の人だけを全奏シートに追加する。
    分奏シートのメンバー情報は全奏参照（ARRAYFORMULA）なので自動で追従する。
    追加があった場合はパート順の並べ替えと分奏イベント列の追従も行う。
    """
    with_part, no_part = await _sync_members_to_sheet(ctx.guild, gs_manager_ensou)
    if with_part + no_part > 0:
        await _sort_and_realign_sheets()
    await ctx.send(
        f"✅ 同期完了: 追加 {with_part + no_part} 名 "
        f"(パート判定あり {with_part} 名, パート無し {no_part} 名)\n"
        f"※分奏シートは全奏参照のため自動反映"
    )
    await ctx.message.add_reaction("✅")


@bot.command(
    name="sortmembers",
    help="$ sortmembers : シートのメンバー行をパート順に並べ替える",
)
@commands.has_any_role(*OUTPUT_ROLES)
async def sort_members_cmd(ctx: commands.Context) -> None:
    """全奏シートをパート順に並べ替え、分奏のイベント列を追従させる"""
    msg = await ctx.send("🔄 パート順に並べ替え中...")
    n_sorted, n_realigned = await _sort_and_realign_sheets()
    await msg.edit(
        content=(
            f"✅ 並べ替え完了（全奏 {n_sorted} 行 / "
            f"分奏イベント列の追従 {n_realigned} 行）"
        )
    )
    await ctx.message.add_reaction("✅")


@bot.command(
    name="sync",
    help="$ sync : 最新のRSVP回答状況をスプレッドシートに強制同期",
)
@commands.has_any_role(*OUTPUT_ROLES)
async def sync_rsvp_cmd(ctx: commands.Context) -> None:
    """現在のDiscord RSVP Channelの最新回答に合わせてシートを更新する"""
    msg = await ctx.send("🔄 最新の回答状況を同期中...")
    
    count = await _sync_latest_rsvp_in_guild(ctx.guild)
    
    await msg.edit(content=f"✅ 同期完了: {count} 件のRSVPチャンネルを更新しました。")
    await ctx.message.add_reaction("✅")


@bot.command(name="remind")
@commands.has_any_role(*OUTPUT_ROLES)
async def cmd_remind(ctx: commands.Context, days_before: int | None = None) -> None:
    """手動でリマインドを実行する。$remind または $remind 3"""
    days = [days_before] if days_before is not None else None
    await ctx.send("⏳ リマインドを実行します…")
    await _run_reminder_job(days)
    await ctx.send("✅ リマインド処理が完了しました。")


@bot.command(name="outputtoday")
@commands.has_any_role(*OUTPUT_ROLES)
async def cmd_outputtoday(ctx: commands.Context) -> None:
    """手動で当日出欠表出力を実行する。"""
    await ctx.send("⏳ 当日の出欠表を出力します…")
    await _run_daily_output_job()
    await ctx.send("✅ 出力処理が完了しました。")

# ============================================================
# その他ヘルパ
# ============================================================
def _draw_attendance_chart(
    attendance: dict,
    date_str_jp: str,
    program_name: str,
) -> Path:
    """同期関数：draw.py を呼んで png を作る"""
    drawer = PlayerBoxDrawer(layout)

    for (part, num, name), status in attendance.items():
        if not part or (part, num) not in layout.seats:
            continue

        # -------- ステータス解析 --------
        has_late = "遅刻" in status
        has_leave = "早退" in status

        if has_late and has_leave:
            fill_l, font_l = LEGEND_COLOR["遅刻"]
            fill_r, font_r = LEGEND_COLOR["早退"]
            drawer.draw_playerbox_split(
                part, num, name,
                fill_left=fill_l,
                fill_right=fill_r,
                font_color=BLACK,
            )
        else:
            base = (
                "遅刻" if has_late else
                "早退" if has_leave else
                _base_status(status)   # '出席' '欠席' '未回答'
            )
            fill, font = LEGEND_COLOR.get(base, LEGEND_COLOR["未回答"])
            drawer.draw_playerbox(part, num, name, fill, font)

    drawer.draw_program(date_str_jp, program_name)

    out_dir = Path("generated")
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"attendance_{program_name}_{date_str_jp}.png"
    drawer.save(out_path)
    return out_path


async def _safe_fetch_member(
    guild: discord.Guild,
    user_id: int,
) -> discord.Member | None:
    """キャッシュに無ければ fetch。見つからなければ None"""
    member = guild.get_member(user_id)
    if member:
        return member
    try:
        return await guild.fetch_member(user_id)
    except discord.NotFound:
        return None


async def _collect_attendance_from_reactions(
    msg: discord.Message,
) -> dict[int, str]:
    """
    メッセージに付いた出欠リアクションを全走査し、
    {member_id: status_raw} を構築して返す。
    仕様:
      - 出席があれば『出席』で確定（欠席/遅刻/早退は無視）
      - 出席が無く欠席があれば『欠席』
      - 上記が無ければ『遅刻』『早退』を同時併記可（時刻なし）
      - 何も無ければ辞書に含めない（= 空セルのまま）
    """
    guild = msg.guild
    if guild is None:
        return {}

    reacted: dict[int, set[str]] = {}

    for reaction in msg.reactions:
        emoji_name = _norm_emoji(
            getattr(reaction.emoji, "name", str(reaction.emoji))
        )
        status = status_from_emoji(emoji_name)
        if status is None:
            continue

        async for user in reaction.users():
            if user.bot:
                continue
            if user.id not in reacted:
                reacted[user.id] = set()
            reacted[user.id].add(status)

    result: dict[int, str] = {}
    for mid, statuses in reacted.items():
        if "出席" in statuses:
            result[mid] = "出席"
            continue
        if "欠席" in statuses:
            result[mid] = "欠席"
            continue
        parts: list[str] = []
        if "遅刻" in statuses:
            parts.append("遅刻")
        if "早退" in statuses:
            parts.append("早退")
        if parts:
            result[mid] = " ".join(parts)

    return result

# ------------------------------------------------------------
# 日付文字列抽出（メッセージ本文 1 行目から yyyy-mm-dd / yyyy/mm/dd を検索）
# ------------------------------------------------------------

# 1) yyyy-mm-dd / yyyy/mm/dd
# 2) mm-dd     / mm/dd
# 3) mm月dd日
# 4) mm月DD日 (未定)
_DATE_RE_FULL = re.compile(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})")
_DATE_RE_MD = re.compile(r"(\d{1,2})[/-](\d{1,2})")
_DATE_RE_JP = re.compile(r"(\d{1,2})月(\d{1,2})日")
_DATE_RE_JP_UNDECIDED = re.compile(r"(\d{1,2})月(?:DD|dd)日")

def _find_date_line(content: str) -> str:
    """
    本文から日付パターンを含む最初の行を返す（見つからなければ空文字）。

    ザムスターク管弦楽団のカレンダー投稿は 1 行目が区切り線（-----）で
    2 行目に『8/1 (土) 全奏 18:00-22:00 @会場』のように日付が来るため、
    先頭行固定ではなく行を走査して日付行を探す。
    """
    for line in content.splitlines():
        line = line.strip()
        for pat in (_DATE_RE_FULL, _DATE_RE_JP_UNDECIDED, _DATE_RE_MD, _DATE_RE_JP):
            if pat.search(line):
                return line
    return ""


def _parse_date_from_msg(msg: discord.Message) -> datetime | None:
    """
    メッセージから練習日の datetime オブジェクトを推定して返す。
    日付は本文中の最初の日付行（_find_date_line）から読み取る。
    年は「メッセージの投稿日時」を基準にする。
    原則として「投稿日よりも過去の練習日はあり得ない」という前提で、
    同年の日付が投稿日より過去になる場合は、翌年と判定する。
    """
    text_first = _find_date_line(msg.content or "")
    
    # 投稿日時（JST）
    posted_at = msg.created_at.astimezone(JST)
    posted_date = posted_at.date()

    # --- パターンA: yyyy-mm-dd (明示) ---
    m_full = _DATE_RE_FULL.search(text_first)
    if m_full:
        y, m, d = map(int, m_full.groups())
        return datetime(y, m, d, tzinfo=JST)

    # --- パターンB: 10月DD日 (未確定) ---
    m_und = _DATE_RE_JP_UNDECIDED.search(text_first)
    if m_und:
        month = int(m_und.group(1))
        year = posted_at.year
        # 「投稿された月」よりも「指定月」が過去なら、翌年の話をしているとみなす
        if month < posted_at.month:
             year += 1
        # 日付比較用に仮で1日を入れて返す
        return datetime(year, month, 1, tzinfo=JST)

    # --- パターンC: mm-dd / mm月dd日 (年補完) ---
    for pat in (_DATE_RE_MD, _DATE_RE_JP):
        m = pat.search(text_first)
        if m:
            month, day = map(int, m.groups())
            
            try:
                # まず「投稿年」で日付を作ってみる
                candidate = datetime(posted_at.year, month, day, tzinfo=JST)
            except ValueError:
                continue # うるう年やありえない日付のガード

            # 作成した日付が「投稿日」より前なら、翌年の日付とする
            if candidate.date() < posted_date:
                try:
                    candidate = candidate.replace(year=candidate.year + 1)
                except ValueError:
                    continue 

            return candidate

    return None


def _get_event_date_iso(msg: discord.Message) -> str | None:
    """
    練習日が確定していれば 'YYYY/MM/DD' 形式で返す（未定パターンは None）。

    add_event_column に渡すと、ヘッダが年情報込みの日付値として
    書き込まれ（表示は 'M月D日(曜)'）、時系列順の位置に列が挿入される。
    """
    if _is_undecided(msg):
        return None

    dt = _parse_date_from_msg(msg)
    if dt is None:
        # 日付解析不能：投稿日時を練習日とみなす（従来のフォールバックと同じ）
        jst = msg.created_at.astimezone(JST)
        return f"{jst.year}/{jst.month:02d}/{jst.day:02d}"
    return f"{dt.year}/{dt.month:02d}/{dt.day:02d}"


def _get_header_from_msg(msg: discord.Message) -> str:
    """
    SpreadSheetのヘッダ用文字列を生成する（例: '7月4日(土)'）。

    ヘッダは人間向けの表示専用（列の同一性は 2 行目のメッセージ ID で
    管理される）ため、年は表示しない。
    ※ 日付が確定している場合は _get_event_date_iso が優先され、
      この文字列は「未定」パターンのときだけ実際にヘッダへ書かれる。
    """
    dt = _parse_date_from_msg(msg)

    # 日付解析不能だった場合: 投稿日時をそのまま使う
    if dt is None:
        jst = msg.created_at.astimezone(JST)
        weekday_jp = _WEEKDAYS_JP[jst.weekday()]
        return f"{jst.month}月{jst.day}日({weekday_jp})"

    # 未定パターンかどうかの判定（ヘッダ文字列生成のため）
    if _is_undecided(msg):
         return f"{dt.month}月DD日(未定)"

    # 確定日付
    weekday_jp = _WEEKDAYS_JP[dt.weekday()]
    return f"{dt.month}月{dt.day}日({weekday_jp})"


# ============================================================
# Bot 起動
# ============================================================
bot.run(BOT_TOKEN)