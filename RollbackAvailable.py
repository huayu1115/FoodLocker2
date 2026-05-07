import json
import os
import logging
import boto3
from datetime import datetime
from botocore.exceptions import ClientError

# 匯入專案共用模組
from locker_repository import LockerRepository, LockerConflictError, LockerRepositoryError

# 系統日誌初始化
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# 初始化 AWS 服務客戶端
repo = LockerRepository()
dynamodb = boto3.resource('dynamodb')

# 從環境變數獲取事務資料表名稱
TRANSACTION_TABLE_NAME = os.environ.get('TRANSACTION_TABLE', 'LockerTransactions')
transaction_table = dynamodb.Table(TRANSACTION_TABLE_NAME)

def lambda_handler(event, context):
    """
    負責處理預約超時或失敗後的復原邏輯。
    1. 將 Locker 狀態由 'SoftLocked' 還原為 'Available'。
    2. 在 LockerTransactions 標記該筆交易為 'TIMEOUT'。
    """
    try:
        # 解析來自 Step Functions 的輸入參數
        # executionArn 通常來自狀態機的 Context Object ($$.Execution.Id)
        location = event.get('location')
        number = event.get('number')
        execution_arn = event.get('executionArn')
        
        if not location or not number:
            logger.error(f"Rollback 失敗: 缺少參數。Payload: {json.dumps(event)}")
            raise ValueError("Invalid Input: Missing location or number")
            
        logger.info(f"啟動補償機制：儲物櫃 {location}-{number}, 交易 ID: {execution_arn}")

        # 還原資源狀態，利用 Repository 的條件寫入，確保只有在狀態仍為 'Reserved' 時才重置
        # 若狀態已變更（例如：使用者剛好在最後一秒確認成功），則會拋出 LockerConflictError
        repo.update_locker_status(
            location=location,
            number=int(number),
            new_status='Available',
            expected_status='SoftLocked'  
        )
        logger.info(f"成功重置儲物櫃資源：{location}-{number} 已恢復為 Available")

        # 更新事務資料表紀錄
        if execution_arn:
            mark_transaction_as_timeout(execution_arn)
        
        return {
            "status": "ROLLED_BACK",
            "executionArn": execution_arn,
            "message": f"Locker {location}-{number} has been released due to timeout."
        }

    except LockerConflictError:
        # 攔截衝突：如果狀態已非 Reserved，代表這是一筆成功的交易，無需補償
        logger.warning(f"補償略過：櫃位 {location}-{number} 狀態已非 SoftLocked，可能已成功佔用。")
        return {
            "status": "SKIPPED",
            "message": "Locker state changed, compensation skipped."
        }
        
    except Exception as e:
        logger.error(f"補償程序發生未預期錯誤: {str(e)}", exc_info=True)
        # 拋出異常讓 Step Functions 知道補償失敗，可根據 ASL 配置進行重試
        raise e

def mark_transaction_as_timeout(execution_arn):
    """更新事務表狀態，紀錄此筆交易因超時而終止。"""
    try:
        now = datetime.utcnow().isoformat()
        transaction_table.update_item(
            Key={'ExecutionArn': execution_arn},
            UpdateExpression="SET #s = :s, UpdatedAt = :t",
            ExpressionAttributeNames={
                "#s": "Status"
            },
            ExpressionAttributeValues={
                ":s": "TIMEOUT",
                ":t": now
            }
        )
        logger.info(f"事務紀錄已更新為 TIMEOUT: {execution_arn}")
    except ClientError as e:
        # 即使更新事務表失敗，也不應影響資源釋放的邏輯，但需記錄日誌
        logger.error(f"更新事務表失敗: {e.response['Error']['Message']}")