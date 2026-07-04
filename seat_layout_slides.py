# seat_layout_slides.py
"""
Google Slides 上の図形から
  - 座席 (パート‐番号) の中心座標とサイズ
  - 凡例５種（出席／欠席／遅刻／早退／未回答）の座標・サイズ・色
  - logo_space / title_space（任意）の座標・サイズ
を抽出して draw.py へ渡すクラス
"""

from __future__ import annotations
import json
import re
from pathlib import Path
from typing import Dict, Tuple, Optional

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# 出力画像サイズ（draw.py と揃える）
IMG_W, IMG_H = 1920, 1080

_PART_NUM_RE = re.compile(r'^([A-Za-z0-9&]+)-(\d+)$')
_LEGEND_SET = {"出席", "欠席", "遅刻", "早退", "未回答"}
_CACHE_DIR = Path(".seat_layout_cache")
_CACHE_DIR.mkdir(exist_ok=True)


def _rgb_float_to_int(rgb: Dict[str, float]) -> Tuple[int, int, int]:
    """Slides API の 0-1 float RGB → 0-255 int RGB に変換"""
    r = int(rgb.get("red",   0) * 255)
    g = int(rgb.get("green", 0) * 255)
    b = int(rgb.get("blue",  0) * 255)
    return (r, g, b)


class SeatLayoutSlides:
    """
    self.seats   : {(part, num): {'center':(x,y), 'size':(w,h)}}
    self.legends : {label: {'center':(x,y), 'size':(w,h),
                            'fill':(r,g,b), 'font':(r,g,b)}}
    self.conductor_pos : (x,y) | None
    self.logo_box : Optional[Dict]
    self.title_box : Optional[Dict]
    use_slides : bool, default False
        - False: 先にローカルキャッシュを探し、
                 無ければ Slides API から取得して保存
        - True : 先にキャッシュがあっても必ず Slides API を呼ぶ
    """
    def __init__(self,
                 presentation_id: str,
                 credential_json: str,
                 slide_index: int = 1,
                 use_slides: bool = False,
                 ):
        self._cache_path = self._build_cache_path(presentation_id,
                                                  slide_index)
        # ===== キャッシュ優先読み込み =====
        if not use_slides and self._cache_path.exists():
            # キャッシュが有れば必ず使う（API は呼ばない）
            try:
                self._load_from_cache()
            except Exception as e:  # 破損など
                raise RuntimeError(
                    f"キャッシュファイル '{self._cache_path}' が読めません: {e}"
                ) from None
            return

        # ----- use_slides=True もしくはキャッシュ無し -----
        try:
            self._build_from_slides(
                presentation_id, credential_json, slide_index
            )
        except HttpError as e:
            if e.resp.status == 404:
                raise ValueError(
                    f"presentation_id '{presentation_id}' が "
                    "Google Slides 上に見つかりません（HTTP 404）"
                ) from None
            # それ以外は元の例外をそのまま再送
            raise

        # 正常取得できた場合のみキャッシュ保存
        self._save_to_cache()

    # =============================================================
    # 内部ユーティリティ
    # =============================================================
    @staticmethod
    def _build_cache_path(presentation_id: str, slide_index: int) -> Path:
        safe_id = re.sub(r"[^0-9A-Za-z_-]", "_", presentation_id)
        fname = f"{safe_id}_{slide_index}.json"
        return _CACHE_DIR / fname

    # ------------------ cache I/O -------------------------------
    @staticmethod
    def _shape_info_to_json(info: Dict) -> Dict:
        out = dict(info)
        out["center"] = list(info["center"])
        out["size"] = list(info["size"])
        if "fill" in info:
            out["fill"] = list(info["fill"])
        if "font" in info:
            out["font"] = list(info["font"])
        return out

    @staticmethod
    def _shape_info_from_json(info: Dict) -> Dict:
        out = dict(info)
        out["center"] = tuple(info["center"])
        out["size"] = tuple(info["size"])
        if "fill" in info:
            out["fill"] = tuple(info["fill"])
        if "font" in info:
            out["font"] = tuple(info["font"])
        return out

    def _load_from_cache(self) -> None:
        with self._cache_path.open("r", encoding="utf-8") as fp:
            data = json.load(fp)

        self.seats = {}
        for key, info in data["seats"].items():
            part, num_str = key.rsplit(":", 1)
            self.seats[(part, int(num_str))] = self._shape_info_from_json(info)

        self.legends = {
            label: self._shape_info_from_json(info)
            for label, info in data["legends"].items()
        }

        self.conductor_pos = (
            tuple(data["conductor_pos"]) if data["conductor_pos"] is not None else None
        )
        self.logo_box = (
            self._shape_info_from_json(data["logo_box"])
            if data["logo_box"] is not None
            else None
        )
        self.title_box = (
            self._shape_info_from_json(data["title_box"])
            if data["title_box"] is not None
            else None
        )

    def _save_to_cache(self) -> None:
        data = {
            "seats": {
                f"{part}:{num}": self._shape_info_to_json(info)
                for (part, num), info in self.seats.items()
            },
            "legends": {
                label: self._shape_info_to_json(info)
                for label, info in self.legends.items()
            },
            "conductor_pos": (
                list(self.conductor_pos) if self.conductor_pos is not None else None
            ),
            "logo_box": (
                self._shape_info_to_json(self.logo_box)
                if self.logo_box is not None
                else None
            ),
            "title_box": (
                self._shape_info_to_json(self.title_box)
                if self.title_box is not None
                else None
            ),
        }
        with self._cache_path.open("w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)

    @staticmethod
    def _extract_label(elem: dict) -> Optional[str]:
        """図形テキストの先頭有効行を取り出す"""
        shape = elem.get("shape")
        if not shape:
            return None
        textobj = shape.get("text")
        if not textobj:
            return None
        for te in textobj.get("textElements", []):
            run = te.get("textRun")
            if not run:
                continue
            content = run.get("content", "")
            if content.strip():
                return content.split("\n")[0].strip()
        return None

    @staticmethod
    def _extract_colors(elem: dict) -> Tuple[Tuple[int, int, int],
                                             Tuple[int, int, int]]:
        """(fill_color_rgb, font_color_rgb) を返す"""
        # 図形塗りつぶし
        # shapeProperties は pageElement['shape']['shapeProperties'] に格納されている
        fill_rgb = (255, 255, 255)  # default white
        props = elem.get("shape", {}).get("shapeProperties", {})
        bgfill = props.get("shapeBackgroundFill", {})
        solid = bgfill.get("solidFill", {})
        color = solid.get("color", {}).get("rgbColor")
        if color:
            fill_rgb = _rgb_float_to_int(color)

        # 文字色：先頭 textRun の style を参照
        font_rgb = (0, 0, 0)        # default black
        shape = elem.get("shape", {})
        text = shape.get("text", {})
        for te in text.get("textElements", []):
            run = te.get("textRun")
            if not run:
                continue
            style = run.get("style", {})
            fg = style.get("foregroundColor", {}).get(
                "opaqueColor", {}).get("rgbColor")
            if fg:
                font_rgb = _rgb_float_to_int(fg)
            break
        return fill_rgb, font_rgb

    def _build_shape_info(self, elem: dict,
                          page_w_pt: float, page_h_pt: float,
                          need_style: bool = False) -> Dict:
        """共通情報を dict にまとめる"""
        size = elem["size"]
        width_pt = size["width"]["magnitude"]
        height_pt = size["height"]["magnitude"]

        tr = elem["transform"]
        tx_pt = tr.get("translateX", 0)
        ty_pt = tr.get("translateY", 0)
        sx = tr.get("scaleX", 1)
        sy = tr.get("scaleY", 1)

        center_x_pt = tx_pt + width_pt * sx / 2
        center_y_pt = ty_pt + height_pt * sy / 2

        # pt → px スケール変換
        cx = center_x_pt * IMG_W / page_w_pt
        cy = center_y_pt * IMG_H / page_h_pt
        w = width_pt * sx * IMG_W / page_w_pt
        h = height_pt * sy * IMG_H / page_h_pt

        from typing import Any
        info: Dict[str, Any] = {"center": (cx, cy), "size": (w, h)}

        if need_style:
            fill_rgb, font_rgb = self._extract_colors(elem)
            info["fill"] = fill_rgb
            info["font"] = font_rgb
        return info

    def _build_from_slides(
            self,
            presentation_id: str,
            credential_json: str,
            slide_index: int,
    ) -> None:
        scopes = ["https://www.googleapis.com/auth/presentations.readonly"]
        creds = Credentials.from_service_account_file(credential_json,
                                                      scopes=scopes)
        service = build("slides", "v1",
                        credentials=creds,
                        cache_discovery=False)

        pres = service.presentations().get(
            presentationId=presentation_id).execute()

        page_size = pres["pageSize"]
        page_w_pt = page_size["width"]["magnitude"]
        page_h_pt = page_size["height"]["magnitude"]

        slide = pres["slides"][slide_index]

        self.seats:   Dict[Tuple[str, int], Dict] = {}
        self.legends: Dict[str, Dict] = {}
        self.conductor_pos: Optional[Tuple[float, float]] = None
        self.logo_box: Optional[Dict] = None
        self.title_box: Optional[Dict] = None

        for elem in slide["pageElements"]:
            label = self._extract_label(elem)
            if not label:
                continue

            # ---------- 凡例（Legend） ----------
            if label in _LEGEND_SET:
                self.legends[label] = self._build_shape_info(elem,
                                                             page_w_pt,
                                                             page_h_pt,
                                                             need_style=True)
                continue

            # ---------- 座席（Part-Num） ----------
            m = _PART_NUM_RE.match(label)
            if m:
                part, num = m.group(1), int(m.group(2))
                info = self._build_shape_info(elem, page_w_pt, page_h_pt)
                self.seats[(part, num)] = info
                if (part, num) == ("Cond", 0):
                    self.conductor_pos = info["center"]
                continue

            # ---------- ロゴ領域 ----------
            if label.lower() == "logo_space":
                self.logo_box = self._build_shape_info(elem,
                                                       page_w_pt,
                                                       page_h_pt)
                continue

            # ---------- タイトル領域 ---------
            if label.lower() == "title_space":
                self.title_box = self._build_shape_info(
                    elem, page_w_pt, page_h_pt
                )
                continue


# ------------------------------------------------------------
# Google API を用いた SeatLayoutSlides テスト
# ------------------------------------------------------------
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="SeatLayoutSlides Google-API 動作テスト")
    parser.add_argument("--presentation_id", required=True,
                        help="Google Slides のファイル ID "
                             "(URL 中 .../d/<この部分>/edit)")
    parser.add_argument("--credentials", default="credentials.json",
                        help="Service Account 秘密鍵 json パス "
                             "(デフォルト: credentials.json)")
    parser.add_argument("--slide_index", type=int, default=1,
                        help="何枚目のスライドを読むか (0 = 1 枚目)")
    parser.add_argument(
        "--use-slides",
        action="store_true",
        help="キャッシュが存在しても必ず Google Slides API から "
        "レイアウトを取得する",
    )

    args = parser.parse_args()

    # SeatLayoutSlides を生成
    layout = SeatLayoutSlides(
        presentation_id=args.presentation_id,
        credential_json=args.credentials,
        slide_index=args.slide_index,
        use_slides=args.use_slides,
    )

    # ---------------- 座席 ----------------
    print("\n=== 座席 (part-num) 一覧 ===")

    _PART_ORDER = [
            "Vn1st", "Vn2nd", "Va", "Vc", "Cb",
            "Fl", "Ob", "Cl", "Fg", "Hr",
            "Tp", "Tb", "Tuba", "Timp", "Perc",
            "Pf", "Hp",
        ]

    def _seat_sort_key(item):
        (part, num), _info = item
        try:
            idx = _PART_ORDER.index(part)
        except ValueError:
            # 規定外パートはリスト後方へ／アルファベット順
            idx = len(_PART_ORDER)
        return (idx, part, num)

    for (part, num), info in sorted(layout.seats.items(), key=_seat_sort_key):
        cx, cy = info["center"]
        w,  h = info["size"]
        print(f"{part}-{num:>2}: center=({cx:7.1f}, {cy:7.1f}), "
              f"size=({w:6.1f}×{h:6.1f})")

    print(f"\n総座席数: {len(layout.seats)}")

    # ---------------- 凡例 ----------------
    print("\n=== 凡例 5 種 ===")
    for label, info in layout.legends.items():
        cx, cy = info["center"]
        w, h = info["size"]
        fill = info["fill"]
        font = info["font"]
        print(f"{label}: center=({cx:.1f}, {cy:.1f}), "
              f"size=({w:.1f}×{h:.1f}), fill={fill}, fontcolor={font}")

    # ---------------- 指揮者 ----------------
    if layout.conductor_pos:
        cx, cy = layout.conductor_pos
        print(f"\n指揮者 (Cond-0) 位置: ({cx:.1f}, {cy:.1f})")
    else:
        print("\n指揮者 (Cond-0) は見つかりませんでした")
