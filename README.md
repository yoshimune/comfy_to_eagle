# Send Image to Eagle

ComfyUIのIMAGEバッチ、または完成済みPNGバイト列を、同じPCで起動しているEagleの現在のライブラリに登録するV3出力ノードです。

## 使い方

1. このフォルダを `ComfyUI\custom_nodes\comfyui_to_eagle` に配置し、ComfyUIを再起動します。追加の依存インストールは不要です。
2. Eagleを起動し、保存先のライブラリを開きます。
3. `image/Eagle` の **Send Image to Eagle** を追加し、`images` にIMAGEを接続して実行します。

| 入力 | 内容 |
|---|---|
| `images` | 任意。IMAGEバッチ全件をPNGに変換して登録 |
| `name` | Eagle上の画像名。既定は `ComfyUI`。IMAGEバッチでは `_00001` からの連番を付与 |
| `tags` | カンマ区切り。例: `生成画像, イラスト` |
| `annotation` | 注釈。複数行・日本語対応 |
| `png_data` | 任意。`PNG_DATA` 型のPNGバイト列を再エンコードせず登録。`images` より優先 |

`images` または `png_data` のどちらかを接続してください。出力ソケット・プレビュー・フォルダ指定はありません。ワークフロー情報を新たに埋め込みませんが、PNG入力に既に含まれるメタデータはそのまま保持します。同じ入力でも実行するたびに登録するため、重複する場合があります。

## Save Image with Provenanceとの接続

`custom_nodes\comfyui_png_provenance` を配置・更新し、両ノードの変更後にComfyUIを再起動してブラウザーを再読み込みします。

| Save Image with Provenanceの出力 | このノードの入力 |
|---|---|
| `PNG data` | `png_data` |
| `filename` | `name` |
| `Description` | `annotation` |

`name` と `annotation` のウィジェットを入力ソケットに変換して接続してください。バッチ全件が対応するファイル名・Descriptionで画像ごとに登録されます。PNG入力では `name` に追加の連番を付けません。内包された入力画像や生成グラフも保持し、保存ノードがoutputに作成したPNGは削除しません。

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
