# 同梱フォント

ここに CJK フォント（`.otf` / `.ttf` / `.ttc`）を 1 つ置くと、
タイトル / 区切り / キャプションの描画に**最優先**で使われます。
置かない場合はシステムフォント（macOS のヒラギノ等、Linux の Noto Sans CJK）に
フォールバックし、どちらも無ければ `autoslide doctor` と `render` が
明示エラーで止まります（文字が出ないまま生成することはありません）。

推奨: **Noto Sans CJK JP**（SIL Open Font License 1.1）
<https://github.com/notofonts/noto-cjk/releases> から
`NotoSansCJKjp-Regular.otf` を取得してこのフォルダに置く。

同梱したフォントのライセンス条文（OFL 等）も同じフォルダに含めること。
