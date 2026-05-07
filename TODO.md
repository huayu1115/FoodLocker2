## Locker_Query_Handler

問題: DynamoDB 的 scan 會遍歷整張資料表來篩選 UID。當使用者數量增加到上千人時，這個操作會變得極度緩慢且昂貴，甚至導致 Lambda 超時。
- 應在 Lockers 資料表上針對 Uid 欄位建立 GSI (Global Secondary Index)

## Locker_Booking_Handler

問題: 如果資料庫更新成功，但 Lambda 在啟動 Step Functions 前突然崩潰或 AWS 服務短暫不穩，該櫃位將永遠卡在 Reserved 狀態，因為補償機制是由狀態機觸發的，而狀態機根本沒啟動。
- 使用 DynamoDB Streams 觸發狀態機，或者確保 Step Functions 的第一個步驟就是執行資料庫鎖定。

問題: get_task_token_with_retry 採用 Busy-waiting 方式獲取 Token。

## Locker_Control_Handler

問題: Lambda 回傳 200 Success 僅代表指令已送出，不代表鎖已打開，若樹莓派斷線，使用者會看到開鎖成功但實體櫃子沒動。
- 應監聽 Shadow 的 reported 屬性。  

問題: target_thing = "locker-pi" 是寫死的。
- 建資料庫

## 在多個 Handler 中使用了 int(number)
問題: 若前端傳入非數字字串，Lambda 會直接拋出 ValueError 並導致 500 Error，而非 400 Bad Request。
- 在執行邏輯前，應集中進行參數驗證。