---
name: trip-video
description: 本人のGoogle Colabユニットで、3場面の指示と任意の参考画像から10秒の音声付き縦型MP4を生成してPCに保存する。初回ログインと中断した動画の再取得にも使う。
---

# 本人のColabで動画を作る

生成と受け取りは、同じPC・同じOSユーザーで行う。Colab APIに直接接続する。以下の `SCRIPT` はこのスキルの `scripts/trip_video.py` の絶対パスに置き換える。各PCにスキルを置き、初回にPython環境と本人のGoogle認証を用意する。

## 初回

1. `uv` が使える環境では `uv run --python 3.12 --script "SCRIPT" --help` で必要なPython環境を準備できる。以降も同じ呼び出し方を使う。
2. `uv run --python 3.12 --script "SCRIPT" check` を実行する。初回または認証の失効が確認できた場合は、利用する本人のGoogleメールアドレスと、Colab APIを有効にしたGoogle CloudプロジェクトIDを確認して、同じコマンドの末尾を `login --account メールアドレス --project プロジェクトID` に変えて起動する。Googleの認証画面で指定した本人のアカウントを選んでもらう。ログイン成功後、**新しいプロセスで** `check` を実行する。これはGPUを確保しない接続確認。通信障害の場合は接続を確認してから再試行する。
3. 認証はこのPCのOSの資格情報ストアに保存する。macOSはキーチェーン、Linux・WSLはユーザーセッションのロック解除済みSecret Serviceが必要。Windowsネイティブ版は実機未検証。Google CLIの対応OSはLinux・macOS。認証コード、トークン、パスワードをチャットに貼らせない。

uvがない環境では、Python 3.12の専用venvに `python -m pip install google-colab-cli==0.8.0 Pillow==12.0.0` を実行する。以降はその環境のPythonで `python "SCRIPT" コマンド` と呼ぶ。Google Cloud CLI、認証JSON、ローカルGPUは使わない。

## 動画の依頼

依頼内容を3つの場面（約3秒、3秒、4秒）にまとめる。人物の特徴、動き、せりふ、効果音を各場面に含める。参考画像は任意。`--image-1`、`--image-2`、`--image-3` で場面別に画像ファイルを指定できる。プロンプトはシェルで展開されないように個別の引数として渡す。

```text
uv run --python 3.12 --script "SCRIPT" generate --prompt "場面1" --prompt "場面2" --prompt "場面3"
```

1回の依頼につき1回起動し、最初に表示された受付IDを控える。初回設定で選んだ本人のユニットでColabのL4を使う。L4の空きがない場合は、その旨を伝える。実行が続いている間は元のプロセスを待つ。成功時に表示されたMP4の絶対パスを返す。既定の保存先はホームフォルダー内の `.config/trip-video/personal-jobs/<受付ID>/result.mp4`。ユーザーが保存先を指定した場合は、取得済みMP4をそこへコピーする。

## 中断・再取得

- 同じPCで `resume 受付ID` を実行すると、同じランタイムを確認し、MP4を取得してGPUを解放する。保存済みで解放も済んでいればローカルのファイルを返す。IDが不明なら `list`。これらは上記の `generate` と同じ形式で呼び出す。
- アプリの終了やネットワーク切断の直後はGPUが動き続けることがある。復帰したら同じ受付を再開し、ユーザーが生成を取りやめた場合は `cancel 受付ID` でそのGPUを解放する。解放が失敗した場合も同じIDで再試行する。
- Colabが先にランタイムを終了すると、その中の未取得動画は失われる。通信中断や待機期限の後は既存の受付を確認し、別の有料生成を自動で追加しない。
