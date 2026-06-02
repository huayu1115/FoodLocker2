import json
import os
import logging
import time
import secrets
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
sqs_client = boto3.client('sqs')
dynamodb = boto3.resource('dynamodb')

# 從環境變數獲取配置
STATE_MACHINE_ARN = os.environ.get('STATE_MACHINE_ARN')
SQS_QUEUE_URL = os.environ.get('SQS_QUEUE_URL')
TRANSACTION_TABLE_NAME = os.environ.get('TRANSACTION_TABLE', 'LockerTransactions')
transaction_table = dynamodb.Table(TRANSACTION_TABLE_NAME)

repo = LockerRepository()

def generate_otp() -> str:
    """使用 secrets 模組安全產生 000000-999999 之間的 6 位數密碼字串"""
    random_number = secrets.randbelow(1000000)
    return f"{random_number:06d}"

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
        # 利用 ConditionExpression 確保只有狀態為 Available 才能成功改為 SoftLocked
        repo.update_locker_status(
            location=location,
            number=number,
            new_status='SoftLocked',
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
        repo.update_locker_status(location, number, 'Available', expected_status='SoftLocked')
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
    """確認預約，負責換取 Token、喚醒狀態機, 並將櫃位最終確定為 Reserved。"""
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
    
    # 直接從 Cognito Token 中取得使用者的 Email 地址
    claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
    user_email = claims.get('email')
    
    if not user_email:
        # 資安或環境配置警訊：如果 Token 沒帶 email
        logger.warning(f"警告：無法從使用者 {uid} 的 Token 中解析出 email 屬性！")

    # 產生一次性密碼 OTP
    otp_password = generate_otp()
    
    # 更新資料庫資源狀態: SoftLocked -> Reserved
    try:
        repo.update_locker_status(
            location=location,
            number=number,
            new_status='Reserved',
            expected_status='SoftLocked'
        )
    except LockerConflictError:
        logger.error(f"資料庫狀態不一致：櫃位 {location}-{number} 無法更新為 Reserved")
        return ResponseFormatter.error("狀態更新衝突，請聯繫管理員", 409)
    
    # 喚醒 Step Functions
    task_token = transaction.get('TaskToken')
    try:
        sfn_client.send_task_success(
            taskToken=task_token,
            output=json.dumps({
                "final_status": "Reserved",
                "confirmed_by": uid
            })
        )
    except sfn_client.exceptions.TaskDoesNotExist:
        # 如果狀態機已經逾時關閉，將資料庫還原為 Available
        repo.update_locker_status(location, number, 'Available', expected_status='Reserved')
        return ResponseFormatter.error("預約已逾時被系統釋放，請重新發起預約", 400)
    except sfn_client.exceptions.InvalidToken:
        return ResponseFormatter.error("事務憑證無效", 400)
    except Exception as e:
        logger.error(f"通知狀態機時發生未預期錯誤: {str(e)}", exc_info=True)
        return ResponseFormatter.error("系統內部錯誤", 500)

    # 將 OTP、櫃位資訊以及 email 共同打包推送至 SQS
    if SQS_QUEUE_URL:
        sqs_payload = {
            "location": location,
            "number": number,
            "otp": otp_password,
            "email": user_email
        }
        try:
            logger.info(f"正在將預約資料與 Email 送往 SQS 佇列: {sqs_payload}")
            sqs_client.send_message(
                QueueUrl=SQS_QUEUE_URL,
                MessageBody=json.dumps(sqs_payload)
            )
        except ClientError as e:
            # 記錄高風險日誌（萬一SQS失敗，由於DB沒存密碼，必須留下紀錄供後台稽核）
            logger.critical(f"無法寫入 SQS! 櫃位 {location}-{number} 的 OTP {otp_password} 可能無法發送。錯誤: {str(e)}")
    else:
        logger.error("環境變數 SQS_QUEUE_URL 尚未配置，跳過 SQS 推送。")

    # 立即對使用者做出成功的 HTTP API 回應
    return ResponseFormatter.success(
        data={"final_status": "Reserved"},
        message="預約成功！取件密碼正發送至您的電子信箱。"
    )