import json
import os
import logging
import boto3
from botocore.exceptions import ClientError

# 初始化日誌紀錄器
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# 初始化 AWS 服務客戶端
iot_data_client = boto3.client('iot-data')
ses_client = boto3.client('ses')  # 用 SES 進行精準個人化 Email 發送

# 讀取環境變數配置
DEFAULT_LOCATION = os.environ.get('DEFAULT_LOCATION', 'A')
DEFAULT_THING_NAME = os.environ.get('DEFAULT_THING_NAME', 'locker-pi')
SES_VERIFIED_SOURCE_EMAIL = os.environ.get('SES_SOURCE_EMAIL', 'noreply@yourdomain.com')


def update_iot_device_shadow(number: int, otp: str):
    """
    同步更新 AWS IoT Core 上的 Named Shadow 密碼與狀態
    """
    thing_name = DEFAULT_THING_NAME
    shadow_name = str(number)

    shadow_payload = {
        "state": {
            "desired": {
                "password": otp,
                "status": "reserved"
            }
        }
    }

    try:
        logger.info(f"正在更新 IoT Device Shadow. ThingName: {thing_name}, ShadowName: {shadow_name}")
        iot_data_client.update_thing_shadow(
            thingName=thing_name,
            shadowName=shadow_name,
            payload=json.dumps(shadow_payload)
        )
        logger.info("AWS IoT Device Shadow 狀態同步成功。")
    except ClientError as e:
        logger.error(f"IoT Shadow 更新發生 AWS 錯誤: {e.response['Error']['Message']}")
        raise e


def send_email_via_ses(to_email: str, location: str, number: int, otp: str):
    """
    透過 AWS SES 發送一次性密碼通知信給預約的使用者
    """
    if not to_email:
        logger.warning(f"缺少收件人 Email，跳過郵件發送流程。櫃位: {location}-{number}")
        return

    # 定義個人化郵件內容
    subject_text = "【智慧儲物櫃】您的預約已成功與取件密碼通知"
    body_text = (
        f"親愛的使用者您好：\n\n"
        f"您的智慧儲物櫃預約已確認成功！相關取件資訊如下：\n"
        f"----------------------------------------\n"
        f" 儲物櫃地點: {location}\n"
        f" 櫃位編號: {number} 號櫃\n"
        f" 一次性取件密碼 (OTP): {otp}\n"
        f"----------------------------------------\n"
        f"提示：取件密碼自預約成功起算 5 分鐘內有效，請儘速至現場操作開櫃。\n"
        f"本信件為系統自動發送，請勿直接回覆。"
    )

    try:
        logger.info(f"正在透過 SES 發送密碼信件至: {to_email}")
        ses_client.send_email(
            Source=SES_VERIFIED_SOURCE_EMAIL,  # 必須是您在 AWS SES 控制台驗證過的電子信箱或網域
            Destination={
                'ToAddresses': [to_email]
            },
            Message={
                'Subject': {
                    'Data': subject_text,
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
        # 拋出異常，讓 SQS 觸發重試機制
        raise e


def lambda_handler(event, context):
    """
    Worker 主要入口點，監聽 SQS 佇列事件。
    """
    records = event.get('Records', [])
    logger.info(f"收到來自 SQS 的事件，包含 {len(records)} 筆訊息")

    for record in records:
        message_id = record.get('messageId')
        
        try:
            # 解析來自 SQS Body 的 JSON 資料
            body = json.loads(record.get('body', '{}'))
            location = body.get('location', DEFAULT_LOCATION)
            number = body.get('number')
            otp = body.get('otp')
            user_email = body.get('email')

            # 基礎防禦性檢查
            if number is None or not otp:
                logger.error(f"[Message ID: {message_id}] SQS 訊息核心欄位缺失，跳過處理。Body: {body}")
                continue

            logger.info(f"[Message ID: {message_id}] 開始執行非同步任務 -> 櫃位: {location}-{number}")

            # 同步更新 IoT Core Named Shadow
            update_iot_device_shadow(int(number), otp)

            # 發送 Email 通知信給使用者
            send_email_via_ses(user_email, location, int(number), otp)

            logger.info(f"[Message ID: {message_id}] 該筆預約的硬體同步與信件通知已全部完成！")

        except Exception as e:
            # 攔截任何未預期或 AWS 服務錯誤
            logger.error(f"[Message ID: {message_id}] 處理訊息時發生錯誤: {str(e)}", exc_info=True)
            # 拋出 Exception，告訴 SQS 這次失敗了，SQS 會在可見性逾時到期後重新派發該訊息進行重試
            raise e

    return {
        'statusCode': 200,
        'body': json.dumps('Worker 批次處理完畢')
    }