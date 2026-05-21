# セットアップ手順

## 1. 依存パッケージのインストール

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

## 2. config.yaml の編集

| 項目 | 説明 |
|------|------|
| `url` | 社内タイムカードページのURL |
| `late_threshold` | 遅刻判定の基準時刻（例: `"09:00"`） |
| `email.smtp_server` | 社内SMTPサーバーのホスト名またはIPアドレス |
| `email.smtp_port` | SMTPポート番号（587=STARTTLS / 465=SSL / 25=平文） |
| `email.sender` | 送信元メールアドレス |
| `email.password` | SMTPパスワード（認証不要なら空文字 `""`） |
| `email.recipients` | 送信先メールアドレスのリスト |
| `slack_webhook_url` | Slack Incoming Webhook URL（不要なら空文字 `""`） |

## 3. 動作確認

```bash
python check_attendance.py
```

ログは `logs/` ディレクトリ以下に日付付きで出力されます。

## 4. Windowsタスクスケジューラへの登録

1. 「タスク スケジューラ」を開く
2. 「基本タスクの作成」をクリック
3. 以下を設定する

| 項目 | 値 |
|------|----|
| 名前 | `タイムカード確認` |
| トリガー | 毎日 |
| 開始時刻 | `10:00:00`（config.yaml の `schedule_time` に合わせる） |
| 操作 | プログラムの開始 |
| プログラム | `C:\attendance\run_attendance.bat`（実際のパスに変更） |
| 開始（オプション） | `C:\attendance` |

> **注意**: タスクは「ユーザーがログオンしているかどうかにかかわらず実行する」に設定し、
> 「最上位の特権で実行する」にチェックを入れることを推奨します。

## 5. ページ構造が異なる場合の調整

`check_attendance.py` の `_find_day_column()` 関数内の `patterns` リストに
実際のHTMLヘッダーに合わせた日付フォーマットを追加してください。

```python
patterns = [
    target,                             # "21"
    f"{today.month}/{target}",          # "5/21"
    f"{target}日",                     # "21日"
    # ← ここに追加
]
```
