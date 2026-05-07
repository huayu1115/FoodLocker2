import json
import boto3
import logging
import os

# 匯入共用模組
from auth import get_user_id, AuthError
from locker_repository import LockerRepository, LockerRepositoryError
from response import ResponseFormatter
from validator import OwnershipValidator, OwnershipError

# 初始化日誌記錄器
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# 初始化 AWS 客戶端
# 注意：iot-data 客戶端需要指定正確的 endpoint_url (可用指令 aws iot describe-endpoint 查詢)
iot_data = boto3.client('iot-data')
repo = LockerRepository()

def lambda_handler(event, context):
    """
    執行遠端開鎖流程。
    1. 從 Token 提取身分
    2. 驗證櫃位擁有權與狀態
    3. 同步更新 IoT Device Shadow 的 desired 狀態
    """
    try:
        # 參數解析
        path_params = event.get('pathParameters') or {}
        location = path_params.get('Location')
        number = path_params.get('Number')

        if not location or not number:
            return ResponseFormatter.error("路徑參數缺失 (Location/Number)", 400)
        
        # 轉為整數以符合資料庫設計
        locker_number = int(number)

        # 身分識別與資料讀取
        uid = get_user_id(event)
        logger.info(f"使用者 {uid} 請求開啟櫃位: {location}-{locker_number}")

        # 從 DynamoDB 取得該櫃位的最新狀態
        locker = repo.get_locker(location, locker_number)

        # 擁有權與狀態檢查
        OwnershipValidator.validate(locker, uid)

        # IoT Device Shadow
        # 根據 Location 決定實體 Thing 名稱 (TODO: 改由配置表取得)
        target_thing = "locker-pi" 
        
        # 將櫃位編號作為 Named Shadow 的名稱
        target_shadow = str(locker_number) 
        
        # 準備 Desired State
        shadow_payload = {
            "state": {
                "desired": {
                    "lock_status": "unlocked" 
                }
            }
        }
        
        # 更新特定shadow
        iot_data.update_thing_shadow(
            thingName=target_thing,
            shadowName=target_shadow,
            payload=json.dumps(shadow_payload)
        )
        
        logger.info(f"IoT 指令發送成功, ThingName: {target_thing}, ShadowName: {target_shadow}")

        # 回傳成功
        return ResponseFormatter.success(
            message=f"Locker {location}-{locker_number} has been unlocked"
        )

    # 異常捕捉
    except AuthError as e:
        return ResponseFormatter.error(e.message, e.status_code)
    except OwnershipError as e:
        # 當使用者嘗試開啟不屬於自己的櫃位時，回傳 403 Forbidden
        logger.warning(f"攔截越權操作嘗試: {e.message}")
        return ResponseFormatter.error(e.message, e.status_code)
    except LockerRepositoryError as e:
        logger.error(f"資料庫操作失敗: {e.message}")
        return ResponseFormatter.error("無法連接資料庫驗證權限", 500)
    except Exception as e:
        logger.error(f"系統未預期錯誤: {str(e)}", exc_info=True)
        return ResponseFormatter.error("伺服器內部錯誤 (Internal Server Error)", 500)