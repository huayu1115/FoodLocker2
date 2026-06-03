import json
import os
import logging
import secrets
import boto3
from datetime import datetime
from botocore.exceptions import ClientError

# 初始化日誌紀錄器
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# 初始化 AWS 服務客戶端
iot_data_client = boto3.client('iot-data')
ses_client = boto3.client('ses')  

# 讀取環境變數配置
DEFAULT_LOCATION = os.environ.get('DEFAULT_LOCATION', 'A')
DEFAULT_THING_NAME = os.environ.get('DEFAULT_THING_NAME', 'locker-pi')
SES_VERIFIED_SOURCE_EMAIL = os.environ.get('SES_SOURCE_EMAIL', 'noreply@yourdomain.com')


def generate_otp() -> str:
    """使用 secrets 模組安全產生 000000-999999 之間的 6 位數密碼字串"""
    random_number = secrets.randbelow(1000000)
    return f"{random_number:06d}"


def update_iot_device_shadow(number: int, otp: str, action: str):
    """
    同步更新 AWS IoT Core 上的 Named Shadow 密碼與狀態。
    直接在函式內部根據 action 決定 Payload 結構。
    """
    thing_name = DEFAULT_THING_NAME
    shadow_name = str(number)

    # 在函式內部做業務邏輯判斷
    if action == "refresh_user_otp":
        desired_state = {
            "password": otp
        }
    else:
        desired_state = {
            "password": otp,
            "status": "reserved"
        }

    shadow_payload = {
        "state": {
            "desired": desired_state
        }
    }

    try:
        logger.info(f"正在更新 IoT Device Shadow. ThingName: {thing_name}, ShadowName: {shadow_name}, Action: {action}")
        iot_data_client.update_thing_shadow(
            thingName=thing_name,
            shadowName=shadow_name,
            payload=json.dumps(shadow_payload)
        )
        logger.info("AWS IoT Device Shadow 狀態同步成功。")
    except ClientError as e:
        logger.error(f"IoT Shadow 更新發生 AWS 錯誤: {e.response['Error']['Message']}")
        raise e


def send_email_via_ses(to_email: str, subject: str, body_text: str, location: str, number: int):
    """
    透過 AWS SES 發送個人化通知信給預約的使用者
    """
    if not to_email:
        logger.warning(f"缺少收件人 Email，跳過郵件發送流程。櫃位: {location}-{number}")
        return

    try:
        logger.info(f"正在透過 SES 發送信件至: {to_email}")
        ses_client.send_email(
            Source=SES_VERIFIED_SOURCE_EMAIL,
            Destination={
                'ToAddresses': [to_email]
            },
            Message={
                'Subject': {
                    'Data': subject,
                    'Charset': 'UTF-8'
                },
                'Body': {
                    'Text': {
                        'Data': body_text,
                        'Charset': 'UTF-8'
                    }
                }
            }
        )
        logger.info(f"SES 郵件發送成功！收件人: {to_email}")
    except ClientError as e:
        logger.error(f"AWS SES 發送信件失敗: {e.response['Error']['Message']}")
        raise e


def lambda_handler(event, context):
    """
    Worker 主要入口點，監聽 SQS 佇列事件。
    1. exec-booking 後的初始密碼派發 (不帶 action 或 action='send_initial_otp')
    2. Shadow 關門觸發的更換新使用者密碼 (action='refresh_user_otp')
    """
    records = event.get('Records', [])
    logger.info(f"收到來自 SQS 的事件，包含 {len(records)} 筆訊息")

    for record in records:
        message_id = record.get('messageId')
        
        try:
            # 解析來自 SQS Body 的 JSON 資料
            body = json.loads(record.get('body', '{}'))
            action = body.get('action', 'send_initial_otp')
            location = body.get('location', DEFAULT_LOCATION)
            number = body.get('number')
            user_email = body.get('email')

            # 基礎防禦性檢查
            if number is None:
                logger.error(f"[Message ID: {message_id}] SQS 訊息缺少櫃位編號，跳過處理。")
                continue

            logger.info(f"[Message ID: {message_id}] 開始執行動作 [{action}] -> 櫃位: {location}-{number}")

            # 根據不同的業務動作，決定密碼獲取方式與信件模板
            if action == "refresh_user_otp":
                # 外送員關門後，Worker 端主動產生全新的一次性取件碼，徹底阻斷外送員回頭開櫃的可能性
                otp = generate_otp()
                subject_text = f"【智慧儲物櫃】餐點已送達！請憑新密碼取件(密碼: {otp})"
                mail_body_prefix = "外送員已將您的餐點安全放入櫃中！"
            else:
                # exec-booking 流程，直接拿前端上傳、已經定義好的預約密碼
                otp = generate_otp()
                subject_text = f"【智慧儲物櫃】您的預約已成功與取件密碼通知(密碼: {otp})"
                mail_body_prefix = "您的智慧儲物櫃預約已確認成功！相關取件資訊如下："

            # 密碼二次防禦檢查
            if not otp:
                logger.error(f"[Message ID: {message_id}] 動作 [{action}] 無法取得有效密碼，跳過處理。")
                continue

            # 覆寫 IoT Core Named Shadow
            update_iot_device_shadow(int(number), otp, action)

            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            email_lines = [
                "親愛的使用者您好：",
                "",  # 空白行
                f"{mail_body_prefix}",
                "----------------------------------------",
                f" 儲物櫃地點: {location}",
                f" 櫃位編號: {number} 號櫃",
                f" 最新取件密碼 (OTP): {otp}",
                f" 通知發送時間: {current_time}",
                "----------------------------------------",
                "",  # 空白行
                "提示：為保障財產安全，此密碼為專屬一次性取件碼，外送員無法再次開啟。",
                "本信件為系統自動發送，請勿直接回覆。"
            ]

            # 使用一個安全斷行 \n 進行完美串接
            body_text = "\n".join(email_lines)

            # 發送 Email 通知信給使用者
            send_email_via_ses(user_email, subject_text, body_text, location, int(number))

            logger.info(f"[Message ID: {message_id}] 櫃位 {location}-{number} 的處理流程 [{action}] 已全部順利完成！")

        except Exception as e:
            # 攔截任何錯誤，拋出 Exception 讓 SQS 重新排隊重試
            logger.error(f"[Message ID: {message_id}] 處理訊息時發生錯誤: {str(e)}", exc_info=True)
            raise e

    return {
        'statusCode': 200,
        'body': json.dumps('Worker 批次處理完畢')
    }