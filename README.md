# trip-video

Codexにプロンプトを渡すと、本人のGoogle ColabでMiniMax H3が動画を生成し、MP4をPCに保存します。参考画像、秒数、出力サイズも指定できます。

## 別のPCで使う

1. [配布用ZIP](https://github.com/xyz0713/trip-video/releases/latest/download/trip-video-skill.zip)を展開し、中の `trip-video` フォルダーをホームディレクトリの `.codex/skills/` に置きます。Windowsでは `%USERPROFILE%\.codex\skills\trip-video\SKILL.md`、macOSとLinuxでは `~/.codex/skills/trip-video/SKILL.md` が見えれば配置完了です。
2. Codexで新しいチャットを開き、「trip-videoの初回ログインと接続確認をして。使うGoogleアカウントは〇〇、プロジェクトIDは〇〇」と依頼します。Googleの認証画面で自分のアカウントを選びます。ここで使用するGoogle Cloudプロジェクトでは [Colab API](https://console.cloud.google.com/apis/library/colaboratory.googleapis.com) を有効にしておきます。アカウントにはそのプロジェクトの `serviceusage.services.use` 権限が必要です。
3. 「MiniMax H3で、海辺を走る犬の動画を作って」のように依頼します。完成するとCodexがMP4の保存先を返します。

「横長で5秒」「960×1280で」のように希望を添えられます。生成できる解像度と尺はColabのGPUメモリや実行時間に依存します。

初回はPython 3.12と依存パッケージが必要です。uvがあるPCではスキルの起動時に準備します。以降の認証はそのPCのOSの資格情報ストアを使います。Googleの認証が失効した場合は再ログインします。Colab CLIの対応OSはmacOSとLinuxです。Windowsネイティブでの動作は未検証です。

## 中断したとき

同じPCから「受付ID ○○ の動画を再取得して」と依頼します。その受付のランタイムが残っていれば、生成を引き継いでMP4を保存します。PCを閉じている間にもGPUがユニットを消費する場合があります。不要になった受付は「受付ID ○○ をキャンセルして」と依頼するとGPUを解放します。完成後のGPU解放は自動です。

コマンドの詳細は [SKILL.md](SKILL.md) にあります。
