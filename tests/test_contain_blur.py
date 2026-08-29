"""fit_mode='contain-blur' が縦写真を「高さ基準で収める + 同じ写真のぼかしで横を埋める」
挙動になっていることの検証。cover はキャンバス幅に合わせて上下を切るので、その対比も見る。

生成フレームは out/contain_blur_demo/ にも書き出す(目視確認用)。
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from autoslide.config import Config
from autoslide.render import _photo_frame

OUT = Path(__file__).resolve().parent.parent / "out" / "contain_blur_demo"


def _marker(w: int, h: int) -> Image.Image:
    """赤画像。最上部 60px=白, 最下部 60px=黄。
    上下端が残っているか(=縦にクロップされていないか)を色で判定できる。"""
    im = Image.new("RGB", (w, h), (200, 40, 40))
    im.paste(Image.new("RGB", (w, 60), (255, 255, 255)), (0, 0))
    im.paste(Image.new("RGB", (w, 60), (255, 230, 0)), (0, h - 60))
    return im


def _portrait_marker() -> Image.Image:
    """縦 2000x3000(実写と同じくキャンバスより大きい)。"""
    return _marker(2000, 3000)


def _landscape_marker() -> Image.Image:
    """横 4000x3000。キャンバス(16:9)より横長。"""
    return _marker(4000, 3000)


def _cfg(fit: str) -> Config:
    return Config(aspect="16:9", fit_mode=fit, letterbox=False)


def test_contain_blur_is_the_default_fit_mode():
    # 既定は全カット原寸を収める contain-blur。レターボックスの上下クロップも既定オフ。
    assert Config().fit_mode == "contain-blur"
    assert Config().letterbox is False


def test_contain_blur_keeps_full_height_and_fills_sides():
    src = _portrait_marker()
    frame = _photo_frame(src, 1920, 1080, _cfg("contain-blur"), None)

    assert frame.size == (1920, 1080)

    # 縦写真は高さ基準で収まる: 2000x3000 -> min(1920/2000,1080/3000) 倍 -> 720x1080
    fg_w = round(2000 * (1080 / 3000))
    left = (1920 - fg_w) // 2

    # 前景の最上部/最下部の帯が残っている = 上下がクロップされていない
    top = frame.getpixel((960, 3))
    bot = frame.getpixel((960, 1076))
    assert top[0] > 230 and top[1] > 230 and top[2] > 230, f"上端の白帯が消えた: {top}"
    assert bot[0] > 230 and bot[1] > 180 and bot[2] < 90, f"下端の黄帯が消えた: {bot}"

    # 横の余白は黒帯ではなく「ぼかした同じ写真」で埋まっている
    side = frame.getpixel((left // 2, 540))
    assert side != (0, 0, 0), "横の余白が黒くなっている(ぼかし背景で埋まっていない)"
    assert max(side) > 40, f"横の余白が暗すぎる: {side}"
    # ぼかし背景は元が赤主体なので赤が最も強い
    assert side[0] >= side[1] and side[0] >= side[2], f"背景が元写真由来でない: {side}"

    OUT.mkdir(parents=True, exist_ok=True)
    frame.save(OUT / "synthetic_contain_blur.png")


def test_cover_crops_top_and_bottom():
    """対比: cover は幅合わせで上下端(白帯・黄帯)が切り落とされる。"""
    src = _portrait_marker()
    frame = _photo_frame(src, 1920, 1080, _cfg("cover"), None)

    assert frame.size == (1920, 1080)
    top = frame.getpixel((960, 3))
    bot = frame.getpixel((960, 1076))
    # 中央の赤だけが残り、白帯・黄帯は見えない
    assert not (top[0] > 230 and top[1] > 230 and top[2] > 230), "cover なのに上端の白帯が残っている"
    assert not (bot[0] > 230 and bot[1] > 180 and bot[2] < 90), "cover なのに下端の黄帯が残っている"

    OUT.mkdir(parents=True, exist_ok=True)
    frame.save(OUT / "synthetic_cover.png")


def test_auto_uses_contain_blur_for_portrait_and_cover_for_landscape():
    """auto: キャンバスより縦長 → contain-blur(上下端が残る)、
    横長 → cover(上下端が切れる、キャンバスを覆いきる)。"""
    p = _photo_frame(_portrait_marker(), 1920, 1080, _cfg("auto"), None)
    p_top, p_bot = p.getpixel((960, 3)), p.getpixel((960, 1076))
    assert p_top[0] > 230 and p_top[1] > 230 and p_top[2] > 230, f"縦写真の上端が消えた: {p_top}"
    assert p_bot[0] > 230 and p_bot[1] > 180 and p_bot[2] < 90, f"縦写真の下端が消えた: {p_bot}"
    assert p.getpixel((30, 540)) != (0, 0, 0), "縦写真の横余白が黒帯"

    ls = _photo_frame(_landscape_marker(), 1920, 1080, _cfg("auto"), None)
    l_top, l_bot = ls.getpixel((960, 3)), ls.getpixel((960, 1076))
    assert not (l_top[0] > 230 and l_top[1] > 230 and l_top[2] > 230), "横写真が cover になっていない(上端の白帯が残る)"
    assert not (l_bot[0] > 230 and l_bot[1] > 180 and l_bot[2] < 90), "横写真が cover になっていない(下端の黄帯が残る)"
    # cover はキャンバス四隅まで写真で覆う(ぼかし背景の帯を出さない)
    assert ls.getpixel((5, 540))[0] > 120, f"横写真の左端が写真で覆われていない: {ls.getpixel((5, 540))}"

    OUT.mkdir(parents=True, exist_ok=True)
    p.save(OUT / "synthetic_auto_portrait.png")
    ls.save(OUT / "synthetic_auto_landscape.png")
