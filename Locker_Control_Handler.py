import json
import boto3
import logging
import os
from botocore.exceptions import ClientError

# 匯入共用模組
from auth import get_user_id, AuthError
from locker_repository import LockerRepository, LockerRepositoryError
from response import ResponseFormatter
from validator import OwnershipValidator, OwnershipError

# 初始化日誌記錄器
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# 初始化 AWS 客戶端
iot_data = boto3.client('iot-data')
dynamodb = boto3.resource('dynamodb')

# 從環境變數獲取配置表名稱
CONFIG_TABLE_NAME = os.environ.get('CONFIG_TABLE', 'LockerConfig')
config_table = dynamodb.Table(CONFIG_TABLE_NAME)
repo = LockerRepository()

def lambda_handler(event, context):
    """
    執行遠端開鎖流程。
    1. 從 Token 提取身分
    2. 驗證櫃位擁有權與狀態
    3. 從配置表取得對應的 ThingName
    4. 更新 IoT Device Shadow 的 desired 狀態
    """
    try:
        # 參數解析
        path_params = event.get('pathParameters') or {}
        location = path_params.get('Location')
        number = path_params.get('Number')

        if not location or not number:
            return ResponseFormatter.error("路徑參數缺失 (Location/Number)", 400)
        
        locker_number = int(number)
        uid = get_user_id(event)
        logger.info(f"使用者 {uid} 請求開啟櫃位: {location}-{locker_number}")

        # 身分識別與權限驗證
        locker = repo.get_locker(location, locker_number)
        OwnershipValidator.validate(locker, uid)

        # 從 LockerConfig 表中查詢該地點對應的實體設備名稱
        try:
            config_res = config_table.get_item(Key={'Location': location})
            config_item = config_res.get('Item')
            
            if not config_item or 'ThingName' not in config_item:
                logger.error(f"地點配置缺失: {location}")
                return ResponseFormatter.error(f"該區域 {location} 尚未配置實體設備", 404)
            
            target_thing = config_item['ThingName']
        except ClientError as e:
            logger.error(f"讀取配置表失敗: {str(e)}")
            return ResponseFormatter.error("系統配置讀取失敗", 500)

        # IoT Device Shadow 更新
        target_shadow = str(locker_number) # 使用櫃位編號作為 Named Shadow
        shadow_payload = {
            "state": {
                "desired": {
                    "lock_status": "unlocked" 
                }
            }
        }
        
        iot_data.update_thing_shadow(
            thingName=target_thing,
            shadowName=target_shadow,
            payload=json.dumps(shadow_payload)
        )
        
        logger.info(f"IoT 指令發送成功, Thing: {target_thing}, Shadow: {target_shadow}")

        return ResponseFormatter.success(
            message=f"Locker {location}-{locker_number} has been unlocked"
        )

    except AuthError as e:
        return ResponseFormatter.error(e.message, e.status_code)
    except OwnershipError as e:
        logger.warning(f"攔截越權操作嘗試: {e.message}")
        return ResponseFormatter.error(e.message, e.status_code)
    except (LockerRepositoryError, Exception) as e:
        logger.error(f"系統錯誤: {str(e)}", exc_info=True)
        return ResponseFormatter.error("伺服器內部錯誤", 500)