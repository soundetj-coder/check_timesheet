#!/usr/bin/env python3
"""
タイムカード自動確認・通知スクリプト
Windowsタスクスケジューラで毎朝10:00に実行する
"""

import json
import re
import smtplib
import sys
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

import yaml
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

CONFIG_PATH = Path(__file__).parent / "config.yaml"
LOG_DIR = Path(__file__).parent / "logs"

# 氏名列と認識するヘッダー文字列
_NAME_HEADERS = {"氏名", "名前", "社員名", "氏　名", "氏　　名", "社員氏名", "name"}


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def log(msg: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# スクレイピング
# ---------------------------------------------------------------------------

def scrape_attendance(config: dict) -> list[dict]:
    """Playwrightで勤怠ページを取得し、社員ごとの打刻データを返す。"""
    url = config["url"]
    today = datetime.now()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            # イントラ内ページのためHTTPS証明書エラーを無視
            ignore_https_errors=True,
        )
        page = context.new_page()

        # 1. ページを開く
        try:
            page.goto(url, wait_until="networkidle", timeout=30_000)
        except PlaywrightTimeoutError:
            # タイムアウト後もDOM取得を試みる
            log("ページ読み込みタイムアウト。取得済みコンテンツで続行します。")

        # 2. 「一覧表示」ボタンをクリックしてデータを表示する
        try:
            page.click('input[value="一覧表示"]', timeout=10_000)
        except Exception:
            # input[value] で見つからない場合はテキストで探す
            try:
                page.get_by_text("一覧表示").click(timeout=10_000)
            except Exception as exc:
                log(f"一覧表示ボタンが見つかりませんでした: {exc}")
                browser.close()
                return []

        # 3. テーブルが表示されるまで待機する
        try:
            page.wait_for_selector("table", timeout=15_000)
        except PlaywrightTimeoutError:
            log("テーブルの表示を待機中にタイムアウトしました。")
            browser.close()
            return []

        # 4. テーブルからデータを取得する
        tables = page.query_selector_all("table")
        if not tables:
            log("テーブルが見つかりませんでした。")
            browser.close()
            return []

        # 最も行数が多いテーブルをメインの勤怠テーブルとみなす
        main_table = max(tables, key=lambda t: len(t.query_selector_all("tr")))
        rows = main_table.query_selector_all("tr")

        day_col_index = _find_day_column(rows, today)
        employees = _parse_rows(rows, day_col_index, config)

        browser.close()

    return employees


def _find_day_column(rows: list, today: datetime) -> int:
    """ヘッダー行から当日の列インデックスを返す。見つからない場合は -1。"""
    if not rows:
        return -1

    target = str(today.day)
    patterns = [
        target,
        f"{today.month}/{target}",
        f"{today.month:02d}/{int(target):02d}",
        f"{target}日",
    ]

    headers = rows[0].query_selector_all("th, td")
    for i, header in enumerate(headers):
        text = header.inner_text().strip()
        if text in patterns:
            return i
        if re.search(rf"(?<![\d]){re.escape(target)}(?![\d])", text):
            return i

    return -1


def _find_name_column(rows: list) -> int:
    """ヘッダー行から氏名列のインデックスを返す。見つからない場合は 0（先頫列）。"""
    if not rows:
        return 0

    headers = rows[0].query_selector_all("th, td")
    for i, header in enumerate(headers):
        text = header.inner_text().strip()
        # 完全一致または閃内包含で判定
        if text in _NAME_HEADERS or any(h in text for h in _NAME_HEADERS):
            return i

    log("氏名列が特定できず先頫列を使用します。")
    return 0


def _parse_rows(rows: list, day_col_index: int, config: dict) -> list[dict]:
    """データ行を走査して社員リストを生成する。"""
    employees = []
    name_col_index = _find_name_column(rows)

    for row in rows[1:]:  # ヘッダー行をスキップ
        cells = row.query_selector_all("td, th")
        if not cells:
            continue

        # 氏名列が列数を超える場合は先頫列にフォールバック
        name_idx = name_col_index if name_col_index < len(cells) else 0
        name = cells[name_idx].inner_text().strip()
        if not name:
            continue

        if 0 < day_col_index < len(cells):
            cell = cells[day_col_index]
            status, clock_in, clock_out = _analyze_cell(
                cell.inner_text().strip(),
                cell.inner_html(),
                config,
            )
        else:
            status, clock_in, clock_out = "未打刻", "", ""

        employees.append(
            {"name": name, "status": status, "clock_in": clock_in, "clock_out": clock_out}
        )

    return employees


# ---------------------------------------------------------------------------
# 打刻状況の判定
# ---------------------------------------------------------------------------

def _analyze_cell(cell_text: str, cell_html: str, config: dict) -> tuple[str, str, str]:
    """セルテキストとHTMLから (status, clock_in, clock_out) を返す。"""
    if not cell_text:
        return "未打刻", "", ""

    times = re.findall(r"\d{1,2}:\d{2}", cell_text)
    if not times:
        return "未打刻", "", ""

    clock_in = times[0]
    clock_out = times[1] if len(times) >= 2 else ""

    is_overtime = bool(clock_out) and "+" in cell_text
    is_late = _is_late(clock_in, cell_html, config)

    if not clock_out:
        status = "遅刻・退勤未打刻" if is_late else "出勤のみ（退勤未打刻）"
    elif is_late and is_overtime:
        status = "遅刻・残業"
    elif is_late:
        status = "遅刻"
    elif is_overtime:
        status = "残業"
    else:
        status = "正常"

    return status, clock_in, clock_out


def _is_late(clock_in: str, cell_html: str, config: dict) -> bool:
    """出勤時刻が遅刻かどうか判定する。

    赤字、または late_threshold（例: "09:30"）を超えた時刻を遅刻とみなす。
    例: late_threshold="09:30" → 9:31以降が遅刻、9:30までは正常。
    """
    html_lower = cell_html.lower()
    if (
        "color:red" in html_lower
        or "color: red" in html_lower
        or 'class="red"' in html_lower
        or "color=#ff" in html_lower
        or 'style="color:red' in html_lower
    ):
        return True

    late_threshold = config.get("late_threshold", "09:30")
    try:
        in_time = datetime.strptime(clock_in, "%H:%M").time()
        threshold = datetime.strptime(late_threshold, "%H:%M").time()
        # threshold の次の分以降を遅刻とする（例: 09:30 設定 → 09:31以降）
        return in_time > threshold
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# 通知メッセージ生成
# ---------------------------------------------------------------------------

def _apply_morning_mode(employees: list[dict]) -> list[dict]:
    """朝チェックモード時、退勤未打刻を含むステータスを再分類する。

    - 出勤のみ（退勤未打刻） → 正常
    - 遅刻・退勤未打刻     → 遅刻
    """
    mapping = {
        "出勤のみ（退勤未打刻）": "正常",
        "遅刻・退勤未打刻": "遅刻",
    }
    return [{**e, "status": mapping.get(e["status"], e["status"])} for e in employees]


def format_message(employees: list[dict], config: dict) -> str:
    today = datetime.now()
    date_str = today.strftime("%Y年%m月%d日")

    morning_mode = config.get("morning_check_mode", True)
    if morning_mode:
        employees = _apply_morning_mode(employees)

    STATUS_LABELS = {
        "未打刻": "未打刻",
        "出勤のみ（退勤未打刻）": "退勤未打刻",
        "遅刻・退勤未打刻": "遅刻+退勤未打刻",
        "遅刻": "遅刻",
        "遅刻・残業": "遅刻+残業",
        "残業": "残業",
        "正常": "正常",
    }

    issues = [e for e in employees if e["status"] != "正常"]
    normal = [e for e in employees if e["status"] == "正常"]

    mode_label = "（朝チェックモード）" if morning_mode else ""
    lines = [
        f"【タイムカード確認レポート】{date_str}{mode_label}",
        f"確認時刻: {today.strftime('%H:%M')}  基準時刻: {config.get('late_threshold', '09:30')}",
        "=" * 44,
    ]

    if issues:
        lines.append(f"\n■ 要確認 ({len(issues)}名)")
        for e in issues:
            label = STATUS_LABELS.get(e["status"], e["status"])
            times = f"  出勤:{e['clock_in']}" if e["clock_in"] else ""
            if e["clock_out"]:
                times += f"  退勤:{e['clock_out']}"
            lines.append(f"  [{label}] {e['name']}{times}")
    else:
        lines.append("\n■ 要確認: なし")

    if normal:
        lines.append(f"\n■ 正常打刻 ({len(normal)}名)")
        for e in normal:
            times = f"  出勤:{e['clock_in']}" if e["clock_in"] else ""
            if e["clock_out"]:
                times += f"  退勤:{e['clock_out']}"
            lines.append(f"  {e['name']}{times}")

    lines.append(f"\n合計: {len(employees)}名")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# メール送信
# ---------------------------------------------------------------------------

def send_email(message: str, config: dict) -> None:
    email_cfg = config["email"]
    today = datetime.now()
    subject = f"タイムカード確認レポート {today.strftime('%Y/%m/%d')}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = email_cfg["sender"]
    msg["To"] = ", ".join(email_cfg["recipients"])
    msg.attach(MIMEText(message, "plain", "utf-8"))

    smtp_server = email_cfg["smtp_server"]
    smtp_port = email_cfg["smtp_port"]
    sender = email_cfg["sender"]
    password = email_cfg.get("password", "")
    use_tls = email_cfg.get("use_tls", True)
    use_ssl = email_cfg.get("use_ssl", False)

    try:
        if use_ssl:
            with smtplib.SMTP_SSL(smtp_server, smtp_port) as server:
                if password:
                    server.login(sender, password)
                server.sendmail(sender, email_cfg["recipients"], msg.as_string())
        else:
            with smtplib.SMTP(smtp_server, smtp_port) as server:
                if use_tls:
                    server.starttls()
                if password:
                    server.login(sender, password)
                server.sendmail(sender, email_cfg["recipients"], msg.as_string())
        log("メール送信完了")
    except Exception as exc:
        log(f"メール送信エラー: {exc}")
        raise


# ---------------------------------------------------------------------------
# Slack 通知
# ---------------------------------------------------------------------------

def send_slack(message: str, config: dict) -> None:
    webhook_url = config.get("slack_webhook_url", "").strip()
    if not webhook_url:
        log("slack_webhook_url が未設定のためスキップします。")
        return

    payload = json.dumps({"text": f"```\n{message}\n```"}).encode("utf-8")
    req = Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                log("Slack 通知完了")
            else:
                log(f"Slack 通知失敗: HTTP {resp.status}")
    except URLError as exc:
        log(f"Slack 送信エラー: {exc}")
        raise


# ---------------------------------------------------------------------------
# エントリポイント
# ---------------------------------------------------------------------------

def main() -> None:
    LOG_DIR.mkdir(exist_ok=True)
    log("タイムカード確認開始")

    config = load_config()

    log("スクレイピング中...")
    employees = scrape_attendance(config)

    if not employees:
        log("社員データを取得できませんでした。スクリプトを終了します。")
        sys.exit(1)

    log(f"{len(employees)} 名のデータを取得しました。")

    message = format_message(employees, config)
    log("\n" + message + "\n")

    send_email(message, config)
    send_slack(message, config)

    log("タイムカード確認完了")


if __name__ == "__main__":
    main()
