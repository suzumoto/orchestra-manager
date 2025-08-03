# draw.py
"""
SeatLayoutSlides の情報を使って
座席と凡例を“スライド通りの位置・サイズ・色”で描画する
"""

from pathlib import Path
from typing import Tuple

from PIL import Image, ImageDraw, ImageFont

# -------- フォント --------
NAME_FONT = ImageFont.truetype("GenShinGothic-Medium.ttf", 24)
PART_FONT = ImageFont.truetype("GenShinGothic-Medium.ttf", 20)
COND_FONT = ImageFont.truetype("GenShinGothic-Medium.ttf", 28)
PROGRAM_FONT = ImageFont.truetype("GenShinGothic-Medium.ttf", 30)

BLACK = (0, 0, 0)
WHITE = (255, 255, 255)

CANVAS_SIZE = (1920, 1080)


class PlayerBoxDrawer:
    def __init__(self, seat_layout, background_color=WHITE):
        """
        seat_layout : SeatLayoutSlides インスタンス
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
        key = (part, num)
        if key not in self.seat_layout.seats:
            raise ValueError(f"座標未定義: {key}")

        info = self.seat_layout.seats[key]
        cx, cy = info["center"]
        w,  h = info["size"]

        ul = (cx - w / 2, cy - h / 2)
        lr = (cx + w / 2, cy + h / 2)

        self.draw.rectangle((ul, lr), fill=fill_color,
                            outline=BLACK, width=2)
        self.draw.text((cx, cy - h * 0.25), part,
                       font=PART_FONT, fill=font_color, anchor="mm")
        self.draw.text((cx, cy + h * 0.25), name,
                       font=NAME_FONT, fill=font_color, anchor="mm")

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
        key = (part, num)
        if key not in self.seat_layout.seats:
            raise ValueError(f"座標未定義: {key}")

        info = self.seat_layout.seats[key]
        cx, cy = info["center"]
        w, h = info["size"]

        ul = (cx - w / 2, cy - h / 2)
        lr = (cx + w / 2, cy + h / 2)
        mid_x = cx

        # 1) 左右を塗り分け（枠を描かず fill のみ）
        self.draw.rectangle((ul, (mid_x, lr[1])), fill=fill_left)
        self.draw.rectangle(((mid_x, ul[1]), lr), fill=fill_right)

        # 2) 外枠を最後に描画  ← これで 1 色 BOX とまったく同じ見た目
        self.draw.rectangle((ul, lr), outline=BLACK, width=2)

        # 3) テキスト
        self.draw.text(
            (cx, cy - h * 0.25),
            part,
            font=PART_FONT,
            fill=font_color,
            anchor="mm",
        )
        self.draw.text(
            (cx, cy + h * 0.25),
            name,
            font=NAME_FONT,
            fill=font_color,
            anchor="mm",
        )

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

        text = f"{date_str} {program_name}"
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
        Path(filepath).parent.mkdir(parents=True, exist_ok=True)
        self.img.save(filepath)

    # =================================================
    # 内部描画ヘルパ
    # =================================================
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
