# draw.py
"""
SeatLayoutSlides の情報を使って
座席と凡例を“スライド通りの位置・サイズ・色”で描画する
"""

import re
from pathlib import Path
from typing import Tuple

from PIL import Image, ImageDraw, ImageFont, features

# -------- フォント --------
NAME_FONT = ImageFont.truetype("GenShinGothic-Medium.ttf", 24)
PART_FONT = ImageFont.truetype("GenShinGothic-Medium.ttf", 20)
COND_FONT = ImageFont.truetype("GenShinGothic-Medium.ttf", 28)
PROGRAM_FONT = ImageFont.truetype("GenShinGothic-Medium.ttf", 30)
# 名前が枠の幅に収まらないときに縮める下限の大きさと、枠の左右に残す余白（px）
NAME_MIN_SIZE = 14
NAME_PADDING = 4

# -------- 絵文字（名前に含まれる 🍊 など） --------
# 源真ゴシックには絵文字が無いので、絵文字の部分だけカラー絵文字フォントで描く。
# フォントは見つかった最初のものを使う（リポジトリには含めない）。
# Raspberry Pi などの Debian 系: sudo apt install fonts-noto-color-emoji
_EMOJI_FONT_PATHS = (
    "NotoColorEmoji.ttf",
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
    "/usr/share/fonts/noto/NotoColorEmoji.ttf",
    r"C:\Windows\Fonts\seguiemj.ttf",
    "/System/Library/Fonts/Apple Color Emoji.ttc",
)
# Noto Color Emoji はこのサイズのビットマップしか持たないため、この大きさで描いて縮める
_EMOJI_RENDER_SIZE = 109
_EMOJI_RE = re.compile(
    "(?:[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\u2300-\u23FF]"
    "[\uFE0F\U0001F3FB-\U0001F3FF]*"
    "(?:\u200D[\U0001F000-\U0001FAFF\u2600-\u27BF][\uFE0F\U0001F3FB-\U0001F3FF]*)*)+"
)


# 肌の色・ZWJ でつないだ絵文字・国旗を 1 つにまとめて描くには Raqm が要る
_HAS_RAQM = features.check("raqm")


def _load_emoji_font() -> ImageFont.FreeTypeFont | None:
    engine = ImageFont.Layout.RAQM if _HAS_RAQM else ImageFont.Layout.BASIC
    for path in _EMOJI_FONT_PATHS:
        try:
            return ImageFont.truetype(path, _EMOJI_RENDER_SIZE, layout_engine=engine)
        except OSError:
            continue
    print("[draw] カラー絵文字フォントが見つからないため、名前の絵文字は描画しません")
    return None


EMOJI_FONT = _load_emoji_font()

BLACK = (0, 0, 0)
WHITE = (255, 255, 255)

CANVAS_SIZE = (1920, 1080)


class PlayerBoxDrawer:
    def __init__(self, seat_layout, background_color=WHITE):
        """
        座席図キャンバスを作成し、ロゴ・凡例・指揮者ボックスを先に描画する。

        入力
        ----
        seat_layout : SeatLayoutSlides
            座席・凡例・ロゴ枠などの位置情報を持つオブジェクト
        background_color : tuple[int, int, int]
            キャンバスの背景色（デフォルト白）

        出力
        ----
        なし（self.img に描画済みの画像を保持する）
        """
        self.seat_layout = seat_layout
        self.img = Image.new("RGB", CANVAS_SIZE, background_color)
        self.draw = ImageDraw.Draw(self.img)

        self._draw_logo()
        self._draw_legends()
        self._draw_conductor()

    # -------------------------------------------------
    # 座席を描画
    # -------------------------------------------------
    def draw_playerbox(self, part: str, num: int,
                       name: str,
                       fill_color: Tuple[int, int, int],
                       font_color: Tuple[int, int, int]) -> None:
        """
        単色 1 枠の座席 BOX を描画する（出席/欠席など単一状態向け）。

        入力
        ----
        part, num : str, int
            座席を特定するパート名と席次（Slides 側の座席キーに対応）
        name : str
            BOX に表示する奏者名
        fill_color, font_color : tuple[int, int, int]
            塗りつぶし色・文字色

        出力
        ----
        なし（self.img に直接描画する）
        """
        cx, cy, w, h, ul, lr = self._seat_geometry(part, num)

        self.draw.rectangle((ul, lr), fill=fill_color,
                            outline=BLACK, width=2)
        self._draw_seat_text(cx, cy, w, h, part, name, font_color)

    # -------------------------------------------------
    # 左右 2 色分割 BOX を描く
    # -------------------------------------------------
    def draw_playerbox_split(  # noqa: PLR0913
        self,
        part: str,
        num: int,
        name: str,
        fill_left: tuple[int, int, int],
        fill_right: tuple[int, int, int],
        font_color: tuple[int, int, int] = BLACK,
    ) -> None:
        """
        左右 2 色に塗り分けた座席 BOX を描画する（遅刻+早退の併記など）。

        入力
        ----
        part, num : str, int
            座席を特定するパート名と席次
        name : str
            BOX に表示する奏者名
        fill_left, fill_right : tuple[int, int, int]
            左半分・右半分の塗りつぶし色
        font_color : tuple[int, int, int]
            文字色

        出力
        ----
        なし（self.img に直接描画する）
        """
        cx, cy, w, h, ul, lr = self._seat_geometry(part, num)
        mid_x = cx

        # 1) 左右を塗り分け（枠を描かず fill のみ）
        self.draw.rectangle((ul, (mid_x, lr[1])), fill=fill_left)
        self.draw.rectangle(((mid_x, ul[1]), lr), fill=fill_right)

        # 2) 外枠を最後に描画  ← これで 1 色 BOX とまったく同じ見た目
        self.draw.rectangle((ul, lr), outline=BLACK, width=2)

        self._draw_seat_text(cx, cy, w, h, part, name, font_color)

    def _seat_geometry(
        self, part: str, num: int
    ) -> tuple[float, float, float, float, tuple[float, float], tuple[float, float]]:
        """
        座席の中心座標・サイズ・矩形の左上/右下座標をまとめて求める。

        入力
        ----
        part, num : str, int
            座席を特定するパート名と席次

        出力
        ----
        (cx, cy, w, h, upper_left, lower_right)
        """
        key = self._resolve_seat_key(part, num)
        info = self.seat_layout.seats[key]
        cx, cy = info["center"]
        w, h = info["size"]
        ul = (cx - w / 2, cy - h / 2)
        lr = (cx + w / 2, cy + h / 2)
        return cx, cy, w, h, ul, lr

    def _draw_seat_text(
        self,
        cx: float,
        cy: float,
        w: float,
        h: float,
        part: str,
        name: str,
        font_color: Tuple[int, int, int],
    ) -> None:
        """
        座席 BOX 内に「パート名（上段）」「奏者名（下段）」を描画する。

        入力
        ----
        cx, cy, w, h : float
            BOX の中心座標と幅・高さ（テキスト位置の計算と、名前を幅に収めるのに使う）
        part, name : str
            表示するパート名・奏者名
        font_color : tuple[int, int, int]
            文字色

        出力
        ----
        なし（self.draw に直接描画する）
        """
        self.draw.text((cx, cy - h * 0.25), part,
                       font=PART_FONT, fill=font_color, anchor="mm")
        self._draw_text_with_emoji(cx, cy + h * 0.25, name, NAME_FONT, font_color,
                                   max_width=w - NAME_PADDING * 2)

    def _draw_text_with_emoji(
        self,
        cx: float,
        cy: float,
        text: str,
        font: ImageFont.FreeTypeFont,
        fill: Tuple[int, int, int],
        max_width: float | None = None,
    ) -> None:
        """
        (cx, cy) を中心に text を描く。絵文字の部分はカラー絵文字フォントで描き、
        文字の高さに合わせて縮めて並べる（絵文字フォントが無ければ絵文字は省く）。
        max_width を超える場合は、収まる大きさまで文字を小さくする（下限 NAME_MIN_SIZE）。
        """
        runs: list[tuple[bool, str]] = []  # (絵文字か, 文字列)
        pos = 0
        for m in _EMOJI_RE.finditer(text):
            if m.start() > pos:
                runs.append((False, text[pos:m.start()]))
            if EMOJI_FONT is not None:
                runs.append((True, m.group()))
            pos = m.end()
        if pos < len(text):
            runs.append((False, text[pos:]))
        runs = [(e, s.replace("️", "")) if not e else (e, s) for e, s in runs]

        def layout(f: ImageFont.FreeTypeFont) -> list[tuple[bool, str | Image.Image, float]]:
            pieces: list[tuple[bool, str | Image.Image, float]] = []
            for is_emoji, s in runs:
                if not is_emoji:
                    pieces.append((False, s, f.getlength(s)))
                    continue
                for cluster in _EMOJI_RE.findall(s) or [s]:
                    img = self._render_emoji(cluster, f.size * 1.1)
                    if img is not None:
                        pieces.append((True, img, img.width))
            return pieces

        pieces = layout(font)
        width = sum(w for _, _, w in pieces)
        # 枠からはみ出す名前は、収まる大きさまで文字を小さくする
        while max_width and width > max_width and font.size > NAME_MIN_SIZE:
            size = max(NAME_MIN_SIZE, min(font.size - 1, int(font.size * max_width / width)))
            font = font.font_variant(size=size)
            pieces = layout(font)
            width = sum(w for _, _, w in pieces)

        x = cx - width / 2
        for is_emoji, piece, w in pieces:
            if is_emoji:
                self.img.paste(piece, (round(x), round(cy - piece.height / 2)), piece)
            else:
                self.draw.text((x, cy), piece, font=font, fill=fill, anchor="lm")
            x += w

    @staticmethod
    def _render_emoji(cluster: str, height: float) -> Image.Image | None:
        """絵文字 1 つをカラーで描き、余白を切り落として高さ height に縮めた RGBA 画像を返す"""
        if not _HAS_RAQM:
            # まとめて描けないので、崩れないよう先頭の基本の絵文字だけにする（👍🏽→👍、👨‍👩‍👧→👨）
            cluster = re.sub("[🏻-🏿️]", "", cluster.split("‍")[0])
        size = _EMOJI_RENDER_SIZE * 3
        canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        ImageDraw.Draw(canvas).text((size / 2, size / 2), cluster, font=EMOJI_FONT,
                                    embedded_color=True, anchor="mm")
        bbox = canvas.getbbox()
        if bbox is None:
            return None
        canvas = canvas.crop(bbox)
        scale = height / canvas.height
        return canvas.resize((max(1, round(canvas.width * scale)), max(1, round(height))),
                             Image.Resampling.LANCZOS)

    # -------------------------------------------------
    # 日付とプログラム名を描画
    # -------------------------------------------------
    def draw_program(self, date_str: str, program_name: str) -> None:
        """
        date_str     : 'yyyy年mm月dd日(曜)' 形式の文字列
        program_name : 曲名など

        Slides 上に 'title_space' の長方形があれば
        ・左右：左寄せ
        ・上下：中央寄せ
        で描画。
        Slides上に枠が無い場合は描画しない。
        """
        title_box = getattr(self.seat_layout, "title_box", None)
        if not title_box:
            # title_space が無い → 描画しない
            return

        text = f"{date_str} {program_name}".strip()
        ul_x = title_box["center"][0] - title_box["size"][0] / 2
        cy = title_box["center"][1]

        # anchor 'lm' : left-middle（左寄せ・中央）
        self.draw.text(
            (ul_x, cy),
            text,
            font=PROGRAM_FONT,
            fill=BLACK,
            anchor="lm",
        )

    # -------------------------------------------------
    # ファイル保存
    # -------------------------------------------------
    def save(self, filepath: str | Path) -> None:
        """
        描画済み画像をファイルに保存する（親ディレクトリが無ければ作成）。

        入力
        ----
        filepath : str | Path
            保存先パス

        出力
        ----
        なし
        """
        Path(filepath).parent.mkdir(parents=True, exist_ok=True)
        self.img.save(filepath)

    # =================================================
    # 内部描画ヘルパ
    # =================================================
    def _resolve_seat_key(self, part: str, num: int) -> tuple[str, int]:
        """
        Slides 側の座席キーを解決する。
        - まず厳密一致 (part, num)
        - 見つからなければ、part の大小文字を無視して探索
        """
        key = (part, num)
        if key in self.seat_layout.seats:
            return key
        lower = part.lower()
        for (p, n) in self.seat_layout.seats.keys():
            if n == num and p.lower() == lower:
                return (p, n)
        raise ValueError(f"座標未定義: {(part, num)}")

    def _draw_conductor(self) -> None:
        pos = self.seat_layout.conductor_pos or (960, 110)
        cx, cy = pos
        size = self.seat_layout.seats.get(("Cond", 0), {}).get(
            "size", (180, 120))
        w, h = size
        ul = (cx - w/2, cy - h/2)
        lr = (cx + w/2, cy + h/2)
        self.draw.rectangle((ul, lr), fill=WHITE,
                            outline=BLACK, width=3)
        self.draw.text((cx, cy), "cond.",
                       font=COND_FONT, fill=BLACK, anchor="mm")

    def _draw_legends(self) -> None:
        """
        Google Slides 上で凡例５種の図形を置けば
        その位置・サイズ・色で描画する
        """
        for label, info in self.seat_layout.legends.items():
            cx, cy = info["center"]
            w, h = info["size"]
            fill = info["fill"]
            font = info["font"]

            ul = (cx - w/2, cy - h/2)
            lr = (cx + w/2, cy + h/2)
            self.draw.rectangle((ul, lr), fill=fill,
                                outline=BLACK, width=2)
            self.draw.text((cx, cy), label,
                           font=NAME_FONT, fill=font, anchor="mm")

    def _draw_logo(self) -> None:
        """
        Google Slides 上で 'logo_space' と書かれた長方形がある場合のみ
        - 左右：右寄せ
        - 上下：中央寄せ
        でロゴ (logo.jpg) を配置する。
        Google Slides上に描画領域が無ければ何も描画しない。
        """
        try:
            from PIL import Image as PILImage
            logo = PILImage.open("logo.jpg")
        except FileNotFoundError:
            return

        # Slides 側にロゴ枠があるか？
        box = getattr(self.seat_layout, "logo_box", None)
        if not box:
            # logo_space が無い → 描画しない
            return

        cx, cy = box["center"]
        bw, bh = box["size"]

        scale = min(bw / logo.width, bh / logo.height)
        new_w = int(logo.width * scale)
        new_h = int(logo.height * scale)
        logo = logo.resize((new_w, new_h), PILImage.Resampling.LANCZOS)

        right_x = int(cx + bw / 2)          # 枠右端
        ul_x = right_x - new_w              # 左上 X (右寄せ)
        ul_y = int(cy - new_h / 2)          # 左上 Y (中央寄せ)

        self.img.paste(logo, (ul_x, ul_y), mask=logo.convert("RGBA"))


# --------------------------------------------------------------------
# 動作確認用：python draw.py --presentation_id
# --------------------------------------------------------------------
if __name__ == "__main__":
    import argparse
    from seat_layout_slides import SeatLayoutSlides

    parser = argparse.ArgumentParser(
        description="draw.py 演奏者BOXの描画テスト"
    )
    parser.add_argument("--presentation_id", required=True)
    parser.add_argument("--credential_json", default="credentials.json")
    parser.add_argument("--slide", type=int, default=1)
    parser.add_argument(
        "--use-slides",
        action="store_true",
        help="ローカルキャッシュがあっても "
        "Slides API から座席レイアウトを取得する",
    )

    args = parser.parse_args()

    layout = SeatLayoutSlides(
        presentation_id=args.presentation_id,
        credential_json=args.credential_json,
        slide_index=args.slide,
        use_slides=args.use_slides,
    )

    pb = PlayerBoxDrawer(layout)

    for (part, num) in layout.seats.keys():
        pb.draw_playerbox(
            part, num,
            name=f"{part}{num}",
            fill_color=WHITE,
            font_color=BLACK,
        )

    pb.draw_program("2025年07月31日(木)", "メイン")
    pb.save("seat_test.png")
    print("seat_test.png を出力しました")
