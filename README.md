# Send Image to Eagle

ComfyUIのIMAGEバッチを、同じPCで起動しているEagleの現在のライブラリにPNGとして登録するV3出力ノードです。

## 使い方

1. このフォルダを `ComfyUI\custom_nodes\comfyui_to_eagle` に配置し、ComfyUIを再起動します。追加の依存インストールは不要です。
2. Eagleを起動し、保存先のライブラリを開きます。
3. `image/Eagle` の **Send Image to Eagle** を追加し、`images` にIMAGEを接続して実行します。

| 入力 | 内容 |
|---|---|
| `images` | バッチ全件を登録 |
| `name` | Eagle上の画像名。既定は `ComfyUI`。バッチでは `_00001` からの連番を付与 |
| `tags` | カンマ区切り。例: `生成画像, イラスト` |
| `annotation` | 注釈。複数行・日本語対応 |

出力ソケット・プレビュー・フォルダ指定・ワークフロー情報の埋め込みはありません。同じ入力でも実行するたびに登録するため、重複する場合があります。

## 一時画像とエラー

画像はComfyUIのtemp内の `eagle_` で始まる専用フォルダに作成し、outputには保存しません。Eagle APIの成功応答はコピー完了より早いため、登録先PNGのサイズとSHA-256が元画像と一致することを確認してから一時画像を削除します。

送信・コピー完了確認に失敗した場合はノードをエラーにし、一時画像の場所を表示します。自動再送は行いません。通信とコピー完了確認のタイムアウトはそれぞれ60秒です。失敗やタイムアウトでも一部または全件が登録済みの可能性があるため、再実行前にEagleを確認してください。コピー完了を確認するまでライブラリを切り替えないでください。

「登録成功、削除失敗」のエラーでは再実行せず、残った一時画像を手動で削除してください。

## 接続と対応環境

接続先は `http://127.0.0.1:41595` 固定です。プロキシやリダイレクトは使用せず、別PCのEagleには対応しません。Eagleのライブラリ内の原画像をComfyUIから読み取れる必要があります。

Eagle 4.0で動作確認済みです。バッチ登録APIが画像ごとの登録IDと現在のライブラリの絶対パスを返さない環境では、安全なコピー完了確認ができないためエラーにして一時画像を残します。

このリポジトリではcustom_nodesがGit除外されています。除外設定は変更していません。

## テスト

```powershell
python -m pytest custom_nodes\comfyui_to_eagle\tests -q
python -m ruff check --no-respect-gitignore custom_nodes\comfyui_to_eagle
```

pytestの一時領域にアクセスできない場合は、空の専用フォルダを `--basetemp` で指定してください。自動テストはEagleに実際の画像を登録しません。

API資料: https://api.eagle.cool/item/add-from-paths
