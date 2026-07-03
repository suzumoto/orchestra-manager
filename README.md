# 出欠確認システム
- オーケストラ・吹奏楽などの楽団で出欠を簡単に確認することができます.
- 他サービスを使うことなく、Discord内で完結したシステムです。便利!
- 団員は 出席/欠席/早退/遅刻 のリアクションをするだけ。出力も簡単です.

# 導入と使い方
- 導入方法と使い方は [wiki](https://github.com/suzumoto/orchestra-manager/wiki/%E5%B0%8E%E5%85%A5%E6%96%B9%E6%B3%95%E3%81%A8%E4%BD%BF%E3%81%84%E6%96%B9) を参照してください。
- [Discordサーバー](https://discord.gg/mewaFyttak) もあります(整備中)。

# セットアップ手順（自分で動かす場合）

このBotを自分のDiscordサーバーで動かすには、いくつか準備が必要です。
プログラミングの知識が無くても、順番通りに進めれば設定できるようにまとめています。
スマホだけでは完結せず、**パソコンでの作業が必要**なので注意してください。

## 用意するもの
- パソコン（Windows / Mac どちらでもOK）
- 自分が管理者権限を持っているDiscordサーバー
- Googleアカウント

## 手順の全体像
1. Discord Botを作って「トークン」をもらう
2. Googleの「サービスアカウント」を作って `credentials.json` をダウンロードする
3. 出欠を記録するGoogleスプレッドシートと、座席図を作るGoogleスライドを用意する
4. パソコンにこのプログラムとPythonを用意する
5. 「環境変数」という設定でトークンやIDを登録する
6. `settings.ini` を自分のDiscordサーバーに合わせて書き換える
7. プログラムを起動する

---

## 1. Discord Botを作ってトークンを取得する
1. [Discord Developer Portal](https://discord.com/developers/applications) にアクセスし、Discordアカウントでログインする
2. 右上の「New Application」を押して、好きな名前（例：出欠管理Bot）を付けて作成する
3. 左側メニューの「Bot」を開き、「Reset Token」（初回は「Add Bot」）を押してトークンを表示する
   - **このトークンは絶対に他人に見せたり、SNSやGitHubに書き込んだりしないこと**（見られると誰でもBotを乗っ取れてしまいます）
   - 表示された文字列をどこかに一時的にメモしておく（後で使います）
4. 同じ「Bot」の画面で「Privileged Gateway Intents」の項目を全部ONにする
   （SERVER MEMBERS INTENT / MESSAGE CONTENT INTENT など）
5. 左側メニューの「OAuth2」→「URL Generator」を開き、
   「SCOPES」で `bot` に、「BOT PERMISSIONS」で `Administrator`（もしくは必要な権限一式）にチェックを入れる
6. 画面下に生成されたURLをコピーしてブラウザで開き、自分のDiscordサーバーにBotを招待する

## 2. Googleサービスアカウントを準備する
このBotはGoogleスプレッドシートとGoogleスライドを直接読み書きするので、
「サービスアカウント」というBot専用のGoogleアカウントのようなものを用意します。

1. [Google Cloud Console](https://console.cloud.google.com/) にアクセスし、Googleアカウントでログインする
2. 画面上部でプロジェクトを新規作成する（名前は何でも良い）
3. 左上のメニューから「APIとサービス」→「有効なAPIとサービス」を開き、「+ APIとサービスの有効化」から次の3つを検索して、それぞれ有効化する
   - Google Sheets API
   - Google Slides API
   - Google Drive API
4. 「APIとサービス」→「認証情報」を開き、「+ 認証情報を作成」→「サービスアカウント」を選ぶ
5. 名前を適当に付けて作成する（権限の割り当て画面は何も設定せずスキップしてOK）
6. 作成したサービスアカウントの一覧から今作ったものをクリックし、「鍵」タブを開く
7. 「鍵を追加」→「新しい鍵を作成」→形式は「JSON」を選んで作成する
   - 自動でファイルがダウンロードされます。これが `credentials.json` の中身になります
8. ダウンロードしたファイルの名前を `credentials.json` に変更し、このプログラムのフォルダ（`orch_bot.py` があるのと同じ場所）に置く
   - このファイルも他人に見せたり公開したりしないこと
9. サービスアカウントの詳細画面に表示されている「メール」（`〇〇@〇〇.iam.gserviceaccount.com` のような形式）をメモしておく（次の手順3で使います）

## 3. Googleスプレッドシート・スライドを用意する

**スプレッドシート（出欠データの保存先）**
1. Googleスプレッドシートで新しいシートを作る
2. シート名を `全奏`・`分奏` という名前にする（`settings.ini` の初期設定に合わせる場合。変える場合は後述の手順6で設定を変更）
3. 右上の「共有」ボタンから、手順2でメモしたサービスアカウントのメールアドレスを追加し、権限は「編集者」にする
4. ブラウザのアドレスバーのURLを見て、`https://docs.google.com/spreadsheets/d/【ここの部分】/edit` の【ここの部分】の文字列をメモしておく（これが `SPREADSHEET_ID` になります）

**スライド（座席図のレイアウト）**
1. Googleスライドで座席図のレイアウトを作る（座席の四角形1つ1つに「Vn1st-1」のような名前を付けるなど、座席レイアウトのルールについては別途 wiki を参照）
2. 右上の「共有」ボタントから、同じサービスアカウントのメールアドレスを追加する（権限は「閲覧者」でOK）
3. アドレスバーのURLの `https://docs.google.com/presentation/d/【ここの部分】/edit` の【ここの部分】をメモしておく（これが `SLIDES_PRESENTATION_ID` になります）

## 4. パソコンにプログラムとPythonを用意する
1. [Python公式サイト](https://www.python.org/downloads/) から最新版をダウンロードしてインストールする
   - インストール画面で「Add Python to PATH」というチェックボックスが出てきたら、必ずチェックを入れる
2. このプログラム一式（`orch_bot.py` などが入っているフォルダ）をパソコンの好きな場所に置く
3. 「コマンドプロンプト」（Windows）または「ターミナル」（Mac）を開き、`cd` コマンドでこのフォルダに移動する
   - 例：`cd C:\Users\自分の名前\Documents\discord-rsvp`
4. 次のコマンドを実行し、必要なプログラム部品（ライブラリ）をまとめてインストールする
   ```
   pip install -r requirements.txt
   ```

## 5. 環境変数を設定する
「環境変数」とは、プログラムに秘密の値を教えるための仕組みです。
`settings.ini` などのファイルに直接書かずに、こちらに設定することで、
うっかりトークンなどをファイルごと人に渡してしまう事故を防げます。

次の3つを設定する必要があります。
- `DISCORD_BOT_TOKEN` … 手順1でメモしたトークン
- `SPREADSHEET_ID` … 手順3でメモしたスプレッドシートのID
- `SLIDES_PRESENTATION_ID` … 手順3でメモしたスライドのID

**Windows（コマンドプロンプト）で毎回起動前に設定する場合**
```
set DISCORD_BOT_TOKEN=ここにトークンを貼り付け
set SPREADSHEET_ID=ここにIDを貼り付け
set SLIDES_PRESENTATION_ID=ここにIDを貼り付け
```

**Windows（PowerShell）で毎回起動前に設定する場合**
```
$env:DISCORD_BOT_TOKEN = "ここにトークンを貼り付け"
$env:SPREADSHEET_ID = "ここにIDを貼り付け"
$env:SLIDES_PRESENTATION_ID = "ここにIDを貼り付け"
```

**Mac/Linux（ターミナル）で毎回起動前に設定する場合**
```
export DISCORD_BOT_TOKEN=ここにトークンを貼り付け
export SPREADSHEET_ID=ここにIDを貼り付け
export SLIDES_PRESENTATION_ID=ここにIDを貼り付け
```

毎回入力するのが面倒な場合は、Windowsの「システム環境変数の編集」から恒久的に登録することもできます。

## 6. `settings.ini` を書き換える
`settings.ini` というファイルをメモ帳などのテキストエディタで開き、
自分のDiscordサーバーの実際のチャンネル名・ロール名・絵文字名に書き換えます。
（この中には秘密の情報は入っていないので、公開しても問題ありません）

主に確認・変更する項目：
- `[COMMAND_CHANNEL]` … Botにコマンドを送るチャンネル名
- `[RSVP_CHANNEL]` … 出欠を取るカレンダー投稿用チャンネル名
- `[OUTPUT_CHANNEL]` … 出欠表の画像を出力するチャンネル名
- `[ROLE]` … 出欠表を出力できる運営ロールの名前
- `[EMOJI]` … 出席/欠席/遅刻/早退などに使うカスタム絵文字の名前
  （絵文字は事前にDiscordサーバーに登録しておく必要があります）
- `[PART_ROLE]` … 各パート（Vn, Fl, Ob…）のロール名との対応

## 7. プログラムを起動する
環境変数を設定したのと同じコマンドプロンプト／ターミナルの画面で、次を実行します。
```
python orch_bot.py
```
「Logged in as ...」のような文字が表示されればBotの起動に成功しています。
起動したままウィンドウを閉じるとBotも止まってしまうので、動かし続けたい間は
このウィンドウ（またはパソコン自体）を開いたままにしておいてください。

## うまくいかないときは
- `環境変数 DISCORD_BOT_TOKEN が未設定です` のようなエラーが出る
  → 手順5の環境変数設定が、プログラムを起動したのと**同じ画面**でされているか確認してください（別のウィンドウで設定すると反映されません）
- Botはオンラインになるのに反応しない
  → 手順1の「Privileged Gateway Intents」がONになっているか、手順6の`settings.ini`のチャンネル名・絵文字名が実際のサーバーと一致しているか確認してください
- スプレッドシートやスライドに関するエラーが出る
  → 手順3で、サービスアカウントのメールアドレスをスプレッドシート・スライドの共有設定にちゃんと追加したか確認してください

# ライセンス
- MIT Licenseです。LICENSE.txtも参照

# 使用フォント
本ソフトでは表示フォントに「源真ゴシック」(http://jikasei.me/font/genshin/) を使用しています。
Licensed under SIL Open Font License 1.1 (http://scripts.sil.org/OFL)
© 2015 自家製フォント工房, © 2014, 2015 Adobe Systems Incorporated, © 2015 M+
FONTS PROJECT
