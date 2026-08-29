"""plan.json → 人間可読な日本語要約。

チャットにそのまま貼れる形。ここで出した内容にユーザーが自然言語で
フィードバックし、plan.json を直して再度この要約を見せ、承認を得てから render する。
"""

from __future__ import annotations

from pathlib import Path


def _fmt_span(t_start: str | None, images: list[str]) -> str:
    if t_start:
        return t_start.replace("T", " ")[:16]
    return "撮影時刻不明"


def _first_preview_dir(per_image: dict) -> str | None:
    for e in per_image.values():
        if e.get("preview"):
            return str(Path(e["preview"]).parent)
    return None


def render_summary(plan: dict, plan_path: Path | None = None) -> str:
    L: list[str] = []
    cfg = plan.get("config", {})
    order = plan.get("order", [])
    chapters = plan.get("chapters") or plan.get("groups") or []
    approved = plan.get("approved") is True

    L.append("=" * 60)
    L.append(f"タイトル : {plan.get('title', '(なし)')}")
    L.append(f"全体トーン: {plan.get('overall_tone') or plan.get('mood', '-')}")
    L.append(f"感情(mood): {plan.get('mood', '-')}")
    L.append(f"出力     : {cfg.get('aspect', '?')} / {cfg.get('fps', '?')}fps"
             f" / fit={cfg.get('fit_mode', '?')}"
             f" / 1枚{cfg.get('seconds_per_slide', '?')}秒"
             f" / 区切り{'あり' if cfg.get('chapter_dividers') else 'なし'}")
    L.append(f"採用枚数 : {len(order)} 枚 / {len(chapters)} 章"
             f"(グルーピング: {cfg.get('grouping_mode', '?')})")
    L.append("")

    # 章立て
    L.append("── 章立て ──")
    for i, ch in enumerate(chapters, 1):
        imgs = ch.get("images", [])
        div = ch.get("divider_text") or ch.get("label") or f"シーン{i}"
        loc = "GPSあり" if ch.get("centroid") else "GPSなし"
        L.append(f"{i:>2}. {ch.get('label') or div}")
        L.append(f"     区切り文言「{div}」 / {len(imgs)}枚 / "
                 f"{_fmt_span(ch.get('t_start'), imgs)} / {loc}")
    L.append("")

    # 代表キャプション
    caps = plan.get("captions", {})
    if caps:
        L.append("── 代表キャプション ──")
        shown = 0
        step = max(1, len(order) // 5)
        for j in range(0, len(order), step):
            p = order[j]
            if caps.get(p):
                L.append(f"  #{j + 1} {Path(p).name}: {caps[p]}")
                shown += 1
            if shown >= 5:
                break
        if not shown:
            L.append("  (キャプションはまだ空)")
        L.append("")

    # BGM
    L.append("── BGM ──")
    music = plan.get("music")
    if music:
        L.append(f"  {Path(music['path']).name} / mood={music.get('mood', '?')}"
                 f" / {music.get('duration', '?')}秒 / license={music.get('license', '?')}")
    else:
        L.append("  無音(music_index.json 未登録、または該当曲なし)")
    L.append(f"  曲調の希望: {plan.get('music_style', '-')}")
    L.append("")

    # 自動補正(露出 → 色)
    L.append("── 自動補正(露出 → 色) ──")
    color = plan.get("color")
    exposure = plan.get("exposure")
    prev_dir = None

    if not cfg.get("exposure_correct"):
        L.append("  露出補正: 無効(config: exposure_correct=false)")
    elif not exposure:
        L.append("  露出補正: 情報なし")
    else:
        per = exposure.get("per_image", {})
        n_on = sum(1 for e in per.values()
                   if not e.get("skipped") and not e.get("disabled"))
        n_skip = sum(1 for e in per.values() if e.get("skipped"))
        n_dis = sum(1 for e in per.values() if e.get("disabled"))
        n_att = sum(1 for e in per.values() if e.get("attenuated"))
        L.append(f"  露出補正: 強度 {exposure.get('strength')} / "
                 f"適用 {n_on} 枚 / スキップ {n_skip} 枚 / 個別無効 {n_dis} 枚 / 自動抑制 {n_att} 枚")
        prev_dir = _first_preview_dir(per) or prev_dir

    if not cfg.get("color_correct"):
        L.append("  色補正  : 無効(config: color_correct=false)")
    elif not color:
        L.append("  色補正  : 情報なし")
    else:
        per = color.get("per_image", {})
        n_skip = sum(1 for e in per.values() if e.get("skipped"))
        n_dis = sum(1 for e in per.values() if e.get("disabled"))
        n_att = sum(1 for e in per.values() if e.get("attenuated"))
        n_on = sum(1 for e in per.values()
                   if not e.get("skipped") and not e.get("disabled")
                   and any(abs(float(x) - 1.0) > 1e-3 for x in e.get("gains", [1, 1, 1])))
        L.append(f"  色補正  : 強度 {color.get('strength')} / 補正あり {n_on} 枚 / "
                 f"低彩度スキップ {n_skip} 枚 / 個別無効 {n_dis} 枚 / 自動抑制 {n_att} 枚")
        prev_dir = _first_preview_dir(per) or prev_dir
        if plan.get("color_note"):
            L.append(f"  所見: {plan['color_note']}")
    if prev_dir:
        L.append(f"  before/after 比較: {prev_dir}/")
    L.append("")

    # 構図スコア(黄金比/三分割の目安。提示のみ、選択には使わない)
    comp = plan.get("composition") or {}
    if comp:
        L.append("── 構図スコア(黄金比/三分割の目安) ──")
        vals = [comp[p] for p in order if p in comp]
        if vals:
            sv = sorted(vals)
            med = sv[len(sv) // 2]
            L.append(f"  分布: min {sv[0]:.2f} / 中央 {med:.2f} / max {sv[-1]:.2f}")
            ranked = sorted(((comp[p], j) for j, p in enumerate(order) if p in comp),
                            reverse=True)[:3]
            tops = ", ".join(f"#{j + 1}({v:.2f})" for v, j in ranked)
            L.append(f"  構図が整っている: {tops}")
        L.append("")

    # 承認状態
    L.append("── 承認 ──")
    if approved:
        L.append("  ✅ 承認済み。autoslide render で最終 MP4 を生成できます。")
    else:
        pp = str(plan_path) if plan_path else "<plan.json>"
        L.append("  ⏳ 未承認。修正の希望を自然言語で伝えてください")
        L.append("     (例: 「7章目を削って」「もっと明るいトーンで」「露出を強く」"
                 "「この写真は補正しない」)。")
        L.append(f"     問題なければ: autoslide approve {pp} → autoslide render {pp}")
    L.append("=" * 60)
    return "\n".join(L)
