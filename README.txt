====================================================================
智慧自助點餐、POS 收銀、人臉辨識與多店家營運系統 (Kiosk & POS)
====================================================================

一套給單店與連鎖餐飲使用的門市系統：顧客自助點餐機、櫃台 POS、廚房出單看板、取餐叫號、
店家後台，以及總部的線上點餐網站。門市斷網照常營業，連線後與線上端雙向同步。

相關文件
- README.txt   (本檔) 功能、安裝、設定、指令、開發慣例
- SYNC.txt     門市 ↔ 線上端同步的設計與合併規則
- DEPLOY.txt   線上端 Docker 部署、門市 Windows 部署、同步設定、線上付款、上線前檢查清單
- MANUAL_TEST.txt  人工實測步驟 (全功能，每項含步驟與預期結果)


一、系統角色 (環境變數 SYNC_ROLE)
--------------------------------------------------------------------
  standalone (預設)  單機門市：SQLite，不同步。
  node               門市分店：SQLite，可完全離線營業；背景與線上端同步。
  hub                線上端：PostgreSQL + Docker；線上點餐網站與總部後台，所有分店資料的匯集點。

    [門市 A (node)]──┐  HTTPS + 分店權杖
    [門市 B (node)]──┼──── [線上端 (hub)] ──── 顧客手機 (線上點餐、綠界付款)
    [門市 C (node)]──┘

  - 菜單、優惠券、紅利設定、員工帳號、會員與點數：同一店家的所有分店共用 (雙向同步)。
  - 訂單：分店自己的訂單上傳到線上端；線上訂單付款後只送到顧客選的取餐分店。
  - 庫存：各分店獨立；線上端用分店回報的庫存快照接受線上訂單。


二、功能
--------------------------------------------------------------------
顧客
- 自助點餐 (/)：菜單分類與搜尋、套餐配餐、客製化 (冰塊/甜度/辣度/加料)、條件加購、優惠券、紅利折抵、我的訂單。
- 會員：手機 + PIN 登入；門市另可人臉登入 (YuNet + SFace，選填並須另行同意)；紅利商城 (/rewards) 兌換優惠券與商品。
- 訪客點餐：門市 (點餐機或顧客用手機連門市網站) 可以；線上點餐只限會員。
- 桌邊掃碼點餐 (門市)：顧客手機連店內 Wi-Fi 掃桌上的 QR code (/t/<代碼>)，以訪客或會員身分點餐；
  固定內用、訂單標記桌號並直接送廚房，用餐後到櫃檯結帳。
- 線上點餐 (線上端)：選取餐分店 → 綠界信用卡付款 → 付款後訂單送到分店廚房；取餐號由分店收單時編號。
- 隱私權告知 (/privacy)：個資法第 8 條告知事項；會員可在此撤回人臉同意並刪除人臉資料。

門市員工
- POS (/pos)：觸控點餐、掛單 (F4)、結帳 (F2)、會員查詢與設定 PIN、找零、80mm 熱感應收據、當日訂單、
  桌位結帳 (一次結清某桌所有掃碼點餐的訂單)。
- 廚房 KDS (/kitchen)、取餐核銷 (/pickup)、叫號看板 (/counter，公開、會員姓名遮罩、語音叫號)：SSE 即時更新。

店家後台 (/admin)
- 營運分析 (今日營收、預估出餐時間、熱門排行)、AI 商品文案與營運建議 (Gemini → 本地 Qwen → 規則引擎)。
- 菜單 / 套餐 / 加料 / 優惠券 / 紅利設定 / 會員管理、稽核日誌、Excel 匯出與智慧匯入。
- 員工帳號 (/admin/staff)、隱私權告知設定 (/admin/privacy)。
- 桌位 QR (/admin/tables，門市)：新增桌位 (可用 A1-A10 範圍)、改名、停用、重新產生 QR code (舊的立即失效)、
  設定 QR code 使用的門市網址、列印桌卡 (A4 每頁 6 張)。
- 整庫備份 / 還原 (只限平台管理者，只支援 SQLite)。
- 線上端：分店管理與分店權杖 (/admin/nodes)；門市：同步狀態與立即同步 (/admin/sync)。
- 換日自動結案：前一天仍在製作中的訂單於隔天自動改為已出餐。

進階營運功能 (2026-10 新增；外部服務的金鑰一律在後台「系統設定 /admin/settings」由管理者填寫，未填寫的功能自動停用)
- A 金流與法規
  - 電子發票 (綠界 B2C)：結帳自動開立 (手機條碼 / 自然人憑證 / 捐贈 / 統編 / 紙本)、失敗自動重送、
    取消自動作廢 (跨期改折讓)、部分退款開折讓、後台列表 / 重送 / 修改載具 / 列印證明聯 (/admin/invoices)。
  - 門市電子支付：LINE Pay 掃顧客付款碼 (Offline API，扣款失敗自動取消訂單)；信用卡刷卡機輸入授權碼 / 末四碼記帳。
  - 作廢 / 部分退款：沒有「作廢 / 退款」權限的員工需主管輸入帳密核准；LINE Pay 自動退款、其他方式登記人工退款
    (/admin/refunds)；儲值金自動退回。
- B 收銀營運
  - 交班 / 日結 (/admin/shifts)：開班零用金、存入 / 支出、依面額盤點、應有現金與帳差、交班報表列印。
  - 服務費、整單折扣與改價 (超過門檻需主管核准)、員工個別權限 (/admin/staff)。
  - 分開結帳 / 多種付款、換桌 / 併桌、整桌開一張發票。
  - 網路熱感印表機 (ESC/POS TCP 9100)：收據、各工作站廚房單、補印、離線佇列重試 (/admin/printers)。
- C 廚房出餐：KDS 工作站分站 (/kitchen?station=)、各站完成、加急、逾時顏色；叫號語音播報設定；
  顧客手機掃收據 QR code 追蹤出餐 (/track/<訂單>)；完成時 LINE 推播。
- D 菜單：供應時段 / 星期 / 販售期間、內用 / 外帶 / 線上 / 外送不同價、過敏原與素食標示、英文 / 日文菜單
  (/admin/menu_pro)；原物料配方扣料、進貨 / 報廢 / 盤點、低庫存通知、毛利 (/admin/inventory)。
- E 會員行銷：會員等級 (依累積消費自動升級、折扣與點數倍率)、生日禮、儲值金 (加值贈送)、LINE 登入 / 綁定與推播、
  簡訊驗證註冊與忘記 PIN (/admin/members_pro)；
  優惠券使用期限 (活動起訖日、領取後 N 天到期)、會員專屬券、批量發放 (全部 / 等級 / 本月壽星 / 已綁 LINE / 手機清單，
  可指定張數與到期日、LINE 通知、整批撤回未使用)；會員票券視窗分開未使用 / 已使用，已使用與過期只顯示近 7 天；
  優惠券防濫用 (猜錯代碼次數過多暫停查詢、公開代碼限會員 / 每人限用次數、最高折抵金額、代碼格式檢查)；
  紅利商城對已持有的券顯示「剩餘 N 張」並可重複兌換；促銷活動 (買 X 送 Y、第 N 件折扣、滿額折 / 滿額贈，
  可限時段 / 管道 / 會員) (/admin/promotions)。
- F 通路：Uber Eats / foodpanda 訂單 Webhook 自動匯入 (或 POS 手動登錄外送單) (/admin/delivery)；
  線上訂位 (/reserve) 與現場取號候位 (/waitlist)，店員看板 (/reservations)，叫號看板顯示候位號碼。
- G 報表與總部：進階報表 (毛利、時段熱度、付款方式、管道、分類 / 品項、收銀員、分店比較、促銷成效)、Excel 匯出、
  定期 Email 寄送 (/admin/reports)；員工打卡 (/timeclock)、班表與工時 (/admin/timeclock)。
- 總部統一設定：不含金鑰的系統設定 (收銀規則、KDS、會員、訂位、報表…) 在線上端儲存後自動下發到所有分店；
  分店可自訂 (覆寫) 並隨時「改回總部設定」；含金鑰與本機性質的設定各台主機自己設定。
- 帳號安全 (/admin/security)：兩步驟驗證 (驗證 App + 一次性備用碼，可要求負責人 / 店長必須啟用)、
  登入中的裝置列表與個別登出、登出所有裝置 (含各分店)、閒置自動登出；負責人可在員工帳號頁強制登出 / 重設員工的 2FA。
- 後台會員名冊分頁與搜尋 (每頁 50 位，票券於開啟時才載入)。
- 線上付款方式：信用卡 / Apple Pay / ATM 虛擬帳號 / 超商代碼可在「系統設定 → 付款方式」勾選；ATM、超商代碼依繳費期限保留
  訂單 (會員訂單頁顯示帳號 / 代碼)；取消已付款的線上信用卡訂單可自動向綠界退刷 (失敗時後台「綠界退刷」重試)。
- 錢櫃：收據印表機勾選「接錢櫃」後，櫃檯收 / 退現金 (結帳、桌位結帳、儲值、零用金、退款) 自動開錢櫃；
  POS「開錢櫃」按鈕手動開啟並留稽核紀錄。
- 客顯螢幕：POS「客顯」開啟第二個視窗 (/pos/display，拖到面向顧客的螢幕)，即時顯示品項、應付金額、收款與找零。
- 會員動態條碼：會員在紅利商城開啟「會員條碼」(每分鐘更新、截圖無效)，櫃檯以掃碼槍掃描即帶出會員。
- 餐點規格 (大小杯)：在 /admin/menu_pro 設定「中杯:0, 大杯:15」，自助點餐 / POS 選規格自動加價。
- 線上預約取餐時間 (系統設定 → 線上點餐)：顧客結帳選「盡快」或指定時段；廚房看板 / POS / 追蹤頁顯示「預約 HH:MM」，
  預約明天以後的訂單當天才出現在廚房看板、不會被換日自動結案。
- 桌邊呼叫服務：掃碼點餐頁「加水 / 想結帳 / 呼叫服務」，POS「呼叫」按鈕顯示待處理並提示音，按「已處理」。
- 點餐機待機輪播：首頁閒置後全螢幕輪播推薦餐點 (系統設定 → 菜單顯示)；「大字」按鈕切換大字 / 高對比模式。
- 餐後評分：出餐追蹤頁 (收據 QR code) 1~5 星與意見，高分邀請到 Google 評論、低分通知店家；後台「顧客評價」(/admin/feedback)。
- 熱量與營養標示：/admin/menu_pro 填寫，點餐頁顯示 kcal 與營養文字，購物車顯示熱量合計。
- 點數有效期限：先得到的點數先到期 (每天由線上端 / 單機執行)，紅利商城顯示最近到期的點數，到期前 LINE 提醒。
- 集點卡 (系統設定 → 集點卡)：買指定分類每份 1 章或每筆滿額 1 章，集滿自動送點數 / 優惠券；取消訂單扣回。
- 推薦好友：會員在紅利商城看到推薦碼 (註冊網址 /register?ref=推薦碼)，新會員第一筆消費達門檻後雙方得點。
- 禮物卡 (系統設定 → 禮物卡；後台 /admin/gift_cards)：POS 售出 16 碼禮物卡 (可列印卡號)、查詢餘額、結帳以「禮物卡」扣款、
  作廢 / 退款退回卡片；各分店通用。售出時記為預收款不開發票，兌換消費時才開立。
- 會員自助 (紅利商城「我的資料」)：下載自己的個人資料 (JSON)、輸入 PIN 刪除帳號 (訂單保留但去識別化；儲值金有餘額不可刪)。
- 供應商與採購單 (/admin/purchasing)：原料設定預設供應商、目標庫存、採購單位；低庫存一鍵建立建議採購單 (依供應商分單)，
  編修 → 送出 (可 Email 給供應商) → 收貨入庫 (填有效期限)；採購單可列印。
- 原料批號效期：進貨 / 收貨建立批號，扣料、報廢、盤虧依「先到期先出」；即將到期與已過期的批號在原物料頁顯示並通知，可整批報廢。
- 備料預測 (/admin/forecast)：以過去幾週同一個星期幾的銷量平均預估份數、來客時段分布與原料需求 (標示不足)。
- 人事成本率：員工帳號頁設定時薪，營運報表顯示期間內打卡工時 × 時薪與佔營業額比例。
- 自有外送 (系統設定 → 自有外送；派車 /admin/dispatch)：線上點餐與 POS 電話訂餐可選「外送到府」(地址、電話、配送區域)，
  依區域收外送費 (最低消費、滿額免運)；派車頁指派外送員 → 出發 → 送達 / 失敗，顧客在追蹤頁與「我的訂單」看到配送狀態。
- 套餐任選 (後台套餐「任選群組」)：每行「群組: 餐點, 餐點+加價」，例如「飲料: 紅茶, 綠茶, 珍珠奶茶+15」；
  點餐機 / 桌邊 / 線上 / POS 點套餐時每組任選一項 (可升級加價、選項售完不能選、選項可客製冰塊甜度)，扣選中的配餐庫存。
- 混合付款 (POS 付款方式「混合付款」)：一筆訂單分現金 / 刷卡 / 儲值金 / 禮物卡 / 行動支付，每種各記一筆收款
  (交班報表依實際付款方式統計)；作廢時每種方式各自退回，部分退款可指定退回哪一種。
- 單品作廢 / 改單 (POS 當日訂單「作廢/退款」→ 作廢品項；桌邊未結帳的訂單「改單」)：作廢某品項的部分份數，
  回補庫存與原料，依比例分攤折扣後部分退款 (未結帳則直接降低應付)；廚房看板顯示作廢份數；作廢份數會同步。
- 桌位平面圖 (桌位 QR 頁)：設定座位數 / 區域 / 形狀，拖拉擺放後儲存；POS「桌位結帳」切換「平面圖」即時顯示
  空桌 / 用餐中 (用餐分鐘、未結帳金額) / 呼叫中 / 即將訂位，點用餐中的桌跳到該桌帳單。
- 桌邊線上結帳 (系統設定 → 桌邊線上結帳)：掃碼點餐的顧客按「線上結帳」查看本桌帳單，以綠界信用卡付清後自動完成桌位結帳；
  付款期間已在櫃檯結帳或訂單有異動 → 標記需退款並通知店家。
- 寄杯 / 多次券 (系統設定 → 寄杯 / 多次券)：設定「美式咖啡 10 杯 $450」等商品；POS 綁定會員後售出 (建立寄杯購買訂單，
  收款 / 發票 / 交班照常)；會員在點餐機 / 桌邊 / 線上 (購物車「我的寄杯」) 或 POS 兌換一份 (免費，只收加料)，
  同時在多個通路兌換不會扣成負數；兌換訂單取消退回次數；購買訂單未使用才可整筆作廢；剩餘次數跨分店同步。
- 分店價格 / 上下架 (線上端：系統設定 → 分店價格 / 上下架)：總部為某家分店設定不同售價 (例如機場店貴 $20) 或下架餐點；
  通路價與特價依同一個價差加減；只同步給該分店，該分店的點餐機 / POS / 桌邊與以該分店取餐的線上點餐都套用，分店頁面為唯讀。
- 菜單工程分析 (系統設定 → 菜單工程分析)：依銷售份數與每份毛利把餐點分成明星 / 金牛 / 潛力 / 瘦狗四象限 (散佈圖與建議)，
  可依期間、分類、分店篩選並匯出 Excel；未設定成本的餐點會提示。
- 分店叫貨 / 中央廚房 / 調撥 (系統設定 → 分店叫貨 / 調撥)：分店向總部中央廚房 (線上端的原料庫存) 或其他分店叫貨，
  出貨方出貨 (扣自己的原料) 或拒絕、收貨方收貨 (加到自己的原料，可填實收數量，差異會通知)；分店間也可直接調撥。
  單據經線上端同步給雙方，同時按兩次出貨只扣一次。
- 會員分群與自動行銷 (系統設定 → 會員分群與自動行銷)：依最近消費 / 次數 / 金額 (RFM) 分成冠軍、忠實、新客、需要關注、
  快流失、沉睡、未消費，可看名單與匯出 Excel；「久未消費自動發券」每天 10:00 對超過 N 天沒來的會員發券並 LINE 推播
  (冷卻期內不重發)，「排程推播」指定時間對某個分群發送；顯示 30 天回購人數 / 營業額與用券數。會員可在紅利商城關閉優惠訊息
  (拒絕行銷的會員不發券、不推播，也排除在 LINE 群發外)。自動行銷只在線上端 / 單機執行。
- 訂位訂金與未到管理 (系統設定 → 訂位 / 候位)：線上訂位達設定人數須以綠界信用卡預付每位訂金，30 分鐘內未付款自動取消；
  入座自動退還 (或結帳折抵)、訂位前 N 小時以前取消自動退刷、之後取消或未到不退；同一支手機未到達設定次數暫停線上訂位；
  訂位前 N 小時以簡訊或 LINE 提醒。訂位看板顯示訂金狀態、未到次數與本月未到 / 不退金額。
- 員工異常偵測 (系統設定 → 員工異常偵測)：各收銀員的整單作廢 / 部分退款 / 單品作廢、手動折扣 / 改價、交班短溢與全店平均比較，
  超過門檻時標示並可每天通知；交班帳差超過設定金額時立即通知。
- 會計匯出 (系統設定 → 會計匯出)：依收款紀錄產生每日傳票 (借：現金 / 信用卡 / 行動支付 / 預收款項…，貸：銷貨收入 + 銷項稅額；
  退款、儲值、禮物卡、零用金另有分錄，借貸平衡) 與發票明細，下載 CSV (UTF-8 BOM) / Excel 匯入會計軟體；科目代號可設定。
- 對外 API 與 Webhook (系統設定 → API 與 Webhook，負責人)：API 金鑰 (只顯示一次、可設權限範圍、可停用) 讀取訂單 / 菜單 / 會員 /
  每日統計、更新售完與庫存 (/api/v1/...)；Webhook 在訂單成立、付款、狀態改變、取消時以 HMAC 簽章 POST 到指定 https 網址，
  失敗自動重試、不送內網位址，後台可看送出紀錄、測試與重送。
- 裝置管理 (系統設定 → 裝置管理)：登記點餐機 / POS / 廚房看板，在裝置上輸入配對碼註冊；顯示在線、程式版本 (舊版提示重新整理)、
  最後頁面與 IP；可遠端鎖定 (畫面鎖定、操作被拒) 與要求重新整理。
- 舊資料封存 (系統設定 → 舊資料封存，負責人)：超過 N 個月的已結束訂單 (含明細、收款、退款、發票) 與日誌搬到
  database/archive 的封存檔，主資料庫保持輕量；可下載、可依訂單編號 / 日期 / 會員手機查詢；會員等級累積消費不受影響；可每月自動執行。
- 營業費用 (系統設定 → 營業費用，店長以上)：逐筆登錄房租、水電、薪資 (未打卡部分 / 勞健保)、耗材、行銷、修繕、手續費、稅捐等，
  可填廠商與進項發票號碼；勾選「每月固定」的費用下個月一鍵複製 (重複按不會重複複製)。分店登錄的費用上傳到線上端
  (線上端看得到但不能改)，刪除為標記刪除並一起同步。
- 損益表 (系統設定 → 損益表，需報表權限；費率在系統設定「損益表手續費」)：營業額 − 銷項稅額 = 營業收入淨額，− 銷貨成本 (配方原料或餐點單位成本) = 營業毛利，
  − 人事成本 (打卡工時 × 時薪) − 金流 / 外送平台手續費 (依收款方式 × 設定的費率估算) − 登錄的營業費用
  = 營業淨利與淨利率；與上一期比較、近 6 個月趨勢、可選月份或任意區間 (最長 400 天)，線上端可依分店篩選，可匯出 Excel。
- 原料成本 / 售價歷史 (系統設定 → 成本 / 售價歷史)：原料單位成本與餐點售價 / 特價 / 通路價 / 單位成本的每一次變動
  (後台編輯、進貨加權平均、調撥收貨、匯入、同步) 都留下紀錄 (誰、何時、舊值 → 新值、漲跌 %)；
  並列出與 N 天前相比原料成本上漲最多的餐點、每份成本與毛利率的變化。
- API 說明文件 (/developers，公開頁面)：每個 /api/v1 端點的參數、權限、回應欄位、錯誤碼、Webhook 標頭與簽章驗證範例，
  可貼上 API 金鑰「線上試打」(只在瀏覽器中使用)；/developers/openapi.json 為 OpenAPI 3.0 規格，可匯入 Postman / Swagger。
- 團體預購 (系統設定 → 團體預購；門市 / 單機)：主揪在點餐首頁「團體預購」(或店員在後台代客建立) 設定取餐時間與截止時間，
  取得分享連結與只顯示一次的管理連結；同事打開分享連結各自填名字、選餐點 / 規格 / 備註 (同一個瀏覽器或個人連結可修改、刪除)；
  截止時自動 (或主揪按「送單」) 合併成一張預約取餐訂單 (現場付款，備註列出每個人點的內容)，廚房在取餐前出現；
  可列印每人一格的分裝清單；送單失敗 (售完等) 通知店家，主揪調整後可再送；同時送單只會建立一張。
- 排班建議 (系統設定 → 排班建議，店長以上；備料預測頁「依預測排班」)：以過去同一個星期幾各時段的訂單數估算每小時需要的人力
  (每人每小時可處理張數、最少人數可設定)，與班表比較標示不足 / 過多，產生建議班別 (選員工後一鍵加入班表)，並估算人事成本率。
- 點餐機斷線暫存：點餐機送出訂單時連不到門市主機，現金 (櫃檯付款) 且未用點數 / 優惠券 / 紅利 / 寄杯的訂單先存在點餐機，
  顧客拿到 4 碼暫存單號至櫃檯付款；連線恢復後自動補送 (以訂單編號去重，不會重複建單；一律以訪客建立、金額以補送時為準)，
  左下角「暫存單」按鈕顯示待補送 / 已補送 (取餐號) / 失敗原因；暫存超過 3 小時不自動補送，改由店員確認。
- 一鍵展示資料 (flask demo seed，見「五、常用指令」)：建立獨立的「示範店家」並產生過去 N 天的營運資料
  (尖峰時段分布、假日較多、多種付款方式、會員常客 / 久未消費、少量作廢、評分、打卡、每月費用、進貨成本變動、收單中的團購)，
  展示或教育訓練時不必動到正式資料；可重建、可整個刪除。
- 即時看板 (/admin/live)：今天營業額 / 訂單 / 客單價 / 與昨日同時段比較、各分店 (線上端彙整) 連線狀態、每小時營業額、熱銷品項、最新訂單，自動更新。
- H 系統維運：門市資料庫每日自動備份、系統健康 / 錯誤紀錄、Webhook / Email / LINE 錯誤通知、主機時鐘誤差檢查
  (/admin/system)；線上端 PostgreSQL 由 docker compose 的 backup 服務每日備份；PWA (可安裝、斷線提示)。


三、快速開始 (Windows 門市 / 單機)
--------------------------------------------------------------------
1. 安裝 Python 3.10 以上 (目前以 3.13 / 3.14 測試)。
2. 建立虛擬環境並安裝套件 (專案資料夾中只保留一個虛擬環境 .venv)：
     python -m venv .venv
     .venv\Scripts\activate                        (PowerShell；Linux / macOS：source .venv/bin/activate)
     pip uninstall -y opencv-python                 (舊版安裝過才需要；本專案改用 opencv-python-headless)
     pip install -r requirements.txt
3. 設定：複製 .env.example 為 .env，至少填 ADMIN_USERNAME / ADMIN_PASSWORD (負責人帳號，只在第一次建立時使用)。
   未填會自動產生一組隨機帳密並只在啟動畫面顯示一次。
4. 啟動：
     python serve_windows.py      正式營業 (Waitress，預設 0.0.0.0:5000，區網內的看板與 POS 可連線)
     python run.py                開發除錯 (FLASK_DEBUG=1 開啟除錯模式)
   前台 http://127.0.0.1:5000/   後台 http://127.0.0.1:5000/admin
5. 啟動時會自動升級資料庫 (AUTO_MIGRATE=1)；舊版資料庫會先整理成基準結構再升級，舊的 static/member、
   static/menu 上傳檔會搬到 uploads/。升級前請先備份 database/ 與 uploads/ (或舊的 static/member、static/menu)。
6. 後台「隱私權告知」填寫營運者名稱與聯絡方式。

門市連到線上端：線上端後台「分店管理」新增分店取得權杖，在門市 .env 設定
  SYNC_ROLE=node、SYNC_HUB_URL=https://線上端網址、SYNC_NODE_TOKEN=kn_…，重新啟動即可 (DEPLOY.txt 第三節)。
線上端部署：Docker (PostgreSQL + Redis + Caddy 自動 HTTPS)，步驟見 DEPLOY.txt 第一節。

本機模擬線上端 (同一台電腦同時跑線上端與門市)：
  1. cp deploy/hub.env.example .env.hub，設定 SITE_DOMAIN=localhost、PUBLIC_BASE_URL=https://localhost，
     填 POSTGRES_PASSWORD (DATABASE_URL 裡的密碼要相同) 與 SECRET_KEY → docker compose up -d --build。
     第一次的負責人帳密在 docker compose logs app；後台 https://localhost/admin (自簽憑證，瀏覽器警告按繼續)。
  2. 線上端後台「分店管理」新增分店 (或 docker compose exec app flask --app run sync create-node --name 台南店) 取得權杖。
  3. 匯出 Caddy 的本機憑證讓門市信任：
       docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt database\caddy-root.crt
  4. 門市 .env (先備份 database\menu.db)：SYNC_ROLE=node、SYNC_HUB_URL=https://localhost、SYNC_NODE_TOKEN=kn_…、
     REQUESTS_CA_BUNDLE=<專案路徑>\database\caddy-root.crt → python serve_windows.py，後台「同步狀態」確認已連線。
  - 綠界伺服器通知連不到 localhost：付款結果改由付款後瀏覽器回到本站與每分鐘向綠界查詢來確認。
  - REQUESTS_CA_BUNDLE 只用於本機模擬 (會讓門市只信任這張憑證)；正式上線前刪除。


四、環境變數
--------------------------------------------------------------------
門市 / 單機寫在專案根目錄的 .env (範例 .env.example)；線上端寫在 .env.hub (範例 deploy/hub.env.example)。
.env 的值優先於系統環境變數。

  一般
    ADMIN_USERNAME / ADMIN_PASSWORD   預設店家負責人 (第一次建立時使用)
    RESET_ADMIN_PASSWORD_ON_START     1 = 重啟時把負責人密碼重設為 ADMIN_PASSWORD (忘記密碼時用，用完改回 0)
    SECRET_KEY                        Session 簽章金鑰；留空自動產生並存於 database/.secret_key (線上端必填)
    DATABASE_URL                      留空 = database/menu.db (SQLite)；線上端為 postgresql://…
    AUTO_MIGRATE                      1 = 啟動時升級資料庫；0 = 需先執行 flask --app run setup
    UPLOAD_FOLDER_MEMBER / _MENU      上傳檔位置 (預設 uploads/member、uploads/menu)
    HOST / PORT                       serve_windows.py、run.py 的監聽位址與埠號 (預設 0.0.0.0:5000)
    WAITRESS_THREADS                  Waitress 執行緒數 (每條 SSE 看板連線占 1 條，預設 128)
  安全
    TRUSTED_PROXY_COUNT               前面有幾層反向代理 (直接對外 0；Nginx / Cloudflare / Caddy 後面 1)
    SESSION_COOKIE_SECURE             全站 HTTPS 時 1 (門市以 http://區網IP 使用時維持 0)
    CSP_MODE                          enforce / report-only / off
    RATELIMIT_ENABLED                 1 = 啟用 IP 頻率限制
    REDIS_URL                         選用：多行程部署時共用頻率限制計數與 SSE 通知
  同步 (說明見 SYNC.txt)
    SYNC_ROLE                         standalone / node / hub
    SYNC_HUB_URL / SYNC_NODE_TOKEN    分店：線上端網址 (https) 與分店權杖
    SYNC_INTERVAL / SYNC_LONG_POLL    分店：上傳檢查間隔、等待線上新訂單的長輪詢秒數
    SYNC_TENANT_SLUG / SYNC_AUTOSTART 分店：要同步的本機店家、啟動時是否自動同步
    SYNC_NODE_OFFLINE_AFTER           線上端：分店超過幾秒沒連線就暫停該分店的線上點餐
  線上付款 (線上端；說明見 DEPLOY.txt 第五節)
    ONLINE_PAYMENT                    1 = 線上點餐需先付款 (付款後才送分店)
    ECPAY_STAGE                       1 = 綠界測試環境 (預設，免註冊、不扣款)；0 = 正式收款 (需填下面三項)
    ECPAY_MERCHANT_ID / _HASH_KEY / _HASH_IV   綠界正式特店資料
    PUBLIC_BASE_URL                   綠界付款通知用的公開網址 (留空使用目前網址)；門市使用桌邊線上結帳 / 訂位訂金時
                                      門市主機也要設定 (對外 HTTPS)，訂位提醒簡訊的取消連結也用這個網址
    PAYMENT_TIMEOUT_MINUTES           逾時未付款自動取消 (預設 15 分鐘)
  Webhook (說明見 app/services/webhook_service.py)
    WEBHOOK_ALLOW_PRIVATE             1 = 允許送到內網 / 本機位址 (只限自架內網整合；預設只送公開網際網路位址)
    WEBHOOK_ALLOW_HTTP                1 = 允許 http:// 網址 (預設只允許 https)
  選用
    GEMINI_API_KEY                    Google Gemini (AI 文案與營運建議)


五、常用指令
--------------------------------------------------------------------
  flask --app run setup                              升級資料庫並建立預設資料 (可重複執行)
  flask --app run db migrate -m "說明"                修改 app/models/ 後產生遷移檔 (檢查後再 setup)
  flask --app run sync create-node --name 台南店      (線上端) 新增分店並顯示權杖
  flask --app run sync now / sync status             (分店) 立即同步 / 顯示同步狀態
  flask --app run payments expire                    (線上端) 取消逾時未付款的訂單 (背景也會每分鐘執行)
  flask --app run privacy purge-inactive [--apply]   刪除停止使用超過設定月數的會員 (多分店時在線上端執行)
  flask --app run tables check [--fix]               檢查桌邊點餐的資料衝突 (沒有桌位的未結帳訂單、已取消卻已收款、
                                                     久未結帳)；--fix 把「已取消卻已收款」改為需退款
  flask --app run ops check-integrations            檢查後台填寫的外部服務 (電子發票、LINE Pay、LINE、簡訊、SMTP、外送平台)
  flask --app run ops retry-invoices                 立即重送待開立的電子發票 (背景每 5 分鐘也會執行)
  flask --app run ops backup [--keep-days 14]        立即備份門市資料庫到 database/backups/
  flask --app run ops daily                          立即執行每日工作 (自動備份、生日禮、定期報表；已執行過的當天不重複)
  flask --app run demo seed [--days 60] [--orders-per-day 80] [--password ...] [--reset] [--slug demo]
                                                     建立獨立的「示範店家」(/shop/demo) 與過去 N 天的訂單、會員、成本、費用、打卡、
                                                     團購等資料，所有報表都有內容 (帳號 demo_owner / demo_manager / demo_pos /
                                                     demo_pos2 / demo_kitchen，密碼顯示在畫面；示範會員手機 0900000001 起、PIN 2468)；
                                                     不會動到其他店家，同代號的真實店家一律拒絕；只能在單機執行 (分店主機 / 線上端拒絕)
  flask --app run demo remove [--yes]                刪除示範店家與它的所有資料 (只刪由 demo seed 建立的店家)
  python tests/test_payment_ecpay_script.py [live]   線上付款測試 (見「七、測試」)
  docker compose up -d --build                       (線上端) 部署 / 更新


六、專案結構
--------------------------------------------------------------------
Project Root/
├── app/
│   ├── __init__.py            create_app：設定、擴充元件、安全標頭 / CSP、錯誤處理、多店家、啟動遷移、背景工作
│   ├── config.py              所有設定 (讀取 .env)
│   ├── cli.py                 flask CLI 指令 (setup、sync、payments、privacy、tables、ops)
│   ├── extensions.py          db、migrate、csrf、login_manager、limiter
│   ├── errors.py              AppError 與統一錯誤處理
│   ├── tenant_context.py      多店家隔離 (查詢自動加店家條件、寫入防跨店)
│   ├── utils.py               role_required、platform_admin_required、輸入驗證
│   ├── models/                ORM 模型
│   │   ├── mixins.py          uuid / updated_at / sync_rev / tenant_id
│   │   ├── enums.py           訂單狀態、來源、付款方式、憑證狀態
│   │   ├── tenant.py          店家、員工帳號
│   │   ├── table.py           門市桌位 (QR code 代碼、平面圖位置 / 座位數)、桌邊線上結帳 (TablePayment)
│   │   ├── user.py            會員 (含拒絕行銷、封存訂單累積消費)、紅利設定、兌換憑證、日誌、隱私權告知設定
│   │   ├── menu.py、order.py、coupon.py
│   │   ├── sync.py            同步序號、刪除紀錄、點數異動帳、分店、庫存快照
│   │   ├── settings.py        後台系統設定 (每家店一組 key / JSON，門市本機資料不同步)
│   │   ├── payment.py         收款紀錄、退款、桌位結帳批次、電子發票
│   │   ├── shift.py、printing.py、inventory.py、member.py、promotion.py、reservation.py、staff.py
│   │   │                      交班、印表機 / 工作站、原物料 / 供應商 / 批號、會員等級 / 儲值金 / 集點 / 禮物卡 / 寄杯、
│   │   │                      促銷、訂位 (含訂金) / 候位、打卡 / 班表
│   │   ├── branch.py          分店價格 / 上下架 (BranchMenuOverride)、分店叫貨 / 調撥單 (StockTransfer)
│   │   ├── marketing.py       自動行銷活動與發送紀錄 (線上端 / 單機本機資料)
│   │   ├── integration.py     API 金鑰、Webhook 端點 / 送出紀錄 / 訂單狀態快照、裝置 (Device)
│   │   ├── expense.py         營業費用 (Expense，分店上傳線上端)、原料成本 / 售價變動歷史 (CostHistory，本機)
│   │   ├── group_order.py     團體預購與參加者點餐 (門市本機資料)
│   │   └── system.py          PIN 登入鎖定
│   ├── services/              商業邏輯
│   │   ├── order_service.py   結帳與訂單的對外入口 (只匯出，實作在下列四個模組；呼叫端一律 import 這裡)
│   │   ├── order_pricing.py   優惠碼、加購配額、購物車明細解析與計價、通路價 / 服務費 / 整單折扣
│   │   ├── order_stock.py     取餐號、條件式扣 / 回補庫存、分店庫存快照、線上取餐分店
│   │   ├── order_checkout.py  原子結帳 process_checkout (各通路共用)
│   │   ├── order_lifecycle.py 訂單狀態機、取消回補、單品作廢、一鍵出餐、換日自動結案
│   │   ├── points_service.py  會員點數 (異動帳)
│   │   ├── reward_service.py、voucher_service.py   紅利兌換
│   │   ├── payment_ecpay.py   綠界金流 (檢查碼、付款通知、逾時處理)
│   │   ├── privacy_service.py 隱私權告知、同意紀錄、撤回人臉、刪除停用會員
│   │   ├── table_service.py   桌位管理、QR code、掃碼綁定桌位、桌位結帳
│   │   ├── upload_paths.py    上傳檔路徑檢查與刪除 (限制在上傳資料夾內)
│   │   ├── cv_service.py      人臉偵測與特徵比對
│   │   ├── ai_service.py、analytics_service.py
│   │   ├── backup_service.py、excel_service.py、menu_service.py
│   │   ├── event_bus.py       SSE 訂單通知 (可選 Redis)
│   │   ├── auth_throttle.py、customer_session.py、audit.py
│   │   ├── db_migrate.py、bootstrap.py、legacy_upgrade.py、baseline_schema.py   資料庫升級與預設資料
│   │   ├── settings_service.py    系統設定的欄位定義 (後台表單自動產生)、讀寫、連線測試
│   │   ├── approval_service.py    員工權限與主管核准
│   │   ├── payment_service.py     收款紀錄、刷卡 / LINE Pay、作廢與部分退款
│   │   ├── einvoice_ecpay.py、linepay.py、line_service.py、sms_service.py、mail_service.py、delivery_service.py
│   │   │                          外部服務串接 (HTTP 呼叫集中在 _post / _request / _http，測試以假 API 取代)
│   │   ├── wallet_service.py、promotion_service.py、inventory_service.py、shift_service.py
│   │   ├── coupon_service.py      會員券發放 / 撤回、可用券、到期、後台票券視窗
│   │   ├── print_service.py、escpos.py、kitchen_service.py、notify_service.py、reservation_service.py
│   │   ├── report_service.py、i18n_service.py
│   │   ├── db_locks.py            「先檢查再新增」的店家鎖 (同時訂最後座位、同時開班、封存)
│   │   ├── loyalty_service.py、giftcard_service.py、member_qr_service.py
│   │   │                          點數到期 / 集點卡 / 推薦好友、禮物卡、會員動態條碼
│   │   ├── pickup_service.py、feedback_service.py、own_delivery_service.py
│   │   │                          預約取餐時段、餐後評分、自有外送 (區域 / 外送費 / 派車)
│   │   ├── purchasing_service.py、forecast_service.py、live_dashboard_service.py
│   │   │                          供應商與採購單、備料預測、即時看板
│   │   ├── pass_service.py        寄杯 / 多次券 (售出、兌換、取消退回、異動帳重算)
│   │   ├── table_pay_service.py   桌邊線上結帳 (綠界)
│   │   ├── branch_service.py      分店價格 / 上下架、分店名單、叫貨 / 調撥 (出貨扣料、收貨入庫)
│   │   ├── menu_engineering_service.py  菜單工程分析 (人氣 × 毛利四象限)
│   │   ├── marketing_service.py   會員 RFM 分群、久未消費自動發券、排程推播、成效
│   │   ├── deposit_service.py     訂位訂金 (綠界付款 / 退刷 / 不退)、未到停權、訂位提醒
│   │   ├── staff_anomaly_service.py  員工異常偵測 (作廢 / 退款、折扣 / 改價、交班帳差)
│   │   ├── accounting_service.py  會計匯出 (每日傳票、發票明細)
│   │   ├── webhook_service.py     Webhook 事件產生、簽章、重試、防 SSRF
│   │   ├── device_service.py      裝置配對、心跳、遠端鎖定、前端版本
│   │   ├── archive_service.py     舊資料封存 (database/archive)
│   │   ├── pnl_service.py         營業費用、損益表 (含手續費估算、上期比較、6 個月趨勢、Excel)、成本變動對毛利的影響
│   │   ├── history_service.py     原料成本 / 餐點售價變動紀錄 (flush 前比對新舊值，任何途徑修改都會記錄)
│   │   ├── api_docs.py            對外 API 說明文件與 OpenAPI 3.0 規格 (端點清單只在這裡宣告一次)
│   │   ├── group_order_service.py 團體預購 (建立、參加、合併送單、截止自動送單)
│   │   ├── staffing_service.py    排班建議 (每小時需要人力、建議班別、人事成本率)
│   │   ├── demo_service.py        一鍵展示資料 (flask demo seed / remove，只寫入示範店家)
│   │   ├── deferred.py            交易提交後才執行的工作 (開發票、列印、推播)
│   │   ├── security_service.py    TOTP (RFC 6238)、備用碼、登入工作階段
│   │   └── maintenance_service.py、monitor_service.py   背景排程 (多行程時只由「排程主控」執行)、自動備份、
│   │                              健康狀態、錯誤通知、時鐘檢查
│   ├── sync/                  門市 ↔ 線上同步
│   │   ├── registry.py        同步的資料表、方向、本機欄位、自然鍵
│   │   ├── tracking.py        變更序號 (sync_rev) 與刪除紀錄
│   │   ├── engine.py          編碼、讀取變更、合併規則
│   │   ├── hub.py             線上端同步 API (/api/sync/v1)
│   │   ├── node.py            分店背景同步
│   │   ├── nodes.py           分店登記與權杖 (線上端)
│   │   └── runtime.py         目前的同步角色與相關工具
│   └── routes/                Blueprint
│       ├── customer.py        點餐頁、結帳、我的訂單、取餐分店
│       ├── auth.py            註冊 (含個資同意)、人臉 / 手機登入、訪客、登出
│       ├── rewards.py、pos.py、kitchen.py、pickup.py (含 SSE)、orders.py (訂單狀態 API)
│       ├── admin.py           後台、員工帳號、備份還原、匯出匯入
│       ├── payment.py         綠界付款頁、付款通知、付款結果
│       ├── privacy.py         隱私權告知、同意、撤回人臉、後台設定
│       ├── tables.py          桌位管理、列印桌卡、掃碼入口 /t/<代碼> (門市)
│       ├── sync_admin.py      分店管理 (線上端)、同步狀態 (分店)
│       ├── media.py           /media/menu、/media/member (會員照片需員工權限)
│       ├── settings_admin.py  系統設定 (/admin/settings，分店顯示「總部設定 / 本店自訂」)
│       ├── security.py        帳號安全：兩步驟驗證登入第二步、驗證 App 設定、登入裝置管理
│       ├── finance.py         電子發票、作廢與退款、交班與日結
│       ├── printing.py、menu_pro.py、inventory.py、members_pro.py、promotions.py、reports.py、system.py
│       ├── member_auth.py     LINE 登入、簡訊驗證碼、忘記 PIN
│       ├── delivery.py        外送平台 Webhook (/webhooks/ubereats、/webhooks/foodpanda/<店家代碼>)
│       ├── reservations.py    訂位 / 候位
│       ├── timeclock.py       打卡、班表、排班建議 (/admin/staffing)
│       ├── public_extras.py   出餐追蹤頁、PWA (manifest、Service Worker、離線頁)
│       ├── feedback.py、gift_cards.py、purchasing.py、live_dashboard.py、dispatch.py
│       │                      顧客評價、禮物卡、供應商與採購單 / 備料預測、即時看板、外送派車
│       ├── passes.py          寄杯 / 多次券 (後台、POS 售出)
│       ├── table_pay.py       桌邊線上結帳 (帳單、綠界付款頁、付款通知)
│       ├── branches.py        分店價格 / 上下架、分店叫貨 / 調撥
│       ├── marketing.py       會員分群與自動行銷
│       ├── integrations.py    API 與 Webhook、API 說明文件 (/developers)、裝置管理 (含 /device/pair、
│       │                      /api/device/heartbeat)、舊資料封存
│       ├── pnl.py             營業費用、損益表、成本 / 售價歷史
│       ├── group_orders.py    團體預購 (/shop/<店家>/group/...、後台 /admin/group-orders)
│       ├── api_v1.py          對外 API /api/v1 (API 金鑰驗證)
│       └── health.py          /healthz
├── migrations/versions/       0001 舊版基準、0002 重構、0003 同步引擎、0004 線上付款與個資同意、
│                              0005 門市桌位與掃碼點餐、0006 進階營運功能、0007 優惠券期限與批量發放、
│                              0008 優惠券防濫用、0009 總部設定 / 兩步驟驗證 / 登入裝置、
│                              0010 線上付款方式 / 錢櫃 / 餐點規格 / 會員條碼、
│                              0011 預約取餐 / 桌邊呼叫 / 餐後評分 / 熱量標示、
│                              0012 集點卡 / 禮物卡 / 推薦好友、0013 供應商採購 / 批號效期 / 員工時薪、
│                              0014 自有外送、0015 套餐訂單記錄賣出當下的配餐、
│                              0016 套餐任選 / 單品作廢、0017 桌位平面圖 / 桌邊線上結帳 / 寄杯
│                              0018 分店價格 / 上下架、分店叫貨 / 調撥
│                              0019 會員分群與自動行銷、訂位訂金 / 提醒
│                              0020 API 金鑰 / Webhook、裝置管理、封存訂單累積消費
│                              0021 營業費用、原料成本 / 售價歷史、0022 團體預購、
│                              0023 訂單預約取餐時間索引 (負載測試後新增)
├── templates/                 頁面樣板 (全部繼承 base.html)
├── static/
│   ├── css/、js/              各頁面樣式與程式 (theme.css、common.js 為共用；pos.js 為 POS 的進入點 (ES module)，
│   │                          功能拆在 js/pos/ (core / menu / cart / member / totals / checkout / live)；floor-plan.js 桌位平面圖、
│   │                          device.js 已註冊裝置的心跳與鎖定畫面、
│   │                          api-docs.js API 說明文件的線上試打、kiosk-offline.js 點餐機斷線暫存與補送)
│   ├── img/                   預設圖片
│   └── vendor/                Bootstrap、Bootstrap Icons、Cropper.js (離線使用)
├── uploads/                   會員照片 (member/)、餐點照片 (menu/)；不放在 static/，由 /media/ 控管讀取 [不進版控]
├── database/                  menu.db、.secret_key、還原前另存的 menu.db.bak-*、本機模擬用的 caddy-root.crt、
│                              backups/ (每日自動備份)、archive/ (舊資料封存檔，需備份保存)、
│                              system_events.jsonl (系統事件) [不進版控]
├── models/                    人臉辨識模型 (YuNet、SFace)；選用的 Qwen2.5 GGUF [GGUF 不進版控]
├── tests/                     所有測試 (含可直接執行的付款測試腳本)，檔名即測試主題
├── deploy/                    線上端部署：gunicorn.conf.py、entrypoint.sh、Caddyfile、hub.env.example、
│                              pg_backup.sh / pg_restore_check.sh (PostgreSQL 備份)、windows/ (門市開機自動啟動)
├── Dockerfile、docker-compose.yml、.dockerignore
├── .env.example               門市 / 單機設定範例 (複製為 .env)
├── .env、.env.hub             門市設定、線上端設定 [不進版控]
├── .gitignore
├── README.txt、SYNC.txt、DEPLOY.txt、MANUAL_TEST.txt
├── pytest.ini                 測試設定 (預設略過 e2e、stress)
├── requirements.txt
├── test_menu.xlsx             菜單匯入範例 (後台「智慧匯入」可直接使用)
├── serve_windows.py           正式營業啟動 (Waitress)
└── run.py                     開發用啟動


七、測試
--------------------------------------------------------------------
測試一律使用暫存資料庫與暫存資料夾，不讀 .env，不會動到 database/、uploads/ 與 static/。

  pytest                              後端測試 (SQLite)
  set TEST_DATABASE_URL=postgresql://kiosk:kiosk@127.0.0.1/kiosk_test
  pytest                              同一套測試改在 PostgreSQL 上跑 (該資料庫會被清空)
  playwright install chromium         (第一次)
  pytest -m e2e                       瀏覽器端對端測試 (實際操作各頁面；JS 錯誤或違反 CSP 都會失敗)
  pytest -m stress                    真實 HTTP 壓力測試
  set TEST_SYNC_HUB_DATABASE_URL=postgresql://kiosk:kiosk@127.0.0.1/kiosk_sync_test
  pytest tests/test_sync_hub_node.py  同步測試的線上端改用 PostgreSQL
  pytest tests/test_pos_checkout.py   只跑一個主題 (有情境依賴的檔案會自動加入前置檔，開頭會列出)
  python tests/test_payment_ecpay_script.py       線上付款：模擬綠界通知走完 30 項情境 (不需網路)
  GitHub Actions (.github/workflows/tests.yml)：push 到 main 或開 PR 時自動執行 SQLite (Python 3.13 / 3.14)、
                                      PostgreSQL (含同步測試的線上端) 與瀏覽器端對端測試
  python tests/load_gunicorn_pg.py --url ... --password ... [--database-url ...]
                                      多 worker 負載測試：對執行中的 gunicorn + PostgreSQL + Redis 模擬點餐機 / POS / 廚房 /
                                      SSE / 對外 API 的尖峰流量並檢查一致性 (步驟與參考結果見 DEPLOY.txt 一-11)
  python tests/test_payment_ecpay_script.py live  線上付款：到綠界測試環境用測試卡實際付款
                                      (卡號 4311-9522-2222-2222、未來的有效期限、任意安全碼、3D 驗證碼 1234)

測試檔 (tests/，檔名即測試主題；新的測試一律放這裡並照此命名)
  啟動與升級    test_startup_defaults           預設店家 / 負責人 / 加料、SECRET_KEY、預設帳密重設規則
                test_legacy_database_upgrade    舊版資料庫升級、舊上傳資料夾搬移
  權限與安全    test_staff_login_and_roles      員工登入、帳號管理、角色權限矩陣、未登入一律擋下
                test_web_security               CSRF (全路由掃描)、安全標頭、訊息跳脫、錯誤輸入不出 500
                test_rate_limits_and_lockout    登入頻率限制、PIN 鎖定、X-Forwarded-For 信任
                test_tenants_and_isolation      新店家註冊、店家入口、跨店資料隔離
                test_backup_restore             整庫備份 / 還原、惡意備份檔、舊版備份還原
  菜單與會員    test_menu_admin                 菜單、套餐、手動售完
                test_member_accounts            會員註冊、欄位驗證、後台編輯 / 刪除與稽核
                test_member_face_and_photos     人臉特徵與登入、重複註冊、上傳圖片限制、照片權限
  點餐與訂單    test_kiosk_checkout             自助點餐結帳 (計價、加料、冪等、取餐號)
                test_coupons_and_rewards        優惠券、紅利商品與兌換憑證、紅利設定
                test_pos_checkout               POS 櫃檯結帳
                test_pos_today_orders_api       POS 當日訂單輕量 API
                test_order_status_and_cancel    訂單狀態流轉、取消回補、換日自動結案
                test_concurrency_stock_points   併發：不超賣、點數不雙花、同時結帳與取消
                test_stock_race_all_channels    各通路同時搶同一份庫存 (點餐機、POS、桌邊、Uber Eats / foodpanda、手動外送單、
                                                外送到府、線上端 ↔ 分店)、平台訂單同時重送只建立一張、分店回報庫存不吃掉線上預扣
                test_coupon_points_race_all_channels  各通路同時用同一張券 / 同一位會員的點數 (點餐機、桌邊、POS、
                                                紅利商城、線上端 ↔ 分店)：只折抵一次、搶輸明確失敗、跨店重複使用通知
                test_combo_and_shared_resource_races  套餐配餐與單點共用庫存、取消回補賣出當下的配餐、配餐停售、
                                                同一會員同時下單 (集點卡 / 推薦獎勵只發一次)、同時訂最後座位、
                                                兩台 POS 同時開班、關班時結帳、跨店禮物卡 / 儲值金同時用光通知
                test_price_gift_and_crossnode_loyalty  改價與送出之間的價差 (畫面金額不同時要求確認)、
                                                贈品最後一份被搶走仍可結帳、跨店重複推薦獎勵沖銷、
                                                跨店同時集滿集點卡通知、點數到期與分店同時使用的校正
                test_live_updates_sse           SSE 即時更新、連線配額、看板遮蔽姓名
  報表          test_reports_and_excel          營運分析、Excel 匯出 / 匯入、儀表板與稽核
  部署與前端    test_deploy_config              /healthz、反向代理標頭、部署設定檔
                test_frontend_static_files      前端靜態檢查 (CSP、外部網址、XSS、POS 模組的 import / export 對得上)
  同步          test_sync_hub_node              一個線上端 + 兩家分店的完整同步情境
                test_sync_admin_pages           分店管理、同步狀態、線上點餐選分店
                test_sync_security              分店權杖外流時的限制 (負責人帳號、路徑、跨店、上傳上限)
                test_sync_invariants            全部測試跑完後檢查同步不變量
  付款與個資    test_payment_ecpay              綠界檢查碼、付款通知、逾時取消
                test_payment_ecpay_script       付款測試腳本 (pytest 跑模擬模式，也可直接執行)
                test_privacy_pdpa               隱私權告知、同意紀錄、撤回人臉、清除久未使用會員
  桌位點餐      test_table_qr_ordering          桌位管理、掃碼點餐、失效 QR code、POS 桌位結帳
                test_table_concurrency          同時操作的衝突 (多台 POS 結同一桌、結帳與取消、送單與刪桌、同名桌位、
                                                換 QR code、線上端取消與分店結帳)；輪數 TABLE_CONFLICT_ROUNDS
  進階功能      test_settings_admin             後台系統設定 (權限、金鑰遮罩、表單驗證、連線測試)
                test_pricing_promotions         通路價、供應時段、改價 / 整單折扣核准、服務費、會員等級、促銷活動
                test_payments_refunds_wallet    刷卡授權碼、付款方式設定、LINE Pay 扣款 / 退款 (假 API)、部分退款、作廢、儲值金
                test_einvoice_ecpay             電子發票 (假綠界 API)：加解密、載具 / 統編、開立 / 重送 / 作廢 / 折讓、後台
                test_shifts_and_table_split     交班 / 帳差、必須開班、換桌、分開結帳、多種付款、LINE Pay 失敗回沖、員工權限
                test_kitchen_printing_inventory 工作站 KDS、加急、熱感印表機出單 (假 TCP)、原物料扣料 / 回補 / 盤點
                test_members_line_sms           會員等級、生日禮、儲值金調整、LINE 登入 / 推播 (假 API)、簡訊驗證
                test_delivery_and_reservations  Uber Eats / foodpanda Webhook、線上訂位名額、候位與叫號看板
                test_ops_reports_timeclock_pwa  進階報表 / Excel / 定期寄送、每日排程、系統監控與備份、打卡、追蹤頁、PWA、多語系
                test_hq_settings_and_staff_security  總部設定下發 / 覆寫 / 分店不可上傳、會員分頁、兩步驟驗證、登入裝置管理、閒置登出
                test_online_payments_and_pos_hardware  綠界付款方式 / ATM / 超商代碼繳費期限、自動退刷 (假 API)、
                                                錢櫃 (假 TCP)、客顯頁、會員動態條碼、餐點規格加價
                test_pickup_calls_feedback_display  預約取餐時段 / 驗證 / 分店廚房看板與換日、桌邊呼叫、待機輪播、
                                                大字按鈕、熱量營養、餐後評分 (Google 連結、低分通知、上傳線上端)
                test_loyalty_giftcards_member_data  點數到期 (先進先出) 與提醒、集點卡、推薦好友、禮物卡 (售出 / 扣款 /
                                                退款 / 作廢 / 跨店同步)、會員下載資料與刪除帳號
                test_purchasing_lots_forecast_labor_live  建議採購 / 送出 / 收貨、批號先到期先出與提醒 / 報廢、
                                                備料預測、人事成本率、即時看板 (含線上端分店彙整)
                test_home_delivery              外送到府的區域 / 外送費 / 驗證、派車流程與追蹤、線上端與分店同步配送狀態
                test_floor_plan_table_pay_passes  平面圖擺放與 POS 狀態、桌邊線上結帳 (綠界通知 / 重複通知 / 衝突)、
                                                寄杯 (售出 / 各通路兌換 / 同時兌換 / 取消退回 / 跨店同步)
                test_accounting_api_devices_archive  傳票借貸平衡 / 科目 / 稅額、發票明細、API 金鑰權限 / 跨店隔離 / 分頁 / 異動追蹤、
                                                Webhook 事件 (含 LINE Pay 扣款中) / 簽章 / 重試 / 內網阻擋 / 固定 IP 連線 / 多行程只送一次、
                                                裝置配對 / 心跳 / 遠端鎖定、封存 (uuid 主鍵) 與查詢、排程主控、匯出防公式注入、遷移代號長度
                test_marketing_deposits_anomalies  RFM 分群、久未消費發券 / 冷卻 / 拒絕行銷 / 成效、排程推播、訂金付款 (每次換交易編號、
                                                重複付款通知) / 退刷 / 不退 / 逾時 / 同時取消與入座、未到停權、訂位提醒、員工異常標示與通知、
                                                交班帳差通知
                test_branch_menu_engineering_transfers  分店價格 / 下架 (只到該分店、各通路、線上取餐分店、促銷試算、對外 API、
                                                特價不可變 0 元)、菜單工程四象限、叫貨 / 調撥 (出貨 / 收貨差異 / 同時出貨 / 偽造狀態 /
                                                取消與出貨同時發生雙方通知)、線上端不收訂位
                test_expenses_pnl_cost_history  費用新增 / 修改 / 刪除 / 驗證 / 跨店、固定費用複製 (月底日期)、損益表數字 (稅額 / 毛利 /
                                                手續費依付款方式 / 費率設定 / 人事 / 費用)、上期與 6 個月趨勢、Excel (防公式)、
                                                成本歷史 (進貨加權平均 / 改價 / 未載入舊值 / 不變不記)、成本變動對毛利、分店費用上傳線上端
                                                與依分店篩選 / 線上端不可改、API 文件與 OpenAPI 涵蓋每個 /api/v1 路由
                test_group_orders_staffing_offline  團體預購 (建立驗證 / 參加 / 規格 / 人數與份數上限 / 個人修改連結 / 管理連結 /
                                                合併送單 / 截止自動送單 / 失敗通知與重送 / 手動團購逾時取消 / 同時送單只建一張 /
                                                後台代建)、排班建議 (人力計算 / 建議班別 / 加入班表 / 人事成本率)、
                                                斷線暫存補送 (訪客 / 現金 / 去重 / 店家不符 / 逾時 / 紅利拒收)
                test_order_service_modules      order_service 拆分後對外名稱不變、模組大小與依賴方向
                test_demo_seed                  示範店家：各報表 (營運 / 損益 / 傳票 / 菜單工程 / 員工異常 / 排班 / RFM / 成本歷史) 有內容、
                                                後台頁面可開、其他店家資料不變、--reset 重建、remove、拒絕覆蓋真實店家 / 分店主機 / 線上端 / 帳號衝突、建到一半失敗會整個刪除
                test_combo_choices_split_payment_void  套餐任選 (加價 / 驗證 / 庫存 / 同步)、混合付款 (收款 / 退回)、
                                                單品作廢 (比例退款 / 未結帳改單 / 同時作廢 / 取消不重複回補 / 同步)
                test_coupon_security            猜碼鎖定、公開代碼限會員 / 每人次數、最高折抵、代碼格式、跨店隔離、商城剩餘張數
                test_coupon_issue_expiry        會員專屬券、批量發放 (對象 / 張數 / 每人上限 / 到期 / LINE 通知 / 撤回)、過期與活動期間、
                                                後台票券視窗 (未使用 / 近 7 天已使用)
                test_sync_pro_tables            促銷 / 等級 / 翻譯下發、兩店離線使用儲值金合併、收款與退款上傳、發放的會員券同步
  瀏覽器        test_e2e_*                      端對端 (後台、顧客點餐、廚房取餐、會員、POS、所有頁面、進階功能、
                                                改價後購物車更新 test_e2e_price_change、套餐任選 / 混合付款 / 作廢品項 / 平面圖 / 桌邊結帳 / 寄杯 / 菜單工程 / 自動行銷 / 裝置鎖定
                                                test_e2e_round3、費用登錄 → 損益表 / API 文件線上試打 / 斷線暫存補送 / 團體預購
                                                test_e2e_round4)
  壓力          test_stress_http                真實 HTTP 壓力測試
                load_gunicorn_pg                (不是 pytest，直接執行) gunicorn 多 worker + PostgreSQL + Redis 負載測試與一致性檢查

測試共用同一個暫存資料庫，有先後依賴 (例如 POS 測試使用菜單測試建立的餐點)：
執行順序寫在 tests/conftest.py 的 RUN_ORDER，前置依賴寫在 NEEDS (新增測試檔時一併維護)。


八、開發慣例
--------------------------------------------------------------------
- 換行格式：專案文字檔統一使用 Windows 換行 (CRLF)。
- 前端 (CSP 強制，tests/test_frontend_static_files.py 自動檢查)：
  - 不寫行內 <script> 或 onclick；事件用 data-action="名稱" 搭配 Kiosk.on(root, 'click', {名稱: fn})。
  - 頁面資料以 <script type="application/json" id="page-data"> 嵌入，用 Kiosk.pageData() 讀取。
  - 不引用外部網址，第三方套件放 static/vendor/。
  - 動態 HTML 一律用 Kiosk.html`...` (自動跳脫) 或 textContent。
  - API 用 Kiosk.api(url, {json})：自動帶 CSRF Token，失敗時丟出含後端訊息的錯誤。
  - 刪除確認用 <form data-confirm="…">；圖片備援用 <img data-fallback="預設圖網址">。
  - POS 前端是 ES module (templates/pos.html 以 type="module" 載入 js/pos.js)：新增功能放進 js/pos/ 對應的模組並 export，
    其他模組以 import 使用；共用狀態只放 core.js 的 state (跨模組不可重新指定 let 變數)；每個檔案 500 行以內 (有測試檢查)。
- 後端：
  - 錯誤一律 raise AppError(訊息, 狀態碼)：API 回 {"success": false, "message": …}；表單的 400/409 flash 後導回。
  - 權限用 @role_required('owner', 'manager')；整庫備份等用 @platform_admin_required。
  - 查詢自動加上目前店家條件；系統工作需要跨店時用 with tenant_bypass():。
  - 結帳一律走 order_service.process_checkout，訂單狀態一律走 order_service.change_order_status
    (實作分在 order_pricing / order_stock / order_checkout / order_lifecycle，下層不可匯入上層，有測試檢查)。
  - 加減會員點數一律用 points_service.change_points (寫入點數異動帳)。
  - 修改資料表：改 app/models/ → flask --app run db migrate → 檢查遷移檔 → 測試。
  - 新資料表要同步：繼承 TenantScoped 並加入 app/sync/registry.py 的 SPECS (依外鍵順序)；本機欄位放 local_only。
  - 遷移代號 (revision) 不可超過 32 個字 (PostgreSQL 的 alembic_version 欄位長度，有測試檢查)。
  - 背景工作 / 會被同時觸發的動作先以條件式 UPDATE「搶」再處理 (例如 status pending → sending)，多個行程才不會重複執行。
  - Excel / CSV 匯出含使用者輸入的文字時，用 app/utils.py 的 neutralize_formulas / csv_safe (防公式注入)。


九、權限與安全
--------------------------------------------------------------------
1. 角色：
   - owner (負責人)：全部權限，含員工帳號。預設店家 (default) 的 owner 為平台管理者，才可整庫備份 / 還原。
   - manager (店長)：後台、菜單 / 優惠券 / 紅利、隱私權告知設定、匯出匯入、POS、出餐、核銷、取消訂單。
   - staff (櫃台)：POS、會員設定 PIN、取餐核銷、當日訂單、出餐、取消未取餐的訂單。登入後直接進 POS (看不到後台)。
   - kitchen (廚房)：KDS，只能出餐。登入後直接進 KDS，看不到會員照片。
2. CSRF：所有表單與 API 的 POST 都需要 CSRF Token；例外只有以權杖驗證的同步 API、以 API 金鑰驗證的 /api/v1、
   以檢查碼驗證的綠界通知 (線上付款、桌邊線上結帳、訂位訂金) 與以簽章驗證的外送平台 Webhook。
   登出、訪客登入只接受 POST (其他網站無法用連結或圖片讓人登出)。
3. 會員照片：只能經 /media/member/ 讀取，需登入同店員工 (廚房除外)；/static/member 一律 404。
   上傳檔路徑一律檢查格式並限制在上傳資料夾內 (app/services/upload_paths.py)，/media/menu 只提供圖片。
4. 頻率限制：登入、註冊、優惠碼、送單有 IP 頻率限制；員工登入與註冊新店家只以 IP 計數 (切換店家繞不過)。
   反向代理之後請設 TRUSTED_PROXY_COUNT=1。
5. 鎖定：
   - 會員 PIN：同裝置對同手機連錯 5 次鎖 15 分鐘；同手機累計錯 10 次鎖定 (每次加倍，上限 24 小時)；同 IP 累計錯 30 次鎖定。
   - 員工密碼：同 IP 對同帳號連錯 5 次、同帳號累計錯 10 次 → 鎖 15 分鐘。
   - 帳號 / 手機不存在時同樣計數且回應時間相同，無法藉此列舉帳號。
   - 新設定的 PIN 不可太簡單 (0000、1234、1212、手機末幾碼)。
6. 工作階段：登入時換新的工作階段；員工改密碼或角色後，舊的登入自動失效。
7. 安全標頭：CSP (script-src 'self')、X-Content-Type-Options、X-Frame-Options、Referrer-Policy、
   Permissions-Policy；HTTPS 時加 HSTS。付款頁只放寬 form-action 到綠界網域。
8. 個資：註冊須同意隱私權告知並記錄版本與時間；人臉另行同意、可撤回 (各分店的照片經同步一併刪除)；
   停用會員可定期刪除 (訂單去識別化保留)。
9. 同步：HTTPS + 分店權杖 (只存雜湊)。分店只能修改自己負責的訂單、不能建立 / 修改 / 刪除負責人帳號、
   不能讀寫其他店家的圖片或資料；單次上傳有筆數上限 (詳見 SYNC.txt)。
10. 線上付款：已付款的訂單被取消 (店員、分店或逾時後才付款) 一律標記「需退款」，由店家到綠界後台退刷。
11. 整庫備份 / 還原：只限平台管理者；大檔案上限只對已登入的平台管理者放寬；含觸發程序或檢視表的備份檔拒絕還原。
12. 匯入：Excel 解壓後超過 30MB 或超過 2 萬列拒絕 (防壓縮炸彈)；匯出時公式一律轉成文字。
13. 開發模式：FLASK_DEBUG=1 只監聽本機 (除錯頁可執行程式碼)。
14. 對外 API：金鑰只顯示一次 (資料庫存雜湊、不寫入 Cookie)，依權限範圍開放、只能讀寫自己店家的資料、每把每分鐘 120 次。
15. Webhook：只送 https 公開網際網路位址 (解析後每個 IP 都檢查，連線固定用檢查過的 IP，防 DNS 換址與內網存取)，
    每次送出附 HMAC-SHA256 簽章與時間戳；接收端應驗證簽章並以 X-Kiosk-Delivery 去除重複。
16. 裝置：裝置權杖存在 HttpOnly Cookie (資料庫只存雜湊)；遠端鎖定的裝置送出的操作一律回 423。
17. 匯出：Excel / CSV 中以 = + - @ 開頭的文字一律當成文字 (會員姓名等不會被 Excel 當成公式執行)。

十、API 摘要
--------------------------------------------------------------------
訂單狀態  POST /api/orders/<id>/status   {"status": "Completed" | "PickedUp" | "Cancelled"}
  流轉：Pending → Completed → PickedUp；非終態都可轉為 Cancelled (終態)。
  權限：             owner  manager  staff  kitchen
    Pending→Completed  O      O       O      O
    Pending→Cancelled  O      O       O      X
    Completed→PickedUp O      O       O      X
    Completed→Cancelled O     O       O      X
    PickedUp→Cancelled O      O       X      X
  回應：200 成功、400 狀態值錯誤、401 未登入、403 權限不足 (先於訂單是否存在判斷)、404 查無訂單、409 非法轉換或已被變更。
  取消會在同一交易內回補庫存、退還點數、還原會員券、扣回贈點，只執行一次。

結帳  POST /submit_order (點餐頁)、POST /api/pos/checkout (POS)
  {"client_order_id": "<UUID，重送沿用>",
   "items": [{"menu_item_id": 1, "quantity": 2, "modifier_ids": [3], "combo_option_id": null,
              "is_add_on": false, "is_reward": false, "customization": "備註"}],
   "payment_method": "Cash", "order_type": "內用", "promo_code": "", "use_points": 0, "note": ""}
  (POS 另有 user_id、cash_received、table_number)
  價格一律由後端計算；replayed: true 表示是重送的同一張訂單；線上端另回傳 payment_url (前往付款)。

其他
  GET  /api/pos/today_orders       POS 當日訂單 (含未結帳桌位)   GET /api/kitchen_orders、/api/pickup_orders、/api/counter_orders
  POST /api/pos/tables/<id>/settle  桌位結帳 {"payment_method": "Cash", "cash_received": 600}
  GET  /api/orders_stream          SSE 訂單通知          GET /healthz   健康檢查 (資料庫連線)
  /pay/<訂單>、/payment/ecpay/notify、/payment/ecpay/result   綠界付款 (線上端)
  /api/sync/v1/hello、push、pull、media/menu/<路徑>          同步 API (分店權杖，詳見 SYNC.txt)

進階功能 (JSON；需主管核准的操作回應 403 + need_approval，帶 approver_username / approver_password 重送)
  POST /api/pos/checkout 另接受 payment_info {auth_code, card_last4, one_time_key}、invoice {type, carrier, love_code,
       buyer_id, buyer_name, email}、order_discount {type: amount|percent, value}、items[].override_price、is_rush
  POST /api/pos/orders/<id>/refund {amount, reason}   部分退款     POST /api/pos/orders/<id>/reprint、/rush
  GET  /api/pos/shift、POST /api/pos/shift/open | movement | close   交班
  POST /api/pos/wallet/topup {user_id, amount, payment_method}       儲值金加值
  POST /api/pos/delivery_order {items, platform, external_ref}       外送單手動登錄
  POST /api/pos/tables/<id>/settle 另接受 order_ids (分開結帳)、payments [{method, amount, ...}] (多種付款)
  POST /api/pos/tables/<id>/move {to_table_id, order_ids}           換桌 / 併桌
  POST /api/kitchen/orders/<id>/station_done {station_id}           工作站完成
  POST /api/promotions/preview                                      促銷試算 (點餐頁 / POS 即時顯示)
  POST /webhooks/ubereats (X-Uber-Signature)、/webhooks/foodpanda/<店家代碼> (Bearer 權杖)   外送平台
  GET  /track/<訂單 uuid>、/api/track/<訂單 uuid>                      出餐追蹤 (公開，uuid 無法猜測)

第三輪功能
  POST /api/pos/orders/<id>/void_item {item_id, quantity, reason, method}   單品作廢 (需作廢權限 / 主管核准)
  結帳 items[] 另接受 combo_choices [任選配餐 id]、pass_id (寄杯兌換)；POS payment_method「混合付款」+ payment_info.tenders
  POST /api/pos/passes/sell {user_id, product_id, payment_method}    POS 售出寄杯
  GET  /api/pos/floor                                                POS 桌位平面圖狀態
  GET  /api/table/bill、POST /api/table/pay                           桌邊線上結帳 (掃碼點餐的手機)
  /payment/ecpay/table_notify | table_result、/payment/ecpay/reservation_notify | reservation_result   綠界通知 (門市)
  /reserve/<代碼>/pay                                                 訂位訂金付款頁 (每次開啟換新的交易編號)
  POST /api/member/marketing {opt_in}                                 會員開啟 / 關閉優惠活動訊息
  /device/pair、POST /api/device/heartbeat {version, path}            裝置配對、心跳 (回傳 locked / reload)
  /api/v1/... (Authorization: Bearer kk_...)                          對外 API：
      GET orders?since=&after_id=&limit=  (依建立時間；next 為下一頁參數)
      GET orders?cursor=0                 (異動追蹤：新增與狀態 / 付款改變，回傳 next_cursor；建議在線上端呼叫)
      GET orders/<uuid>、GET menu、POST menu/<id>/availability {sold_out, stock}、GET members/<手機>、GET reports/daily?date=
      完整說明 (參數、回應欄位、錯誤碼、Webhook 簽章範例、線上試打)：/developers；OpenAPI 3.0：/developers/openapi.json

第四輪功能
  /admin/expenses?month=YYYY-MM     營業費用 (POST action=add | edit | delete | copy_fixed)
  /admin/pnl?month=YYYY-MM | start=&end= [&node=分店 uuid] [&format=xlsx]   損益表
  /admin/cost-history?days=30&kind=ingredient|menu                         原料成本 / 售價歷史
  /shop/<店家>/group/new、/shop/<店家>/group/<token> (參加)、…/manage (主揪)、…/print (分裝清單)   團體預購
  /admin/group-orders (店員以上)、/admin/staffing?date=YYYY-MM-DD (店長以上)                      團體預購列表、排班建議
  POST /api/kiosk/offline_order {shop, code, queued_at, order: {client_order_id, items, order_type, note, invoice}}
       點餐機斷線暫存單補送 (一律訪客 / 現金；重送回傳原訂單 replayed: true；店家不符 409、超過 3 小時 410)


十一、從舊版升級
--------------------------------------------------------------------
1. 先備份 database/menu.db 與舊的 static/member、static/menu。
2. pip uninstall -y opencv-python 後 pip install -r requirements.txt (新增 Flask-Migrate、Flask-WTF、Flask-Login、
   Flask-Limiter、psycopg、opencv-python-headless、segno (桌位 QR code) 等)。
3. 第一次啟動自動完成：升級資料庫 (0001 → 0005)、舊上傳檔搬到 uploads/、會員現有點數記為「期初」點數異動。
4. 舊檔案已不使用：app/routes/common.py、app/services/idempotency.py、app/services/migration_service.py、test_all_features.py。
5. 環境變數 DISABLE_RATE_LIMIT 改為 RATELIMIT_ENABLED (0 = 停用)；門市不需要 Redis (只有線上端多行程部署使用)。
6. 升級前註冊的會員沒有個資同意紀錄：登入後點餐頁上方會請他閱讀並同意；原有人臉資料仍可使用，可在 /privacy 刪除。
7. 2026-10 進階營運功能：pip install -r requirements.txt (新增 cryptography，電子發票加密用)；
   啟動時自動升級資料庫到 0006。所有新功能預設關閉或不影響原流程，到後台「系統設定」逐項啟用並填寫金鑰；
   隱私權告知版本更新為 2026-10-23 (新增 LINE 綁定、生日、儲值金、電子發票載具等蒐集項目)，會員下次登入會再請他同意。
