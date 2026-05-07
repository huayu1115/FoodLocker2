import json
import os
import logging
import time
import boto3
from botocore.exceptions import ClientError

# 引入共用模組
from auth import get_user_id, AuthError
from locker_repository import LockerRepository, LockerConflictError, LockerRepositoryError
from response import ResponseFormatter

# 初始化日誌紀錄器，便於 CloudWatch 追蹤
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# 初始化 AWS 客戶端與資源
sfn_client = boto3.client('stepfunctions')
dynamodb = boto3.resource('dynamodb')

# 從環境變數獲取配置
STATE_MACHINE_ARN = os.environ.get('STATE_MACHINE_ARN')
TRANSACTION_TABLE_NAME = os.environ.get('TRANSACTION_TABLE', 'LockerTransactions')
transaction_table = dynamodb.Table(TRANSACTION_TABLE_NAME)

repo = LockerRepository()

def lambda_handler(event, context):
    """
    負責處理櫃位的預約生命週期。
    1. start-booking: Soft Lock櫃位,並啟動超時倒數。
    2. exec-booking: 確認預約,完成交易。
    """
    try:
        resource = event.get('resource', '')
        path_params = event.get('pathParameters') or {}
        location = path_params.get('Location')
        number = path_params.get('Number')

        if not location or not number:
            return ResponseFormatter.error("缺少必要參數: Location 或 Number", 400)
        
        number = int(number)

        # 路由分流
        if resource.endswith('/start-booking'):
            return handle_start_booking(event, location, number)
        elif resource.endswith('/exec-booking'):
            return handle_exec_booking(event, location, number)
        else:
            return ResponseFormatter.error("無效的 API 路由", 404)

    except AuthError as e:
        logger.warning(f"授權失敗: {e.message}")
        return ResponseFormatter.error(e.message, e.status_code)
    except Exception as e:
        logger.error(f"伺服器內部錯誤: {str(e)}", exc_info=True)
        return ResponseFormatter.error("系統發生未預期錯誤，請稍後再試", 500)

def handle_start_booking(event, location, number):
    """發起預約負責將櫃位保留，並啟動超時倒數工作流。"""
    uid = get_user_id(event)
    logger.info(f"使用者 {uid} 嘗試預約 {location}-{number}")

    try:
        # 利用 ConditionExpression 確保只有狀態為 Available 才能成功改為 Reserved
        repo.update_locker_status(
            location=location,
            number=number,
            new_status='Reserved',
            uid=uid,
            expected_status='Available'
        )
    except LockerConflictError:
        return ResponseFormatter.error("該櫃位剛剛已被預約，請選擇其他櫃位", 409)

    # 啟動 Step Functions
    try:
        response = sfn_client.start_execution(
            stateMachineArn=STATE_MACHINE_ARN,
            input=json.dumps({
                "location": location,
                "number": number,
                "uid": uid
            })
        )
        execution_arn = response.get('executionArn')
        
        return ResponseFormatter.success(
            data={"executionArn": execution_arn, "status": "PENDING"},
            message="櫃位已為您保留 5 分鐘，請盡快確認。"
        )
    except ClientError as e:
        logger.error(f"啟動狀態機失敗: {str(e)}")
        # 若狀態機啟動失敗，必須立刻退回資料庫的資源狀態，避免 Deadlock
        repo.update_locker_status(location, number, 'Available', expected_status='Reserved')
        return ResponseFormatter.error("系統繁忙，無法建立預約事務", 500)

def get_transaction_with_retry(execution_arn, max_retries=4, delay=0.25):
    """因 Step Functions 產生 Token 寫入 DB 是非同步的，需要給予微小緩衝時間。"""
    for i in range(max_retries):
        try:
            response = transaction_table.get_item(Key={'ExecutionArn': execution_arn})
            item = response.get('Item')
            if item and item.get('TaskToken'):
                return item 
        except ClientError as e:
            logger.warning(f"讀取事務表失敗: {str(e)}")
            
        time.sleep(delay)
    return None

def handle_exec_booking(event, location, number):
    """確認預約，負責換取 Token、喚醒狀態機, 並將櫃位最終確定為 Occupied。"""
    uid = get_user_id(event)
    body = json.loads(event.get('body') or '{}')
    execution_arn = body.get('executionArn')

    if not execution_arn:
        return ResponseFormatter.error("缺少事務憑證 (executionArn)", 400)

    # 執行帶有重試機制的讀取
    transaction = get_transaction_with_retry(execution_arn)
    
    if not transaction:
        return ResponseFormatter.error("找不到預約紀錄，請稍後再試", 404)

    # 身分與資源一致性校驗
    if (transaction.get('Uid') != uid or 
        transaction.get('Location') != location or 
        int(transaction.get('Number')) != number):
        logger.error(f"安全性衝突：事務資訊不匹配！")
        return ResponseFormatter.error("憑證資訊與目標櫃位不符", 403)
    
    # 先更新資料庫資源狀態: Reserved -> Occupied
    try:
        repo.update_locker_status(
            location=location,
            number=number,
            new_status='Occupied',
            expected_status='Reserved'
        )
    except LockerConflictError:
        logger.error(f"資料庫狀態不一致：櫃位 {location}-{number} 無法更新為 Occupied")
        return ResponseFormatter.error("狀態更新衝突，請聯繫管理員", 409)
    
    # 最後才喚醒 Step Functions
    task_token = transaction.get('TaskToken')
    try:
        sfn_client.send_task_success(
            taskToken=task_token,
            output=json.dumps({
                "final_status": "Occupied",
                "confirmed_by": uid
            })
        )
    except sfn_client.exceptions.TaskDoesNotExist:
        return ResponseFormatter.error("預約已逾時被系統釋放，請重新發起預約", 400)
    except sfn_client.exceptions.InvalidToken:
        return ResponseFormatter.error("事務憑證無效", 400)
    except Exception as e:
        logger.error(f"通知狀態機時發生未預期錯誤: {str(e)}", exc_info=True)

    return ResponseFormatter.success(
        data={"final_status": "Occupied"},
        message="預約成功！儲物櫃已正式為您保留。"
    )