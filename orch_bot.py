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
from gspread.exceptions import APIError

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

# ---------------- Sheets / Slides ---------------------------
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
if not SPREADSHEET_ID:
    raise RuntimeError("環境変数 SPREADSHEET_ID が未設定です。")

# シート名の設定（デフォルト: 全奏 / 分奏）
ENSOU_SHEET_NAME = config["SPREADSHEET"].get(
    "ensou_worksheet_name",
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


def _sheet_key_from_channel_name(ch_name: str) -> str | None:
    if ch_name in RSVP_CHANNELS_ENSOU:
        return SHEET_KEY_ENSOU
    if ch_name in RSVP_CHANNELS_BUNSOU:
        return SHEET_KEY_BUNSOU
    return None


def _bridge_for_sheet_key(sheet_key: str) -> SheetAsyncBridge:
    if sheet_key == SHEET_KEY_ENSOU:
        return sheet_bridge_ensou
    if sheet_key == SHEET_KEY_BUNSOU:
        return sheet_bridge_bunsou
    raise ValueError(f"unknown sheet_key: {sheet_key}")


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


async def _send_time_prompt(
    *,
    member: discord.Member,
    server_message_id: int,
    date_str_jp: str,
    status: str,
    sheet_key: str,
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
    _PROMPT_CONTEXT[sent.id] = (server_message_id, status, sheet_key)
    await asyncio.to_thread(_save_prompt_context, _PROMPT_CONTEXT)


# ------------------------------------------------------------
# 遅刻／早退プロンプトの永続化
# ------------------------------------------------------------
_PROMPT_CONTEXT_FILE = Path("prompt_context.json")


def _load_prompt_context() -> dict[int, tuple[int, str, str]]:
    """JSON から復元。存在しなければ空 dict"""
    try:
        data = json.loads(_PROMPT_CONTEXT_FILE.read_text(encoding="utf-8"))
        ctx: dict[int, tuple[int, str, str]] = {}
        for k, v in data.items():
            if isinstance(v, list | tuple):
                if len(v) == 3:
                    server_id, status, sheet_key = v
                elif len(v) == 2:
                    # 後方互換（旧フォーマットは ensou 扱い）
                    server_id, status = v
                    sheet_key = SHEET_KEY_ENSOU
                else:
                    continue
                ctx[int(k)] = (int(server_id), str(status), str(sheet_key))
        return ctx
    except FileNotFoundError:
        return {}
    except Exception:
        # 破損していた場合は空で開始
        return {}


def _save_prompt_context(ctx: dict[int, tuple[int, str, str]]) -> None:
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
            return _canon_part(part)
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
# ギルドメンバー → SpreadSheet 同期（全奏のみ）
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

    for m in guild.members:
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


# prompt_msg.id → (server_message_id, status, sheet_key) を保持
_PROMPT_CONTEXT: dict[int, tuple[int, str, str]] = _load_prompt_context()


# ============================================================
# Bot 起動時に自動同期
# ============================================================
_SYNC_DONE_ON_STARTUP = False

# ============================================================
# [追加] 同期用: 最新RSVPメッセージの状態をシートへ強制同期
# ============================================================
async def _sync_latest_rsvp_in_guild(guild: discord.Guild) -> int:
    """
    RSVPチャンネルの履歴(最新20件)から、1行目に日付がある投稿を同期する。
    API制限回避のため、処理ごとに待機時間を設ける。
    """
    synced_count = 0
    
    # [追加] 現在日付(JST)を基準に過去ログ走査を止めるため
    JST = timezone(timedelta(hours=9))
    today = datetime.now(JST).date()

    for ch_name in RSVP_CHANNELS:
        channel = discord.utils.get(guild.text_channels, name=ch_name)
        if not channel:
            continue

        sheet_key = _sheet_key_from_channel_name(ch_name)
        if not sheet_key:
            continue
        
        bridge = _bridge_for_sheet_key(sheet_key)
        print(f"--- Scanning channel: {ch_name} ---")

        # 最新20件を走査
        async for msg in channel.history(limit=20):
            # Bot自身の投稿や、システムメッセージは無視
            if msg.author.bot:
                continue

            # リアクションが付いていない投稿は無視（ただの連絡事項とみなす）
            if len(msg.reactions) == 0:
                continue

            # ヘッダ候補と日付オブジェクトを取得
            dt = _parse_date_from_msg(msg)
            
            # 過去の練習日だと判明したら、それより古いログも見なくていいので打ち切る
            if dt and dt.date() < today:
                print(f"    [SKIP] Found past event {dt.date()} (msg {msg.id}). Stopping history scan.")
                break

            # ヘッダ文字列を生成 (dtがNoneなら投稿日から生成などよしなにやる)
            header_str = _get_header_from_msg(msg)
            
            print(f"    Syncing msg {msg.id} -> Header: {header_str}")

            try:
                # 1. 列確保
                await bridge.add_event_column_async(header_str, msg.id)

                # 2. 集計
                values_by_member = await _collect_attendance_from_reactions(msg)

                # 3. 反映
                await bridge.bulk_update_status_column_async(
                    msg.id,
                    values_by_member,
                )
                synced_count += 1
                
                # [修正] API制限(429 Quota exceeded)回避のため、1件処理するごとに5秒待機
                print("        ...Waiting 5s for API limits...")
                await asyncio.sleep(5.0)

            except APIError as e:
                # 万が一制限に達してもBotごと落ちないようにキャッチしてスキップ/待機
                print(f"    ⚠️ API Error on msg {msg.id}: {e}")
                print("    Waiting 30s before retrying next message...")
                await asyncio.sleep(30.0)
                continue
            
    return synced_count

# ============================================================
# イベントハンドラ
# ============================================================
@bot.event
async def on_ready() -> None:  # type: ignore[override]
    global _SYNC_DONE_ON_STARTUP

    print(f"Logged in as {bot.user} (id={bot.user.id})")

    if not _SYNC_DONE_ON_STARTUP:
        for g in bot.guilds:
            # 1. メンバー情報の同期（全奏シートのみを正とする運用のようなのでこのまま）
            print(f"Syncing members for guild: {g.name}...")
            asyncio.create_task(_sync_members_to_sheet(g, gs_manager_ensou))
            
            # 2. [復元] 最新RSVP(回答)状況の同期
            print(f"Syncing latest RSVP for guild: {g.name}...")
            # ここは非同期タスクとして投げっぱなしにするか、awaitするかは運用次第ですが
            # on_readyがブロックされるのを防ぐなら create_task が安全です
            asyncio.create_task(_sync_latest_rsvp_in_guild(g))

        _SYNC_DONE_ON_STARTUP = True
        print("Startup sync initiated.")


@bot.event
async def on_message(message: discord.Message) -> None:
    # RSVP チャンネルで投稿があったら自動でリアクションを付与
    ch_name = str(message.channel)
    if ch_name in RSVP_CHANNELS and not message.author.bot:
        for key in ("出席", "欠席", "遅刻", "早退"):
            emoji = discord.utils.get(message.guild.emojis, name=EMOJI_NAME[key])
            if emoji:
                await message.add_reaction(emoji)

        # まだ Sheets にメッセージ ID 未登録なら列を追加して登録
        # [変更] 新しいヘッダ生成ロジックを使用
        header_str = _get_header_from_msg(message)
        sheet_key = _sheet_key_from_channel_name(ch_name)
        if sheet_key:
            bridge = _bridge_for_sheet_key(sheet_key)
            await bridge.add_event_column_async(header_str, message.id)

    await bot.process_commands(message)  # これを忘れるとコマンドが動かない

    # ===== DM 返信での遅刻／早退時刻登録＋非返信DMの案内 ====================
    if message.guild is None:
        # Bot が送った DM メッセージには反応しない（無限ループ防止）
        if message.author.bot:
            return

        if message.reference:
            # 返信として届いた DM
            ref_id = message.reference.message_id
            ctx = _PROMPT_CONTEXT.get(ref_id)
            if ctx:
                server_msg_id, status, sheet_key = ctx
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

                    bridge = _bridge_for_sheet_key(sheet_key)
                    await bridge.update_status_async(
                        server_msg_id,
                        message.author.id,
                        cell_value,
                    )
                    await message.add_reaction("✅")
            else:
                # 返信ではあるが、当Botの確認メッセージへの返信ではない
                await message.channel.send(
                    "この返信は当Botの確認メッセージに紐付いていないため処理できませんでした。\n"
                    "遅刻/早退の時刻を登録するには、当Botが送信した『確認メッセージ』に対して返信してください。\n"
                    "スマホではメッセージを長押しして「返信」を選択してください。"
                )
        else:
            # 返信でない DM はガイダンスを返す
            await message.channel.send(
                "このDMには自動対応していません。\n"
                "遅刻/早退の時刻を登録するには、当Botが送信した『確認メッセージ』に対して返信してください。\n"
                "スマホではメッセージを長押しして「返信」を選択してください。"
            )


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
    if channel is None:
        return
    ch_name = str(channel)
    if ch_name not in RSVP_CHANNELS:
        return

    sheet_key = _sheet_key_from_channel_name(ch_name)
    if sheet_key is None:
        return
    bridge = _bridge_for_sheet_key(sheet_key)

    raw_name = payload.emoji.name
    emoji_name = _norm_emoji(raw_name)
    message_id = payload.message_id

    # 出席系ステータス更新 ------------------------------------
    status = status_from_emoji(emoji_name)
    if status:
        await bridge.update_status_async(message_id, member.id, status)

        # ---- 遅刻／早退 → DM で時刻を問い合わせ ----
        if status in {"遅刻", "早退"}:
            msg = await channel.fetch_message(message_id)
            # [変更] 日付解析
            dt = _parse_date_from_msg(msg)
            # 日付不明なら投稿日で
            if dt:
                date_str_jp = f"{dt.month:02d}月{dt.day:02d}日" # 年なし
            else:
                jst = msg.created_at.replace(tzinfo=timezone.utc) + timedelta(hours=9)
                date_str_jp = f"{jst.month:02d}月{jst.day:02d}日"

            await _send_time_prompt(
                member=member,
                server_message_id=message_id,
                date_str_jp=date_str_jp,
                status=status,
                sheet_key=sheet_key,
            )
        return

    # ✅: 練習日列の手動追加 -----------------------------------
    if emoji_name == CHECKMARK_EMOJI:
        msg = await channel.fetch_message(message_id)

        # 0) 列の追加（既存なら既存列番号が返るだけ）
        # [変更] 新しいヘッダ生成ロジック
        header_str = _get_header_from_msg(msg)
        await bridge.add_event_column_async(header_str, message_id)

        # 1) リアクションを集計して列一括更新（1 API call）
        values_by_member = await _collect_attendance_from_reactions(msg)
        await bridge.bulk_update_status_column_async(
            message_id,
            values_by_member,
        )

        # 2) ✅ を消す
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
    
    # [変更] 画像生成用の日付文字列も新ロジックから取得
    # ここでは年を含まない短い形式が欲しいので _parse_date_from_msg を使う
    dt = _parse_date_from_msg(msg)
    if dt:
        weekday_jp = _WEEKDAYS_JP[dt.weekday()]
        date_str_jp = f"{dt.month:02d}月{dt.day:02d}日({weekday_jp})"
    else:
        # フォールバック
        jst = msg.created_at.replace(tzinfo=timezone.utc) + timedelta(hours=9)
        weekday_jp = _WEEKDAYS_JP[jst.weekday()]
        date_str_jp = f"{jst.month:02d}月{jst.day:02d}日({weekday_jp})"

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
    """乗り番（パート・席次）を SpreadSheet に登録する（全奏のみ）"""
    # ---------- 入力チェック ----------
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
            reaction, _ = await bot.wait_for("reaction_add", timeout=30.0, check=check)
        except asyncio.TimeoutError:
            await warn.edit(content="タイムアウトしました。上書きは行われませんでした。")
            return

        # -------- ユーザー反応を判定 ----------
        if str(reaction.emoji) == CANCEL_EMOJI:
            await warn.edit(content="キャンセルしました。上書きは行われませんでした。")
            return

        # -------- 上書き実行（✅ が押された場合） ----------
        await sheet_bridge_ensou.append_member_async(
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
# コマンド: サーバーメンバー → SpreadSheet 同期（全奏のみ）
# ============================================================
@bot.command(
    name="syncmembers",
    help="$ syncmembers : サーバーメンバーの Discord 表示名 / ID を "
         "SpreadSheet（全奏）に追加または上書き",
)
@commands.has_any_role(*OUTPUT_ROLES)
async def sync_members_cmd(ctx: commands.Context) -> None:
    with_part, no_part = await _sync_members_to_sheet(ctx.guild, gs_manager_ensou)
    await ctx.send(
        f"✅ 同期完了: 追加 {with_part + no_part} 名 "
        f"パート判定あり {with_part} 名, "
        f"パート無し {no_part} 名"
    )
    await ctx.message.add_reaction("✅")

@bot.command(
    name="sync",
    help="$ sync : 最新のRSVP回答状況をスプレッドシートに強制同期",
)
@commands.has_any_role(*OUTPUT_ROLES)
async def sync_rsvp_cmd(ctx: commands.Context) -> None:
    """現在のDiscord RSVP Channelの最新回答に合わせてシートを更新する"""
    
    # Command_channel 以外での実行を制限したい場合は以下のコメントアウトを外す
    # if str(ctx.channel) not in COMMAND_CHANNELS:
    #     return
    msg = await ctx.send("🔄 最新の回答状況を同期中...")
    
    count = await _sync_latest_rsvp_in_guild(ctx.guild)
    
    await msg.edit(content=f"✅ 同期完了: {count} 件のRSVPチャンネルを更新しました。")
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

# JST (UTC+9)
JST = timezone(timedelta(hours=9))

def _parse_date_from_msg(msg: discord.Message) -> datetime | None:
    """
    メッセージから練習日の datetime オブジェクトを推定して返す。
    年は「メッセージの投稿日時」を基準にする。
    原則として「投稿日よりも過去の練習日はあり得ない」という前提で、
    同年の日付が投稿日より過去になる場合は、翌年と判定する。
    """
    text_first = msg.content.splitlines()[0] if msg.content else ""
    text_first = text_first.strip()
    
    # 投稿日時（JST）
    posted_at = msg.created_at.astimezone(JST)
    posted_date = posted_at.date()

    # --- パターンA: yyyy-mm-dd (明示) ---
    m_full = _DATE_RE_FULL.search(text_first)
    if m_full:
        y, m, d = map(int, m_full.groups())
        return datetime(y, m, d).astimezone(JST)

    # --- パターンB: 10月DD日 (未確定) ---
    m_und = _DATE_RE_JP_UNDECIDED.search(text_first)
    if m_und:
        month = int(m_und.group(1))
        year = posted_at.year
        # 「投稿された月」よりも「指定月」が過去なら、翌年の話をしているとみなす
        if month < posted_at.month:
             year += 1
        # 日付比較用に仮で1日を入れて返す
        return datetime(year, month, 1).astimezone(JST)

    # --- パターンC: mm-dd / mm月dd日 (年補完) ---
    for pat in (_DATE_RE_MD, _DATE_RE_JP):
        m = pat.search(text_first)
        if m:
            month, day = map(int, m.groups())
            
            try:
                # まず「投稿年」で日付を作ってみる
                candidate = datetime(posted_at.year, month, day).astimezone(JST)
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


def _get_header_from_msg(msg: discord.Message) -> str:
    """
    SpreadSheetのヘッダ用文字列を生成する。
    """
    dt = _parse_date_from_msg(msg)
    
    # 日付解析不能だった場合: 投稿日時をそのまま使う
    if dt is None:
        jst = msg.created_at.astimezone(JST)
        weekday_jp = _WEEKDAYS_JP[jst.weekday()]
        return f"{jst.year}年{jst.month:02d}月{jst.day:02d}日({weekday_jp})"
        
    # 未定パターンかどうかの判定（ヘッダ文字列生成のため）
    # dt は _parse_date_from_msg で年補完済みなので、その年を使う
    text_first = msg.content.splitlines()[0] if msg.content else ""
    if _DATE_RE_JP_UNDECIDED.search(text_first):
         return f"{dt.year}年{dt.month:02d}月DD日(未定)"
    
    # 確定日付
    weekday_jp = _WEEKDAYS_JP[dt.weekday()]
    return f"{dt.year}年{dt.month:02d}月{dt.day:02d}日({weekday_jp})"


# ============================================================
# Bot 起動
# ============================================================
bot.run(BOT_TOKEN)