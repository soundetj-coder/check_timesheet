@echo off
REM ============================================================
REM タイムカード自動確認スクリプト 実行バッチ
REM Windowsタスクスケジューラから呼び出す
REM
REM タスクスケジューラ設定例:
REM   操作     : プログラムの開始
REM   プログラム: C:\attendance\run_attendance.bat
REM   開始場所  : C:\attendance
REM   トリガー  : 毎日 10:00
REM ============================================================

SETLOCAL

REM スクリプトが置かれているディレクトリを作業ディレクトリにする
cd /d "%~dp0"

REM ログディレクトリ作成
if not exist logs mkdir logs

REM ログファイル名 (日付付き)
set LOG_FILE=logs\attendance_%DATE:~-10,4%%DATE:~-5,2%%DATE:~-2,2%.log

REM Python実行 (フルパス指定推奨: 例 C:\Python314\python.exe)
python check_attendance.py >> "%LOG_FILE%" 2>&1

if %ERRORLEVEL% neq 0 (
    echo [ERROR] check_attendance.py が異常終了しました。ログを確認してください: %LOG_FILE%
    exit /b 1
)

ENDLOCAL
