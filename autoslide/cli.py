"""コマンドライン入口。

  autoslide scan       <PATH>
  autoslide candidates <PATH> --count N --out DIR     # 候補プール + コンタクトシート
  autoslide pick       <DIR> --set 1,4,9,...          # 候補番号から selection.json
  autoslide plan       <PATH> --out DIR [--selection DIR/selection.json] [--no-llm]
  autoslide render     <plan.json> [--out out.mp4]
  autoslide run        <PATH> --count N [...]         # アルゴリズム選択で一括

写真の良し悪しを Claude 自身に見て選ばせる場合は candidates → (写真を見る) → pick → plan → render。
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import Config

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config.toml"


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config", default=str(DEFAULT_CONFIG) if DEFAULT_CONFIG.exists() else None,
                   help="config.toml のパス")
    p.add_argument("--aspect", choices=["16:9", "1:1", "9:16"], help="出力アスペクト比")
    p.add_argument("--seconds", type=float, help="1 枚あたりの表示秒数 (既定 4)")
    p.add_argument("--grouping", choices=["folder", "exif", "auto"],
                   help="グルーピング方針 (既定 auto)")
    p.add_argument("--fit", choices=["cover", "contain-blur", "auto"],
                   help="写真の正規化方法 (既定 auto=向きが逆の写真だけ contain-blur)")
    p.add_argument("-v", "--verbose", action="store_true")


def _load_cfg(args) -> Config:
    cfg = Config.load(args.config)
    if getattr(args, "aspect", None):
        cfg.aspect = args.aspect
    if getattr(args, "seconds", None):
        cfg.seconds_per_slide = args.seconds
    if getattr(args, "grouping", None):
        cfg.grouping_mode = args.grouping
    if getattr(args, "fit", None):
        cfg.fit_mode = args.fit
    cfg.__post_init__()  # 上書き後の再バリデーション
    cfg.resolution
    return cfg


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autoslide", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("scan", help="画像を索引化・特徴量抽出(増分)")
    sp.add_argument("path")
    _add_common(sp)

    cp = sub.add_parser("candidates", help="候補プールとコンタクトシートを出力(最終選択は skill/人)")
    cp.add_argument("path")
    cp.add_argument("--count", type=int, required=True, help="最終的に選ぶ枚数")
    cp.add_argument("--out", default=None, help="出力ディレクトリ (既定 ./out/<日時>)")
    cp.add_argument("--pool-factor", type=float, default=2.5, help="候補プールを count の何倍にするか")
    _add_common(cp)

    kp = sub.add_parser("pick", help="candidates.json の番号リストから selection.json を作る")
    kp.add_argument("dir", help="candidates を実行した出力ディレクトリ")
    kp.add_argument("--set", dest="picks", required=True,
                    help="採用する候補番号をカンマ区切りで (例: 1,4,9,12)")
    kp.add_argument("--config", default=str(DEFAULT_CONFIG) if DEFAULT_CONFIG.exists() else None)
    kp.add_argument("-v", "--verbose", action="store_true")

    pp = sub.add_parser("plan", help="選択→提案。plan.json を出力")
    pp.add_argument("path")
    pp.add_argument("--count", type=int, default=8, help="選ぶ枚数(--selection 指定時は無視)")
    pp.add_argument("--out", default=None, help="出力ディレクトリ (既定 ./out/<日時>)")
    pp.add_argument("--selection", default=None,
                    help="pick が作った selection.json。指定するとアルゴリズム選択を飛ばす")
    pp.add_argument("--proposal", default=None,
                    help="タイトル/感情/キャプションの JSON（Claude Code が記入）。API を使わない")
    pp.add_argument("--no-llm", action="store_true",
                    help="API を呼ばずフォールバック案にする（--proposal 指定時は不要）")
    pp.add_argument("--refresh-proposal", action="store_true",
                    help="Vision 結果キャッシュを無視して問い合わせ直す")
    pp.add_argument("--music", default=None, help="BGM を明示指定する音声ファイル")
    _add_common(pp)

    smp = sub.add_parser("summary", help="plan.json の人間可読な要約を表示")
    smp.add_argument("plan")
    smp.add_argument("-v", "--verbose", action="store_true")

    apr = sub.add_parser("approve", help="plan.json を承認する（render の前提）")
    apr.add_argument("plan")
    apr.add_argument("--undo", action="store_true", help="承認を取り消す")
    apr.add_argument("-v", "--verbose", action="store_true")

    dp = sub.add_parser("doctor", help="実行環境をチェック（ffmpeg フィルタ / フォント / HEIC）")
    dp.add_argument("-v", "--verbose", action="store_true")

    rp = sub.add_parser("render", help="承認済み plan.json から mp4 + srt を生成")
    rp.add_argument("plan")
    rp.add_argument("--out", default=None, help="出力 mp4 パス (既定 <plan と同じ場所>/out.mp4)")
    rp.add_argument("--keep-workdir", action="store_true")
    rp.add_argument("--force", action="store_true",
                    help="未承認でも生成する。対話レビューを飛ばす（非推奨）")
    rp.add_argument("-v", "--verbose", action="store_true")

    up = sub.add_parser("run", help="scan→plan を一括。既定は要約提示で停止（render しない）")
    up.add_argument("path")
    up.add_argument("--count", type=int, required=True)
    up.add_argument("--out", default=None)
    up.add_argument("--no-llm", action="store_true")
    up.add_argument("--music", default=None)
    up.add_argument("--keep-workdir", action="store_true")
    up.add_argument("--yes", action="store_true",
                    help="要約を待たず承認して render まで通す（CI / 明示的な一括実行向け）")
    _add_common(up)
    return parser


def _default_out_dir(path: str) -> Path:
    from datetime import datetime

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Path("out") / f"{Path(path).name}-{stamp}"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    # 遅延 import (依存が無くても --help は動く)
    from . import pipeline

    try:
        if args.cmd == "scan":
            _load_cfg(args)
            pipeline.do_scan(args.path)

        elif args.cmd == "candidates":
            cfg = _load_cfg(args)
            out_dir = Path(args.out) if args.out else _default_out_dir(args.path)
            path = pipeline.do_candidates(args.path, args.count, cfg, out_dir,
                                          pool_factor=args.pool_factor)
            print(path)

        elif args.cmd == "pick":
            cfg = Config.load(args.config)
            picks = [int(x) for x in args.picks.replace(" ", "").split(",") if x]
            if not picks:
                raise ValueError("--set に番号がありません")
            path = pipeline.do_pick(args.dir, picks, cfg)
            print(path)

        elif args.cmd == "plan":
            cfg = _load_cfg(args)
            out_dir = Path(args.out) if args.out else _default_out_dir(args.path)
            plan = pipeline.do_plan(args.path, args.count, cfg, out_dir,
                                    use_llm=not args.no_llm, music_override=args.music,
                                    selection_path=args.selection, proposal_path=args.proposal,
                                    refresh_proposal=args.refresh_proposal)
            print(plan)
            print(pipeline.do_summary(plan))

        elif args.cmd == "summary":
            print(pipeline.do_summary(args.plan))

        elif args.cmd == "approve":
            print(pipeline.do_approve(args.plan, approved=not args.undo))

        elif args.cmd == "doctor":
            return pipeline.do_doctor()

        elif args.cmd == "render":
            out = pipeline.do_render(args.plan, args.out, keep_workdir=args.keep_workdir,
                                     force=args.force)
            print(out)

        elif args.cmd == "run":
            cfg = _load_cfg(args)
            out_dir = Path(args.out) if args.out else _default_out_dir(args.path)
            out = pipeline.do_run(args.path, args.count, cfg, out_dir,
                                  use_llm=not args.no_llm, music_override=args.music,
                                  keep_workdir=args.keep_workdir, approve=args.yes)
            print(out)
    except (RuntimeError, FileNotFoundError, ValueError) as e:
        logging.error("%s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
