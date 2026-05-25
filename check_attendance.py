#!/usr/bin/env python3
"""
タイムカード自動確認・通知スクリプト
Windowsタスクスケジューラで毎朝10:00に実行する
"""

import json
import re
import smtplib
import socket
import sys
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

import jpholiday
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
# 平日判定・前営業日計算
# ---------------------------------------------------------------------------

def is_business_day(d: date) -> bool:
    """平日（土日・日曜・祝日以外）の場合 True を返す。"""
    if d.weekday() >= 5:  # 5=土曜、6=日曜
        return False
    if jpholiday.is_holiday(d):
        return False
    return True


def skip_reason(d: date) -> str:
    """スキップ理由の文字列を返す（ログ出力用）。"""
    if d.weekday() == 5:
        return "土曜日"
    if d.weekday() == 6:
        return "日曜日"
    holiday_name = jpholiday.is_holiday(d)
    return f"祝日（{holiday_name}）"


def get_previous_business_day(today: date) -> date:
    """今日の1日前から順に退い、最初に見つかった平日を返す。

    土日・日曜・祝日（jpholiday）をスキップする。
    月をまたいで遅るケース（例: 5月1日 → 4月30日）にも対応。
    """
    d = today - timedelta(days=1)
    while not is_business_day(d):
        d -= timedelta(days=1)
    return d


# ---------------------------------------------------------------------------
# Playwright ヘルパー
# ---------------------------------------------------------------------------

def _click_list_button(page) -> None:
    """「一覧表示」ボタンをクリックする。見つからない場合は RuntimeError を送出。"""
    try:
        page.click('input[value="一覧表示"]', timeout=10_000)
        return
    except Exception:
        pass
    try:
        page.get_by_text("一覧表示").click(timeout=10_000)
    except Exception as exc:
        raise RuntimeError(f"一覧表示ボタンが見つかりませんでした: {exc}") from exc


def _switch_month(page, year: int, month: int) -> None:
    """月プルダウンを指定年月に切り替え、一覧表示を再クリックしてテーブルを待つ。

    Args:
        page: Playwright の page オブジェクト
        year: 対象年（例: 2026）
        month: 対象月（1〜12）

    Raises:
        RuntimeError: プルダウン操作またはテーブル待機が失敗した場合
    """
    month_value = f"{year:04d}-{month:02d}"  # 例: "2026-04"
    try:
        page.select_option('select[name="month"]', month_value)
    except Exception as exc:
        raise RuntimeError(
            f"月プルダウン (select[name='month']) の操作に失敗しました。\n"
            f"  実際のHTMLのname属性を確認して調整してください。\n"
            f"  (値: {month_value}, エラー: {exc})"
        ) from exc

    _click_list_button(page)

    try:
        page.wait_for_selector("table", timeout=15_000)
    except PlaywrightTimeoutError as exc:
        raise RuntimeError(
            f"{year}年{month}月のテーブル表示待機でタイムアウトしました。"
        ) from exc


def _get_main_table_rows(page) -> list:
    """現在のページから最も行数の多いテーブルの行リストを返す。"""
    tables = page.query_selector_all("table")
    if not tables:
        return []
    main_table = max(tables, key=lambda t: len(t.query_selector_all("tr")))
    return main_table.query_selector_all("tr")


# ---------------------------------------------------------------------------
# スクレイピング
# ---------------------------------------------------------------------------

def scrape_attendance(
    config: dict, prev_biz_day: date
) -> tuple[list[dict], list[dict]]:
    """今日の打刻データと前営業日の退勤未打刻データをで1セッションで取得する。

    Returns:
        (today_employees, prev_day_missing)
        today_employees : 今日の全社員の打刻状況リスト
        prev_day_missing: 前営業日に退勤打刻がなかった社員リスト
    """
    url = config["url"]
    today = date.today()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()

        # ステップ1: URLを開く
        try:
            page.goto(url, wait_until="networkidle", timeout=30_000)
        except PlaywrightTimeoutError:
            log("ページ読み込みタイムアウト。取得済みコンテンツで続行します。")

        # ステップ2: 「一覧表示」ボタンをクリック（当月が表示される）
        try:
            _click_list_button(page)
        except RuntimeError as exc:
            log(str(exc))
            browser.close()
            return [], []

        # ステップ3: 当月テーブルを待機
        try:
            page.wait_for_selector("table", timeout=15_000)
        except PlaywrightTimeoutError:
            log("テーブルの表示を待機中にタイムアウトしました。")
            browser.close()
            return [], []

        # ステップ4: 当月テーブルから今日の打刻データを取得
        today_rows = _get_main_table_rows(page)
        if not today_rows:
            log("テーブルが見つかりませんでした。")
            browser.close()
            return [], []

        today_dt = datetime.combine(today, datetime.min.time())
        today_employees = _parse_rows(
            today_rows, _find_day_column(today_rows, today_dt), config
        )

        # ステップ5: 前営業日の退勤打刻データを取得
        prev_in_different_month = (
            prev_biz_day.year != today.year or prev_biz_day.month != today.month
        )

        if prev_in_different_month:
            # 前月に切り替えて前営業日のデータを取得
            log(
                f"前営業日 ({prev_biz_day}) は前月のため、"
                f"{prev_biz_day.year}年{prev_biz_day.month}月に切り替えます。"
            )
            try:
                _switch_month(page, prev_biz_day.year, prev_biz_day.month)
                prev_rows = _get_main_table_rows(page)
            except RuntimeError as exc:
                log(f"月切り替えに失敗しました: {exc}")
                prev_rows = []
        else:
            # 同月の場合は当月テーブルをそのまま使用
            prev_rows = today_rows

        prev_day_missing = (
            _get_missing_clockout(prev_rows, prev_biz_day) if prev_rows else []
        )

        browser.close()

    return today_employees, prev_day_missing


def _get_missing_clockout(rows: list, target_day: date) -> list[dict]:
    """指定日の列で退勤打刻がない社員リストを返す。

    退勤打刻なし = 時刻が0個（未打刻）または1個（出勤のみ）。
    """
    target_dt = datetime.combine(target_day, datetime.min.time())
    col_index = _find_day_column(rows, target_dt)

    if col_index < 0:
        log(f"{target_day.month}/{target_day.day} の列が見つかりませんでした。")
        return []

    name_col = _find_name_column(rows)
    missing = []

    for row in rows[1:]:  # ヘッダー行をスキップ
        cells = row.query_selector_all("td, th")
        if not cells:
            continue

        name_idx = name_col if name_col < len(cells) else 0
        name = cells[name_idx].inner_text().strip()
        if not name:
            continue

        if col_index < len(cells):
            cell_text = cells[col_index].inner_text().strip()
            times = re.findall(r"\d{1,2}:\d{2}", cell_text)
            clock_in = times[0] if times else ""
            clock_out = times[1] if len(times) >= 2 else ""

            if not clock_out:
                missing.append(
                    {"name": name, "clock_in": clock_in, "date": target_day}
                )

    return missing


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
        if text in _NAME_HEADERS or any(h in text for h in _NAME_HEADERS):
            return i

    log("氏名列が特定できず先頫列を使用します。")
    return 0


def _parse_rows(rows: list, day_col_index: int, config: dict) -> list[dict]:
    """データ行を走査して社員リストを生成する。"""
    employees = []
    name_col_index = _find_name_column(rows)

    for row in rows[1:]:
        cells = row.query_selector_all("td, th")
        if not cells:
            continue

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


def format_message(
    employees: list[dict],
    prev_missing: list[dict],
    prev_biz_day: date,
    config: dict,
) -> str:
    """Todayの打刻データと前営業日の退勤未打刻情報を整形した通知文を返す。"""
    today = datetime.now()
    date_str = today.strftime("%Y年%m月%d日")
    prev_date_str = f"{prev_biz_day.month}/{prev_biz_day.day}"  # 例: "4/30"

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

    # --- 今日の要確認 ---
    if issues:
        lines.append(f"\n■ 要確認（今日） ({len(issues)}名)")
        for e in issues:
            label = STATUS_LABELS.get(e["status"], e["status"])
            times = f"  出勤:{e['clock_in']}" if e["clock_in"] else ""
            if e["clock_out"]:
                times += f"  退勤:{e['clock_out']}"
            lines.append(f"  [{label}] {e['name']}{times}")
    else:
        lines.append("\n■ 要確認（今日）: なし")

    # --- 前営業日の退勤未打刻 ---
    if prev_missing:
        lines.append(
            f"\n■ 前営業日（{prev_date_str}）退勤打刻なし ({len(prev_missing)}名)"
        )
        for e in prev_missing:
            if e["clock_in"]:
                detail = f"出勤:{e['clock_in']} / 退勤打刻なし"
            else:
                detail = "出勤・退勤ともに打刻なし"
            lines.append(f"  ・{e['name']}：前営業日（{prev_date_str}）{detail}")
    else:
        lines.append(
            f"\n■ 前営業日（{prev_date_str}）退勤打刻: 全員確認済み"
        )

    # --- 正常打刻 ---
    if normal:
        lines.append(f"\n■ 正常打刻（今日） ({len(normal)}名)")
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
    except socket.gaierror:
        log(
            f"メール送信エラー: SMTPサーバー '{smtp_server}' の名前解決に失敗しました。\n"
            f"  → config.yaml の smtp_server を正しいホスト名またはIPアドレスに変更してください。"
        )
        raise
    except ConnectionRefusedError:
        log(
            f"メール送信エラー: SMTPサーバー '{smtp_server}:{smtp_port}' に接続できませんでした。\n"
            f"  → smtp_server ・ smtp_port ・ use_tls/use_ssl の設定を確認してください。"
        )
        raise
    except smtplib.SMTPAuthenticationError:
        log(
            "メール送信エラー: SMTP認証に失敗しました。\n"
            "  → config.yaml の sender ・ password を確認してください。"
        )
        raise
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

    # 土日・日曜・祝日は処理をスキップ
    today = date.today()
    if not is_business_day(today):
        log(f"本日は{skip_reason(today)}のため処理をスキップします。")
        sys.exit(0)

    # 前営業日を計算（月またぎ・祝日考慣済み）
    prev_biz_day = get_previous_business_day(today)
    log(f"前営業日: {prev_biz_day.strftime('%Y-%m-%d (%A)')}")

    config = load_config()

    log("スクレイピング中...")
    today_employees, prev_missing = scrape_attendance(config, prev_biz_day)

    if not today_employees:
        log("社員データを取得できませんでした。スクリプトを終了します。")
        sys.exit(1)

    log(f"{len(today_employees)} 名のデータを取得しました。")
    if prev_missing:
        log(f"前営業日退勤未打刻: {len(prev_missing)} 名")

    message = format_message(today_employees, prev_missing, prev_biz_day, config)
    log("\n" + message + "\n")

    # メール・Slackは独立して実行—片方が失敗してももう片方は実行する
    failed: list[str] = []

    try:
        send_email(message, config)
    except Exception:
        failed.append("メール")

    try:
        send_slack(message, config)
    except Exception:
        failed.append("Slack")

    if failed:
        log(f"通知失敗: {', '.join(failed)}")
        sys.exit(1)

    log("タイムカード確認完了")


if __name__ == "__main__":
    main()
