# 智慧外送儲物櫃系統：後端 Lambda 職責與 API 規範說明書

本文件定義了系統核心 Lambda 函數的職責、對應 API 及其邏輯運作流程。所有請求皆須透過 **Amazon API Gateway** 並經由 **AWS Cognito** 進行身份驗證。後端統一由 `event.requestContext.authorizer.claims.sub` 提取安全的使用者 ID (`Uid`)。

---
## 核心服務

### 1. Locker_Query_Handler (查詢服務)

- **負責的 API**
  - **API: `GET {apiUrl}/lockers`**
    - 描述：取得當前使用者所預約或佔用的所有櫃位資料清單。
  - **API: `GET {apiUrl}/lockers/{Location}`**
    - 描述：取得特定區域的所有櫃位狀態矩陣。

- **功能**
  - **個人資料過濾**：從 Cognito Token 解析出 `Uid`，並在 DynamoDB `Lockers` 資料表執行 Query 操作，篩選出與該 `Uid` 匹配的紀錄。
  - **特定區域查詢**：捨棄消耗資源的 Scan 操作，改用 Query 操作。將路徑參數 {Location} 作為 Partition Key，大幅降低 I/O 成本與回應延遲。
  - **資料格式轉換 (Grid Mapping)**：針對區域查詢，將資料庫中的扁平列表資料轉換為前端 Vue 渲染所需的二維陣列（Grid Structure），並確保 `Status` 映射為 `Occupy`, `Available`, 或 `Error`。

- **回傳**
  - **成功 (200 OK)**：回傳包含 `lockers` 物件陣列（個人查詢）或 `Grid` 二維陣列（區域查詢）之 JSON 格式。
  - **失敗**：回傳 `404 Not Found` (無此區域) 或 `500 Internal Server Error`。

---

### 2. Locker_Booking_Handler (預約服務)

- **負責的 API**
  - **API: `POST {apiUrl}/lockers/{Location}/{Number}/start-booking`**
    - 描述：使用者點擊可用櫃位後發起，將資料庫狀態由 Available 轉為 SoftLocked（暫鎖狀態），並啟動 Saga 事務追蹤。
  - **API: `POST {apiUrl}/lockers/{Location}/{Number}/exec-booking`**
    - 描述：使用者完成最終確認後發起，帶回事務憑證（executionArn），告知系統該筆預約正式生效。

- **功能**
  - **原子性狀態寫入**：在 `start-booking` 階段使用 DynamoDB 的 `ConditionExpression`，確保只有在櫃位狀態為 `Available` 時才能成功更新為 `SoftLocked`，徹底防止 Race Condition。
  - **憑證換取邏輯**：在 `exec-booking` 階段，Lambda 會查詢 `LockerTransactions` 資料表，根據 `executionArn` 提取由狀態機產生的 `taskToken`，實現非同步工作流的同步確認。
  - **分散式交易管理**：啟動 **AWS Step Functions** 工作流。若 5 分鐘內未收到 `exec-booking` 請求，狀態機將自動觸發 `RollbackAvailable` Lambda，將資料庫狀態重置回 `Available` 以供他人使用。

- **回傳**
  - **start-booking**：`200 OK` 並回傳 `executionArn` 供前端後續追蹤，及告知櫃位暫時保留 5 分鐘。
  - **exec-booking**：`200 OK` 訊息 `Transaction completed`。

---

### 3. Locker_Control_Handler (控制服務)

- **負責的 API**
  - **API: `POST {apiUrl}/lockers/{Location}/{Number}/unlock`**
    - 描述：當使用者在 Vue 前端點擊開鎖按鈕時觸發，必須通過 Cognito 授權器，前端不傳送 Uid，由後端從 Token 中提取。

- **功能**
  - **擁有權二次校驗**：從資料庫中檢查該櫃位的 `Uid` 是否與請求者的 Cognito `sub` 完全一致，確保使用者無法開啟非自己預約的櫃位。
  - **物聯網設備影子同步**：透過 **AWS IoT Core** SDK 更新指定儲物櫃的 **Device Shadow** 為期望狀態 (`desired: {"open": true}`)。樹莓派偵測到 Shadow 更新後，驅動 GPIO 繼電器開啟實體電控鎖。

- **回傳**
  - **成功 (200 OK)**：`{"success": true, "message": "Locker [ID] has been unlocked"}`。
  - **失敗 (403 Forbidden)**：當使用者嘗試開啟非本人佔用的櫃位時回傳。

---

### 4. RollbackAvailable (事務補償服務)

- **觸發來源**
  - **觸發者：AWS Step Functions**
    - 描述：當預約工作流超過 5 分鐘未收到確認，或流程中發生非預期錯誤時，由狀態機自動觸發執行。

- **核心功能**
  - **原子性狀態重置**：利用 DynamoDB 的條件寫入機制，確保僅在櫃位狀態仍處於 `SoftLocked`（暫鎖）時執行重置，這能有效避免覆蓋掉在超時邊緣剛好成功的合法預約（已轉為 `Reserved`）。
  - **資源與事務同步初始化**：
    1. **儲物櫃表 (Lockers)**：將狀態恢復為 `Available` 並移除 `Uid` 欄位，釋放實體櫃位供後續搜尋。
    2. **事務表 (LockerTransactions)**：將對應的 `ExecutionArn` 紀錄標記為 `TIMEOUT` 或 `ROLLED_BACK`，建立完整的審計追蹤路徑。
  - **系統級權限控制**：由雲端狀態機內部調用，不涉及前端傳入的 Token 解析，確保補償流程在不受外部干擾的情況下安全執行。

- **執行結果**
  - **成功 (Success)**：完成資源釋放與紀錄更新，狀態回傳至 Step Functions 並記錄於執行日誌中。
  - **忽略 (Skipped)**：若偵測到櫃位狀態已非 `Reserved`，則不執行寫入動作，維護資料的一致性並結束任務。

---

### 5. Register_Task_Token_Handler (憑證註冊服務)

- **觸發來源**
  - **觸發者：AWS Step Functions**
    - 描述：當狀態機進入 `.waitForTaskToken` 任務節點時自動觸發，此 Lambda 扮演狀態機與資料庫之間的通訊橋樑。

- **功能**
  - **非同步憑證持久化**：接收由狀態機產生的 `taskToken`，並將其與當前的 `executionArn` 綁定存入 `LockerTransactions` 資料表。
  - **事務狀態初始化**：在資料表中建立一筆初始狀態為 `PENDING` 的紀錄，並記錄櫃位的 `Location` 與 `Number`，以便後續 API 檢索。
  - **解耦 API 與工作流**：讓前端 API 能在使用者點擊確認時，隨時找到正確的工作流憑證。

- **執行結果**
  - **成功 (Success)**：回傳註冊成功的狀態，狀態機隨即進入暫停等待模式，直到收到回傳指令或達到 5 分鐘超時。
  - **失敗**：拋出例外並觸發狀態機的重試機制，確保憑證註冊流程的可靠性。
---

### 6. Locker_Sync_Shadow_Handler (影子同步服務)

- **觸發來源**
  - **觸發者：AWS IoT Rule (物聯網規則引擎)**
    - 描述：非由 API 直接呼叫。當樹莓派上的 Device Shadow 偵測到門關閉事件時，透過 IoT Rule 轉發 MQTT 訊息觸發 Lambda 執行。

- **功能**
  - **硬體狀態同步**：根據 Device Shadow 的 `reported` 狀態（`door_sensor: "closed"`），自動更新 DynamoDB 中對應櫃位的業務狀態。
  - **業務邏輯判斷**：依據當前資料庫狀態（Reserved/Occupied）決定同步行為：
    - Reserved → Occupied：放置完成
    - Occupied → Available：取貨完成，釋放櫃位供他人使用。
  - **條件寫入保護**：使用 DynamoDB 條件更新，避免並發事件導致狀態不一致。

- **執行結果**
  - **成功 (200 OK)**：回傳同步後的櫃位狀態及硬體資訊。
  - **忽略 (200 OK)**：若狀態不符合同步條件，則記錄警告但不執行更新。

---
## 共用模組
將共用的 .py 檔案放在名為 python/ 的資料夾內，然後將其壓縮，打包成 Layer 後掛到每個 Lambda 上使用

### 1. Cognito UID Parsing (`auth.py`)
- **輸入**
  - AWS Lambda `event` 物件（包含 `requestContext.authorizer.claims`）

- **輸出**
  - `uid: str`（Cognito 使用者唯一識別碼）

- **功能**
  - 從 API Gateway + Cognito 授權資訊中解析出使用者的 `sub`（UID）
  - 封裝 IAM / Cognito claims 存取邏輯，避免各 Lambda 重複解析 request context
  - 提供一致的身份識別來源，確保後續 DB 操作與權限驗證基於同一 UID

- **使用情境**
  - 查詢使用者自己的櫃位（Locker_Query）
  - 預約櫃位時寫入 uid（Locker_Booking）
  - 開鎖前驗證櫃位擁有者（Locker_Control）

---

### 2. DynamoDB Access Layer (`locker_repository.py`)
- **功能**
  - 封裝所有與 DynamoDB `Lockers` table 的 CRUD 操作
  - 提供一致的資料存取介面（Query / Get / Update / Conditional Update）
  - 支援：
    - 查詢特定區域或使用者的 lockers
    - 更新 locker 狀態（Available / Occupied / Reserved）
    - 原子性更新（ConditionExpression，避免 race condition）
  - 隔離 AWS SDK（boto3）細節，使 Lambda handler 保持簡潔

- **使用情境**
  - 查詢所有櫃位狀態（Locker_Query）
  - start-booking 時鎖定櫃位（Locker_Booking）
  - exec-booking / rollback 狀態更新
  - unlock 前確認 ownership（Locker_Control）

---

### 3. Response Formatting (`response.py`)
- **輸入**
  - `data: dict | list | str`
  - `status_code: int`
  - `message: str（可選）`

- **輸出**
  - API Gateway 標準 HTTP Response 格式：
    ```json
    {
      "statusCode": 200,
      "body": "{...json string...}"
    }
    ```

- **功能**
  - 統一所有 Lambda 的回傳格式
  - 自動將 Python object 轉換為 JSON string
  - 標準化 success / error response 結構
  - 提升 API 回傳一致性

- **使用情境**
  - 查詢 API 回傳 locker grid（Locker_Query）
  - 預約成功回傳 executionArn（Locker_Booking）
  - 開鎖成功 / 權限錯誤回傳（Locker_Control）

---

### 4. Ownership Validation (`validator.py`)
- **輸入**
  - `locker: dict`（從 DynamoDB 取得的單一櫃位資料）
  - `uid: str`（當前使用者 Cognito UID）

- **輸出**
  - `bool`（驗證結果，或直接 raise exception）

- **功能**
  - 提供統一的權限檢查邏輯
  - 驗證當前使用者是否為指定櫃位的合法擁有者
  - 比對 `locker["uid"]` 與 `uid` 是否一致
  - 可選擇直接拋出 `PermissionError` / `ForbiddenException` 以中斷流程
  - 確保敏感操作（如開鎖、取消預約）僅限授權使用者執行

- **使用情境**
  - 開鎖前驗證使用者是否為該櫃位擁有者（Locker_Control）
  - 進行取消預約或狀態修改前的權限檢查（Locker_Booking rollback / cancel）

---

## 資料表
### 1. Lockers 資料表
| 欄位名稱 (Attribute) | 型別 (Type) | 說明與用途 |
|----------------------|------------|------------|
| Location             | String (PK) | 代表儲物櫃所在的地理位置，這能讓前端 Vue 透過一次 Query 取得該區域的所有櫃位。 |
| Number               | Number (SK) | 儲物櫃的實體編號，與 Location 組合後形成唯一的資源識別碼。 |
| Status               | String | 當前狀態：Available（可用）、Reserved（暫鎖）、Occupied（佔用）、Error（故障）。 |
| Uid                  | String | 當前持有者的 Cognito User ID（sub）。若狀態為 Available，此欄位通常不存在或為空。 |
| UpdateDate           | String | 最後一次狀態變更的 ISO 8601 時間戳記。 |

### 2. LockerTransactions 資料表

| 欄位名稱 (Attribute) | 型別 (Type) | 說明與用途 |
|----------------------|------------|------------|
| ExecutionArn         | String (PK) | Step Functions 的執行實例 ID，確保每筆交易唯一。 |
| TaskToken            | String | 由狀態機產生的非同步回傳憑證，用於 `send_task_success` 喚醒工作流。 |
| Location             | String | 目標儲物櫃所在區域。 |
| Number               | Number | 目標儲物櫃的實體編號。 |
| Uid                  | String | 發起預約者的 Cognito User ID，用於後續安全驗證。 |
| Status               | String | 追蹤交易生命週期。建議值：PENDING, CONFIRMED, TIMEOUT。|
| CreatedAt            | String | 交易發起時間，用於前端排序與除錯。 |
| UpdatedAt            | String | 狀態最後變更時間。 |
| ExpireAt             | Number | TTL 欄位，Unix Timestamp 格式。 |

---
## 環境變數設定

1. LOCKER_TABLE 設定
    - 選擇 Locker_Query_Handler 
    - 選擇 Configuration
    - 選擇 Environment variables，設定 Key, Value
2. iot-data 客戶端需要指定正確的 endpoint_url (可用指令 aws iot describe-endpoint 查詢)
