# 株価監視アプリ セットアップ手順

必要な登録はGitHub（すでにお持ち）だけです。別の通知アプリは不要で、このアプリ自体がiPhoneに直接プッシュ通知を送ります。

## 1. GitHubリポジトリを作る

1. GitHubで新しいリポジトリを作成（例: `stock-alert-app`）。**Public（公開）** にしてください（無料のGitHub Pagesは公開リポジトリでのみ使えます）。
   - ウォッチリストや閾値は公開されますが、通知用の秘密鍵は下記の方法で非公開にするので悪用はされません。
2. このフォルダ一式をそのリポジトリにアップロードします（GitHubのWeb画面から「Add file → Upload files」でドラッグ&ドロップでOK。`.github/workflows/check.yml`のような隠しフォルダも忘れずに）。

## 2. 通知用の鍵(VAPID)をSecretsに登録する

あなた専用の鍵ペアをすでに生成済みです。**秘密鍵は他人に教えないでください。**

- 公開鍵（`docs/index.html`に埋め込み済み、そのままでOK）:
  `BD1xkmAgJlkI6E9SorHcMMDhYmdlsQ2aMTya5RO6NCLMSisOzmCIfMdqHQBJsN8oeQ5GwiGpCwcgk4ujZEwWmAc`
- 秘密鍵（これをGitHub Secretsに登録します）:
  `yEuXWSBsvKL4WwpTze4RhoL2ydN4YrVw6Q8WkNjnsjg`

GitHubのリポジトリ画面で **Settings → Secrets and variables → Actions → New repository secret** を開き、以下の2つを登録してください。

| Name | Value |
|---|---|
| `VAPID_PRIVATE_KEY` | `yEuXWSBsvKL4WwpTze4RhoL2ydN4YrVw6Q8WkNjnsjg` |
| `VAPID_SUBJECT` | `mailto:あなたのメールアドレス`（形式が正しければ実在しなくても動作します） |

## 3. GitHub Pagesを有効化する

1. リポジトリの **Settings → Pages** を開く
2. 「Build and deployment」の Source を **Deploy from a branch** にし、Branch を `main` / フォルダを `/docs` に設定して保存
3. 数分後、`https://あなたのユーザー名.github.io/リポジトリ名/` でアプリが表示されます

## 4. iPhoneのホーム画面に追加する

1. iPhoneのSafariで上記URLを開く（**必ずSafariで**。他のブラウザだとWeb Pushが使えません）
2. 共有ボタン → 「ホーム画面に追加」
3. ホーム画面のアイコンから開く（ここ以降は必ずこのアイコンから開いてください。SafariのブックマークからだとWeb Pushが動きません）

## 5. アプリ内で接続設定＋通知を有効化する

ホーム画面のアイコンから開き、右上の ⚙ から：

1. **GitHub接続設定**（この情報は端末内のブラウザにのみ保存されます）
   - GitHubユーザー名 / リポジトリ名 / ブランチ（通常 `main`） / Personal Access Token（下記参照）
2. 「**通知を有効にする**」ボタンをタップ → iPhoneの通知許可ポップアップが出るので「許可」をタップ
   - これで購読情報が`subscription.json`としてリポジトリに自動保存されます

### Personal Access Tokenの発行方法

1. GitHubの **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**
2. Repository access を「Only select repositories」にして、このリポジトリを選択
3. Permissions の **Contents** を **Read and write** に設定
4. 発行されたトークン（`github_pat_...`）をコピーして、アプリの接続設定に貼り付ける

## 6. 動作確認

リポジトリの **Actions** タブ → 「Stock Check」→ **Run workflow** で手動実行できます。しばらくして、iPhoneに通知が届けば成功です（条件を満たした銘柄がある場合のみ届きます）。

普段は平日14:40(JST)に自動実行されます。

## アプリの使い方

- ホーム画面のアイコンから開くと、監視銘柄の状況（現在値・RSI・5MA・シグナルの有無）が一覧表示されます
- ⚙から銘柄の追加・削除、RSI/MAの閾値変更ができ、保存するとGitHub上の`config.json`が更新され、次回のチェックから反映されます

## ロジックの補足

- RSIは14日ベース（Wilder方式）
- 5MAは直近5営業日の終値の単純移動平均
- 「14:40頃の現在値」を本日の終値の見込みとして、RSI・5MA・クロス判定を計算します
- 買いシグナル: (RSI ≤ 35 かつ 5MAを終値ベースで上抜けしそう) または (RSI ≤ 20)
- 売りシグナル: RSI ≥ 75 かつ 5MAを終値ベースで下抜けしそう（監視銘柄と同じ5銘柄すべてが対象）
- 通知は最大で1日1回、14:45頃に届きます（条件を満たさない日は通知なし）
- 「過去1年の買い/売り回数」は、条件を満たした状態が連続している間はまとめて1回と数え、さらに直近のカウントから5営業日以内の再発生はカウントしません（閾値付近を短期間で行き来した場合の水増しを防ぐため）。買いの回数は「RSI≤35 かつ 5MA上抜け」を基準とし、そのうち「RSI≤20到達」で成立した回数は括弧内に別途表示します（同じ数え方＝連続状態はまとめて1回・5営業日クールダウン、を適用）

## 既知の制限

- 使用しているyfinanceは非公式ライブラリのため、まれにYahoo側の仕様変更で取得エラーになることがあります（その場合はリストにエラー表示されます）
- 株価には数分〜20分程度の遅延がある可能性があります
- iOSのWeb Push機能はiOS 16.4以降が必要です。また、機種変更やアプリの再インストール（ホーム画面から削除して再追加）をした場合は、⚙から「通知を有効にする」をもう一度タップし直してください
- 次回決算日はyfinance経由で取得を試みますが、日本株は情報源(Yahoo Finance)側のカバー率が低く、取得できないことが多いです。取得できない場合は「未定」と表示されます
