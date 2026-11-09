# 門市主機：開機自動啟動點餐系統 + 每日校時
# 以「系統管理員身分」開啟 PowerShell，在專案資料夾執行：
#   powershell -ExecutionPolicy Bypass -File deploy\windows\install_autostart.ps1
# 移除：Unregister-ScheduledTask -TaskName "KioskPOS-Server" -Confirm:$false
#       Unregister-ScheduledTask -TaskName "KioskPOS-TimeSync" -Confirm:$false
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$cmd = Join-Path $root "deploy\windows\start_store.cmd"

# 本檔必須存成 UTF-8 with BOM：Windows PowerShell 5.1 讀沒有 BOM 的 .ps1 會用系統字碼頁 (繁中 950) 解碼，中文字串解析失敗、整個腳本不會執行。
#
# 1. 開機啟動 (不需登入)。重試設定只在工作「無法啟動」時生效 (每分鐘重試，最多 999 次)；
#    伺服器啟動後才結束 (當機、5000 埠被占用、資料庫遷移失敗) 不會自動重啟，需要自動復原請改用 NSSM (docs/DEPLOY.txt 二-2 (A))。
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$cmd`"" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartInterval (New-TimeSpan -Minutes 1) -RestartCount 999 -ExecutionTimeLimit (New-TimeSpan -Days 3650)
Register-ScheduledTask -TaskName "KioskPOS-Server" -Action $action -Trigger $trigger -Settings $settings `
    -User "SYSTEM" -RunLevel Highest -Force | Out-Null
Write-Host "已建立開機自動啟動工作：KioskPOS-Server"

# 2. Windows 時間服務：使用國家時間與頻率標準實驗室的校時伺服器，開機自動啟動
w32tm /config /manualpeerlist:"time.stdtime.gov.tw,0x9 tock.stdtime.gov.tw,0x9" /syncfromflags:manual /update | Out-Null
Set-Service -Name w32time -StartupType Automatic
Start-Service w32time -ErrorAction SilentlyContinue
w32tm /resync /nowait | Out-Null

# 3. 每天 05:00 再校時一次
$syncAction = New-ScheduledTaskAction -Execute "w32tm.exe" -Argument "/resync /nowait"
$syncTrigger = New-ScheduledTaskTrigger -Daily -At 5:00
Register-ScheduledTask -TaskName "KioskPOS-TimeSync" -Action $syncAction -Trigger $syncTrigger -User "SYSTEM" -Force | Out-Null
Write-Host "已設定校時伺服器 time.stdtime.gov.tw 與每日校時工作：KioskPOS-TimeSync"
Write-Host "完成。重新開機後系統會自動啟動 (記錄檔：database\server.log)。"
