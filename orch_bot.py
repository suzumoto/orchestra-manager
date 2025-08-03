# orch_bot.py
from __future__ import annotations

import os
import asyncio
import re
import json
from pathlib import Path
from datetime import timezone, timedelta, datetime
import discord
from discord.ext import commands
from gspread.exceptions import GSpreadException

from draw import PlayerBoxDrawer, BLACK
from seat_layout_slides import SeatLayoutSlides
from sheet import (
    GoogleSheetsManager,
    SheetAsyncBridge,
    CellOccupiedError,
    _base_status,
    _default_headers,
)

# ------------------------------------------------------------
# 設定読み込み
# ------------------------------------------------------------
import configparser

config = configparser.ConfigParser()
config.read("settings.ini")

BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("環境変数 DISCORD_BOT_TOKEN が未設定です。")

COMMAND_CHANNELS = {v for v in config["COMMAND_CHANNEL"].values()}
RSVP_CHANNELS = {v for v in config["RSVP_CHANNEL"].values()}
OUTPUT_CHANNEL = config["OUTPUT_CHANNEL"]["output_channel"]

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

# ---------------- Sheets / Slides ---------------------------
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
if not SPREADSHEET_ID:
    raise RuntimeError("環境変数 SPREADSHEET_ID が未設定です。")

gs_manager = GoogleSheetsManager(
    spreadsheet_id=SPREADSHEET_ID,
    worksheet_name=config["SPREADSHEET"]["worksheet_name"],
    credential_json=config["SPREADSHEET"].get("credential_json",
                                              "credentials.json"),
    programs=PROGRAMS,
)
sheet_bridge = SheetAsyncBridge(gs_manager)

PRESENTATION_ID = os.getenv("SLIDES_PRESENTATION_ID")
if not PRESENTATION_ID:
    raise RuntimeError("環境変数 SLIDES_PRESENTATION_ID が未設定です。")

layout = SeatLayoutSlides(
    presentation_id=PRESENTATION_ID,
    credential_json=config["SLIDES"].get("credential_json",
                                         "credentials.json"),
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


def status_from_emoji(emoji_name: str) -> str | None:
    for status, ename in EMOJI_NAME.items():
        if emoji_name == ename and status in {"出席", "欠席", "遅刻", "早退"}:
            return status
    return None


# ------------------------------------------------------------
# yyyy-mm-dd → yyyy年mm月dd日(曜)
# ------------------------------------------------------------
def _format_japanese_date(date_iso: str) -> str:
    """'YYYY-mm-dd' → 'YYYY年MM月DD日(曜)' に変換"""
    dt = datetime.strptime(date_iso, "%Y-%m-%d")
    weekday_jp = _WEEKDAYS_JP[dt.weekday()]  # 0=Mon
    return f"{dt.year}年{dt.month:02d}月{dt.day:02d}日({weekday_jp})"


# ------------------------------------------------------------
# 年無し JP フォーマッタ
# ------------------------------------------------------------
def _format_japanese_date_short(date_iso: str) -> str:
    """'YYYY-mm-dd' → 'MM月DD日(曜)' （年を含めない）"""
    dt = datetime.strptime(date_iso, "%Y-%m-%d")
    weekday_jp = _WEEKDAYS_JP[dt.weekday()]
    return f"{dt.month:02d}月{dt.day:02d}日({weekday_jp})"


# ------------------------------------------------------------
# ユーティリティ群
# ------------------------------------------------------------
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")  # HH:MM 24h


def _norm_emoji(txt: str) -> str:
    """絵文字文字列から VS-16 (U+FE0F) を除去して正規化"""
    return txt.replace("\uFE0F", "")


def _valid_time(text: str) -> bool:
    return bool(_TIME_RE.match(text.strip()))


# ------------------------------------------------------------
# Part 名の正規化ユーティリティ
# ------------------------------------------------------------
def _canon_part(part: str | None) -> str | None:
    """先頭大文字・残り小文字へ統一。None はそのまま返す。"""
    if part is None:
        return None
    return part[:1].upper() + part[1:].lower()


async def _send_time_prompt(
    *,
    member: discord.Member,
    server_message_id: int,
    date_str_jp: str,
    status: str,
) -> None:
    dm = await member.create_dm()
    verb = "到着" if status == "遅刻" else "退出"
    prompt = (
        f"{date_str_jp}の練習に{status}の予定ですね。"
        f"{verb}予定時刻を HH:MM 形式（例 19:30）で"
        "本メッセージへの返信で教えてください。"
    )
    sent = await dm.send(prompt)

    # --- コンテキスト記録 & 保存 ----------------------
    _PROMPT_CONTEXT[sent.id] = (server_message_id, status)
    await asyncio.to_thread(_save_prompt_context, _PROMPT_CONTEXT)


# ------------------------------------------------------------
# 遅刻／早退プロンプトの永続化
# ------------------------------------------------------------
_PROMPT_CONTEXT_FILE = Path("prompt_context.json")


def _load_prompt_context() -> dict[int, tuple[int, str]]:
    """JSON から復元。存在しなければ空 dict"""
    try:
        data = json.loads(_PROMPT_CONTEXT_FILE.read_text(encoding="utf-8"))
        # key は str で保存しているので int に戻す
        return {int(k): tuple(v) for k, v in data.items()}
    except FileNotFoundError:
        return {}
    except Exception:
        # 破損していた場合は空で開始
        return {}


def _save_prompt_context(ctx: dict[int, tuple[int, str]]) -> None:
    """dict を JSON へ保存"""
    tmp = {str(k): list(v) for k, v in ctx.items()}
    _PROMPT_CONTEXT_FILE.write_text(
        json.dumps(tmp, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ============================================================
# ロール名からパート推定
# ============================================================
def _detect_part_from_roles(member: discord.Member) -> str | None:
    """Discord Member のロールからパート名を推定。該当無しなら None"""
    for pattern, part in _ROLE_PATTERNS:
        if any(pattern.match(role.name) for role in member.roles):
            return _canon_part(part)          # ← ここで正規化
    return None


# ============================================================
# 同期用：行 Upsert ユーティリティ（同期関数）
# ============================================================
def _insert_member_row_if_absent(
    gs: GoogleSheetsManager,
    member: discord.Member,
    part: str | None,
) -> bool:
    """
    Discord ID が未登録なら新規行を追加して True を返す。
    既に存在していれば何もせず False。
    """
    if member.id in gs._member_to_row:
        return False

    part = _canon_part(part)

    heads = _default_headers(gs.programs)
    new_row = [""] * len(heads)
    new_row[heads.index("discord表示名")] = member.display_name
    new_row[heads.index("氏名")] = ""
    new_row[heads.index("Discord ID")] = str(member.id)
    if part:
        for prog in gs.programs:
            new_row[heads.index(f"{prog}_パート")] = part
    gs.append_rows_bulk([new_row])
    gs._build_index()
    return True


# ============================================================
# ギルドメンバー → SpreadSheet 同期
# ============================================================
async def _sync_members_to_sheet(guild: discord.Guild) -> tuple[int, int]:
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

    gs = sheet_bridge.gs               # short-hand
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

    for m in guild.members:
        if m.bot or m.id in gs._member_to_row:
            continue
        part_norm = _detect_part_from_roles(m)
        rows_to_append.append(_build_row(m, part_norm))
        if part_norm:
            added_with_part += 1
        else:
            added_no_part += 1

    # ---- gspread  I/O はスレッドへ ------------------------
    await asyncio.to_thread(gs.append_rows_bulk, rows_to_append)
    return added_with_part, added_no_part

# prompt_msg.id → (server_message_id, status) を保持
_PROMPT_CONTEXT: dict[int, tuple[int, str]] = _load_prompt_context()


# ============================================================
# Bot 起動時に自動同期
# ============================================================
_SYNC_DONE_ON_STARTUP = False


# ============================================================
# イベントハンドラ
# ============================================================
@bot.event
async def on_ready() -> None:  # type: ignore[override]
    global _SYNC_DONE_ON_STARTUP

    print(f"Logged in as {bot.user} (id={bot.user.id})")

    if not _SYNC_DONE_ON_STARTUP:
        for g in bot.guilds:
            asyncio.create_task(_sync_members_to_sheet(g))
        _SYNC_DONE_ON_STARTUP = True


@bot.event
async def on_message(message: discord.Message) -> None:
    # RSVP チャンネルで投稿があったら自動でリアクションを付与
    if str(message.channel) in RSVP_CHANNELS and not message.author.bot:
        for key in ("出席", "欠席", "遅刻", "早退"):
            emoji = discord.utils.get(message.guild.emojis,
                                      name=EMOJI_NAME[key])
            if emoji:
                await message.add_reaction(emoji)

        # まだ Sheets にメッセージ ID 未登録なら列を追加して登録
        # 変更: ヘッダを 'yyyy年mm月dd日(曜)' 形式で登録
        date_iso = _extract_date_string(message)
        header_str = _format_japanese_date(date_iso)
        await sheet_bridge.add_event_column_async(header_str, message.id)

    await bot.process_commands(message)  # これを忘れるとコマンドが動かない

    # ===== DM 返信での遅刻／早退時刻登録 ====================
    if message.guild is None and message.reference:
        ref_id = message.reference.message_id
        ctx = _PROMPT_CONTEXT.get(ref_id)
        if ctx:
            server_msg_id, status = ctx
            time_str = message.content.strip()
            if not _valid_time(time_str):
                await message.channel.send(
                    "❌ 形式が正しくありませんでした。"
                    "HH:MM（24 時間制）で入力し直してください。"
                )
            else:
                if status == "遅刻":
                    cell_value = f"遅刻({time_str}～)"
                else:  # 早退
                    cell_value = f"早退(～{time_str})"

                await sheet_bridge.update_status_async(
                    server_msg_id,
                    message.author.id,
                    cell_value,
                )
                await message.add_reaction("✅")


@bot.event
async def on_raw_reaction_add(
    payload: discord.RawReactionActionEvent,
) -> None:
    """リアクション追加ハンドラ"""

    # -------------------------------------------------
    # Guild / Member 取得
    # -------------------------------------------------
    guild = bot.get_guild(payload.guild_id)
    if guild is None:  # DM など
        return

    member: discord.Member | None = payload.member
    if member is None:  # キャッシュに居ない場合は取得を試みる
        member = guild.get_member(payload.user_id)
        if member is None:
            try:
                member = await guild.fetch_member(payload.user_id)
            except discord.NotFound:
                return

    # Bot リアクションは無視
    if member.bot:
        return

    # -------------------------------------------------
    # 以降、payload.member ではなく member を使用
    # -------------------------------------------------
    channel = guild.get_channel(payload.channel_id)
    if channel is None or str(channel) not in RSVP_CHANNELS:
        return

    raw_name = payload.emoji.name
    emoji_name = _norm_emoji(raw_name)
    message_id = payload.message_id

    # 出席系ステータス更新 ------------------------------------
    status = status_from_emoji(emoji_name)
    if status:
        await sheet_bridge.update_status_async(
            message_id, member.id, status
        )

        # ---- 遅刻／早退 → DM で時刻を問い合わせ ----
        if status in {"遅刻", "早退"}:
            date_iso = _extract_date_string(
                await channel.fetch_message(message_id)
            )
            date_str_jp = _format_japanese_date_short(date_iso)
            await _send_time_prompt(
                member=member,
                server_message_id=message_id,
                date_str_jp=date_str_jp,
                status=status,
            )
        return

    # ✅: 練習日列の手動追加 -----------------------------------
    if emoji_name == CHECKMARK_EMOJI:
        msg = await channel.fetch_message(message_id)
        date_iso = _extract_date_string(msg)
        header_str = _format_japanese_date(date_iso)
        await sheet_bridge.add_event_column_async(header_str, message_id)
        await msg.remove_reaction(payload.emoji, member)
        return

    # 出力系 ---------------------------------------------------
    if _norm_emoji(emoji_name) == _norm_emoji(EMOJI_NAME["出力"]):
        send_mode = "channel"
    elif _norm_emoji(emoji_name) == _norm_emoji(EMOJI_NAME["DM"]):
        send_mode = "dm"
    else:
        return

    msg = await channel.fetch_message(message_id)
    date_iso = _extract_date_string(msg)
    date_str_jp = _format_japanese_date_short(date_iso)

    out_ch: discord.TextChannel | None = None
    if send_mode == "channel":
        out_ch = discord.utils.get(guild.channels,
                                   name=OUTPUT_CHANNEL) or channel

    for prog in PROGRAMS:
        try:
            attendance = await sheet_bridge.attendance_dict_async(
                message_id, prog
            )
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
            await member.send(msg_text)
            return
        else:
            img_path = await asyncio.to_thread(
                _draw_attendance_chart,
                attendance,
                date_str_jp,
                prog,
            )
            file = discord.File(img_path)
            if send_mode == "channel":
                await out_ch.send(file=file)
            else:
                await member.send(file=file)


# ============================================================
# コマンド
# ============================================================
# -----------------------------------------------------------
# $ append @member プログラム パート 席次
# -----------------------------------------------------------
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
    """乗り番（パート・席次）を SpreadSheet に登録する"""
    # ---------- 入力チェック ----------
    if program not in PROGRAMS:
        await ctx.send(
            f"プログラム名 '{program}' は無効です。\n"
            f"有効値: {', '.join(PROGRAMS)}"
        )
        return

    try:
        await sheet_bridge.append_member_async(
            program=program,
            part=part,
            num=num,
            display_name=member.display_name,
            member_id=member.id,
            overwrite=False,
        )
    except CellOccupiedError as e:
        # --- 既に登録済み：上書き確認 ---
        warn_msg = (
            f"{member.mention} さんは既に "
            f"{e.program} で {e.prev_part}-{e.prev_num} として登録されています。\n"
            f"新しく {e.new_part}-{e.new_num} で上書きしてもよろしいですか？\n"
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
            reaction, _ = await bot.wait_for(
                "reaction_add", timeout=30.0, check=check
            )
        except asyncio.TimeoutError:
            await warn.edit(content="タイムアウトしました。上書きは行われませんでした。")
            return

        # -------- ユーザー反応を判定 ----------
        if str(reaction.emoji) == CANCEL_EMOJI:
            await warn.edit(content="キャンセルしました。上書きは行われませんでした。")
            return

        # -------- 上書き実行（✅ が押された場合） ----------
        await sheet_bridge.append_member_async(
            program=program,
            part=part,
            num=num,
            display_name=member.display_name,
            member_id=member.id,
            overwrite=True,
        )
        await warn.delete()  # ダイアログを片付ける

    # ---------- 成功：コマンド発言に ✅ ---------------
    await ctx.message.add_reaction("✅")


# ============================================================
# コマンド: サーバーメンバー → SpreadSheet 同期
# ============================================================
@bot.command(
    name="syncmembers",
    help="$ syncmembers : サーバーメンバーの Discord 表示名 / ID を "
         "SpreadSheet に追加または上書き",
)
@commands.has_any_role(*OUTPUT_ROLES)
async def sync_members_cmd(ctx: commands.Context) -> None:
    with_part, no_part = await _sync_members_to_sheet(ctx.guild)
    await ctx.send(
        f"✅ 同期完了: 追加 {with_part + no_part} 名 "
        f"パート判定あり {with_part} 名, "
        f"パート無し {no_part} 名"
    )
    await ctx.message.add_reaction("✅")


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


# ------------------------------------------------------------
# 日付文字列抽出（メッセージ本文 1 行目から yyyy-mm-dd / yyyy/mm/dd を検索）
# ------------------------------------------------------------

# 1) yyyy-mm-dd / yyyy/mm/dd
# 2) mm-dd     / mm/dd
# 3) mm月dd日
_DATE_RE_FULL = re.compile(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})")
_DATE_RE_MD = re.compile(r"(\d{1,2})[/-](\d{1,2})")
_DATE_RE_JP = re.compile(r"(\d{1,2})月(\d{1,2})日")


def _extract_date_string(msg: discord.Message) -> str:
    """メッセージ本文から日付を探す。見つからなければ投稿日時を返す。"""
    text_first = msg.content.splitlines()[0]

    # --- yyyy-mm-dd 明示パターン ------------------------
    m_full = _DATE_RE_FULL.search(text_first)
    if m_full:
        y, m, d = map(int, m_full.groups())
        return f"{y:04d}-{m:02d}-{d:02d}"

    # --- 年無し mm-dd / mm/dd --------------------------
    for pat in (_DATE_RE_MD, _DATE_RE_JP):
        m = pat.search(text_first)
        if m:
            month, day = map(int, m.groups())
            return _nearest_future_date(month, day)

    # --- 該当無し：投稿日時（JST） -----------------------
    jst = msg.created_at.replace(tzinfo=timezone.utc) + timedelta(hours=9)
    return jst.strftime("%Y-%m-%d")


def _nearest_future_date(month: int, day: int) -> str:
    today = datetime.now(tz=timezone(timedelta(hours=9)))  # JST 現在
    year = today.year
    try:
        candidate = datetime(year, month, day, tzinfo=today.tzinfo)
    except ValueError:
        # 無効日付はそのまま raise させる
        raise

    if candidate >= today:
        return candidate.strftime("%Y-%m-%d")
    # 今年は過ぎている → 翌年
    candidate = datetime(year + 1, month, day, tzinfo=today.tzinfo)
    return candidate.strftime("%Y-%m-%d")


# ============================================================
# Bot 起動
# ============================================================
bot.run(BOT_TOKEN)
