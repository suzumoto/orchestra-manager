# message_window.py
"""
Bot のお知らせ（出欠リマインド・遅刻/早退の時刻確認）を、RPG のメッセージウィンドウ風の
画像にする。

青のグラデーションの背景・銀の縁取り・白い文字に影、という見た目。
メンション・リンク・ボタンは画像の外（メッセージ本文やボタン）に置く前提で、
ここでは文面だけを描く。

使い方: python message_window.py → generated/ に見本を出力する
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT_PATH = "GenShinGothic-Medium.ttf"
SCALE = 2           # 2 倍の解像度で出力する（スマホでもくっきり見えるように）
# ウィンドウの幅（等倍換算）。スマホは画像を画面の幅に合わせて表示するので、
# 幅に対する文字の大きさの割合がそのまま見た目の文字の大きさになる
WIDTH = 640
# ウィンドウのまわりの透明な余白（等倍換算）。Discord のスマホアプリは画像の四隅を
# 丸く切り抜いて表示するので、切られるのが余白だけになるようにする
MARGIN = 12

# 4 隅の背景色（左上が明るい青、右下が濃紺）
_CORNER_COLORS = ((40, 72, 200), (16, 30, 120), (16, 30, 120), (6, 8, 56))
_TEXT = (255, 255, 255)
_TITLE = (200, 210, 255)
_SHADOW = (10, 10, 30)
_OUTER_LINE = (30, 30, 40)
_INNER_LINE = (20, 22, 50)


def _gradient_mask(w: int, h: int, horizontal: bool) -> Image.Image:
    """0 → 255 の直線的なグラデーションのマスク（横なら左→右、縦なら上→下）"""
    if horizontal:
        return Image.linear_gradient("L").rotate(90, expand=True).resize(
            (w, h), Image.Resampling.BILINEAR)
    return Image.linear_gradient("L").resize((w, h), Image.Resampling.BILINEAR)


def render_window(title: str, lines: list[str], width: int = WIDTH) -> Image.Image:
    """
    メッセージウィンドウの画像（RGBA、角は透過）を返す。

    入力
    ----
    title : str
        上の小さい行（用件）。空なら描かない
    lines : list[str]
        本文の各行
    width : int
        ウィンドウの幅（等倍換算の px）
    """
    s = SCALE
    pad_x, pad_y, line_h, title_h = 30 * s, 24 * s, 42 * s, 34 * s
    body_font = ImageFont.truetype(FONT_PATH, 28 * s)
    title_font = ImageFont.truetype(FONT_PATH, 23 * s)
    w = width * s
    lines = [wrapped for line in lines for wrapped in _wrap(line, body_font, w - pad_x * 2)]
    h = pad_y * 2 + line_h * len(lines) + (title_h if title else 0)

    # 背景: 4 隅の色の双線形グラデーション（Pillow だけで描く）
    across = _gradient_mask(w, h, horizontal=True)   # 左 0 → 右 255
    down = _gradient_mask(w, h, horizontal=False)    # 上 0 → 下 255
    tl, tr, bl, br = (Image.new("RGB", (w, h), c) for c in _CORNER_COLORS)
    grad = Image.composite(Image.composite(br, bl, across), Image.composite(tr, tl, across), down)
    radius = 14 * s
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius, fill=255)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    img.paste(grad, mask=mask)

    # 縁: 外側の暗い線 → 銀の太線（上が明るく下が暗い）→ 内側の暗い線
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, w - 1, h - 1), radius, outline=_OUTER_LINE, width=s)
    silver = Image.composite(Image.new("RGB", (w, h), (150, 150, 158)),
                             Image.new("RGB", (w, h), (235, 235, 243)), down)
    ring = Image.new("L", (w, h), 0)
    ImageDraw.Draw(ring).rounded_rectangle((s, s, w - 1 - s, h - 1 - s), radius - s, outline=255, width=4 * s)
    img.paste(silver, mask=ring)
    d.rounded_rectangle((5 * s, 5 * s, w - 1 - 5 * s, h - 1 - 5 * s), radius - 5 * s, outline=_INNER_LINE, width=s)

    def text(y: int, line: str, font: ImageFont.FreeTypeFont, fill: tuple[int, int, int]) -> None:
        d.text((pad_x + 2 * s, y + 2 * s), line, font=font, fill=_SHADOW)
        d.text((pad_x, y), line, font=font, fill=fill)

    y = pad_y
    if title:
        text(y, title, title_font, _TITLE)
        y += title_h
    for line in lines:
        text(y, line, body_font, _TEXT)
        y += line_h

    m = MARGIN * s
    canvas = Image.new("RGBA", (w + m * 2, h + m * 2), (0, 0, 0, 0))
    canvas.paste(img, (m, m), img)
    return canvas


# 行頭に来てはいけない文字（句読点・閉じ括弧など）
_NO_LINE_START = set("、。，．・：；？！）」』】〉》ー…ぁぃぅぇぉっゃゅょァィゥェォッャュョ")


def _wrap(line: str, font: ImageFont.FreeTypeFont, max_w: int) -> list[str]:
    """
    1 行を幅 max_w に収まるよう折り返す。文面中の '|'（改行してよい位置の目印、
    描画しない）と読点・句点・空白の直後を優先して改行し、そこが無ければ
    1 文字単位で折り返す（行頭禁則つき）。
    """
    out: list[str] = []
    cur = ""      # 描く文字列
    breaks: list[int] = []  # cur の中で改行してよい位置（その位置の直前で切る）
    for ch in line:
        if ch == "|":
            breaks.append(len(cur))
            continue
        if cur and font.getlength(cur + ch) > max_w and ch not in _NO_LINE_START:
            cands = [b for b in breaks if 0 < b <= len(cur)]
            cands += [i + 1 for i, c in enumerate(cur) if c in "、。 " and i + 1 < len(cur)]
            cut = max(cands) if cands else len(cur)
            out.append(cur[:cut].rstrip())
            cur = cur[cut:].lstrip()
            breaks = [b - cut for b in breaks if b > cut]
        cur += ch
    return out + [cur] if cur else out


# ---- 文面（今の Bot のメッセージの文言。リンクとメンションは画像の外に置く） ----
def reminder_window(month: int, day: int, days_before: int) -> Image.Image:
    """出欠リマインドの画像"""
    label = "前日" if days_before == 1 else f"{days_before}日前"
    return render_window(
        f"出欠リマインド（練習{label}）",
        [f"{month}月{day}日の練習の出欠が|未回答です。",
         "該当する投稿に|リアクションで|回答してください。"],
    )


def time_input_window(date_str_jp: str, status: str) -> Image.Image:
    """遅刻・早退の時刻確認の画像（status は '遅刻' か '早退'）"""
    verb = "到着" if status == "遅刻" else "退出"
    return render_window(
        f"{status}時刻の入力",
        [f"{date_str_jp}の練習に|{status}の予定ですね。",
         f"下のボタンから|{verb}予定時刻を|入力してください。",
         "時刻を間違えたときは、|もう一度ボタンを押して|入力し直せば|上書きされます。"],
    )


if __name__ == "__main__":
    out = Path("generated")
    out.mkdir(exist_ok=True)
    samples = {
        "window_reminder_3days.png": reminder_window(11, 7, 3),
        "window_reminder_1day.png": reminder_window(11, 7, 1),
        "window_time_late.png": time_input_window("11月7日", "遅刻"),
        "window_time_leave.png": time_input_window("11月7日", "早退"),
    }
    for name, im in samples.items():
        im.save(out / name)
        print(out / name, im.size)
