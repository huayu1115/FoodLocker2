import json
import os
import logging
import boto3
from botocore.exceptions import ClientError
from datetime import datetime, timedelta

# 初始化日誌紀錄器
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# 初始化 DynamoDB 資源
dynamodb = boto3.resource('dynamodb')
# 從環境變數讀取交易資料表名稱
TRANSACTION_TABLE = os.environ.get('TRANSACTION_TABLE', 'LockerTransactions')
table = dynamodb.Table(TRANSACTION_TABLE)

def lambda_handler(event, context):
    """負責接收 Step Functions 產生的 Task Token，並將其與執行憑證綁定存入資料庫。"""
    logger.info(f"收到來自 Step Functions 的註冊請求: {json.dumps(event)}")

    # 提取輸入參數
    # 這些參數必須在 Step Functions ASL 的 Parameters 中定義傳遞
    task_token = event.get('taskToken')
    execution_arn = event.get('executionArn')
    location = event.get('location')
    number = event.get('number')
    uid = event.get('uid')

    # 基本參數校驗
    if not task_token or not execution_arn:
        logger.error("錯誤：缺少關鍵參數 taskToken 或 executionArn")
        raise ValueError("Invalid input: taskToken and executionArn are required.")

    # 計算 TTL (Time to Live)，讓 DynamoDB 自動清理已完成的事務紀錄
    expire_at = int((datetime.now() + timedelta(hours=2)).timestamp())

    try:
        # 執行資料寫入
        table.put_item(
            Item={
                'ExecutionArn': execution_arn,
                'TaskToken': task_token,      
                'Location': location,
                'Number': int(number),
                'Uid': uid,
                'Status': 'PENDING',
                'CreatedAt': datetime.utcnow().isoformat(),
                'ExpireAt': expire_at 
            }
        )
        
        logger.info(f"成功註冊任務憑證。ExecutionArn: {execution_arn}")

        # 回傳給 Step Functions 的回應
        # 此回應會被作為下一個步驟的輸入，或僅作為成功記錄
        return {
            "status": "REGISTERED",
            "executionArn": execution_arn,
            "location": location,
            "number": number
        }

    except ClientError as e:
        logger.error(f"資料庫寫入失敗: {e.response['Error']['Message']}")
        # 拋出異常以觸發 Step Functions 的 Retry 機制
        raise Exception("Database insertion failed, triggering state machine retry.")
    except Exception as e:
        logger.error(f"未預期的錯誤: {str(e)}", exc_info=True)
        raise e