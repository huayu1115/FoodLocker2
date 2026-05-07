import json
import logging
import os

from locker_repository import LockerConflictError, LockerRepository, LockerRepositoryError
from response import ResponseFormatter


logger = logging.getLogger()
logger.setLevel(logging.INFO)

repo = LockerRepository()


def get_locker_number(event):
    """
    這個 handler 只處理 AWS IoT Rule 轉來的 Shadow event。
    不處理 API Gateway，也不處理前端直接呼叫。

    IoT Rule 只會用 Named Shadow topic 觸發這支 Lambda。
    固定格式: $aws/things/locker-pi/shadow/name/3
    最後一段 3 就是 DynamoDB 的 Number。
    
    # 例如:
    # topic = "$aws/things/locker-pi/shadow/name/3"
    # parts[-1] = "3"
    
    # 如果 topic 不存在或最後一段不是數字，代表 IoT Rule 格式不符合預期。
    """
    topic = event.get("topic")
    if not isinstance(topic, str):
        return None

    parts = topic.split("/")
    if len(parts) >= 6 and parts[-2] == "name":
        try:
            return int(parts[-1])
        except ValueError:
            return None

    return None


def response_data(location, number, status=None, reported=None):
    """
    統一整理 Lambda response 裡的 data。
    這不是業務邏輯，只是讓成功/錯誤回應都帶相同的除錯資訊。
    
    reported 來自 Device Shadow 的 state.reported。
    目前只回傳 door_sensor / lock_status，方便 CloudWatch 或測試時確認硬體狀態。
    """
    data = {"location": location, "number": number}
    if status:
        data["status"] = status
    if reported:
        data["door_sensor"] = reported.get("door_sensor")
        data["lock_status"] = reported.get("lock_status")
    return data


def sync_locker_status(location, number, locker):
    """
    這裡是整支 Lambda 的核心判斷:
    同一個「door closed」硬體事件，代表什麼業務動作，
    要看 DynamoDB 目前的 Status。
    
    Reserved:
      使用者已預約櫃位，外送員正在放餐。
      door closed = 放餐完成。
    
    Occupied:
      餐點已在櫃內，使用者正在取餐。
      door closed = 取餐完成。
    
    其他狀態:
       例如 Available / Maintenance，不應被硬體關門事件改動。
    """
    current_status = locker.get("Status")
    uid = locker.get("Uid")
    # DB 是 Reserved 代表使用者預約後正在放餐。
    # 關門事件進來後, 放餐完成, 狀態改成 Occupied。
    if current_status == "Reserved":
        # 使用 expected_status 做條件更新，避免重複 IoT event 或並發請求
        # 把已經變動的狀態覆蓋掉。
        repo.update_locker_status(
            location=location,
            number=number,
            new_status="Occupied",
            uid=locker.get("Uid"),
            expected_status="Reserved",
        )
        # 目前同步處理僅更新 DB 狀態，不再依賴 Step Functions TaskToken 通知。
        logger.info("放餐完成，櫃位已轉為使用中: %s-%s", location, number)
        return ResponseFormatter.success(
            data=response_data(location, number, "Occupied"),
            message="櫃位已更新為使用中",
        )

    # DB 是 Occupied 代表餐點已在櫃內, 使用者正在取餐。
    # 關門事件進來後, 取餐完成, 狀態改回 Available 並清除 Uid。
    if current_status == "Occupied":
        # update_locker_status 在 new_status='Available' 時會清除 Uid。
        # 這代表櫃位已釋放，可以被下一位使用者預約。
        repo.update_locker_status(
            location=location,
            number=number,
            new_status="Available",
            expected_status="Occupied",
        )
        logger.info("取餐完成，櫃位已轉為可使用: %s-%s", location, number)
        return ResponseFormatter.success(
            data=response_data(location, number, "Available"),
            message="櫃位已釋放為可使用",
        )

    # 其他狀態例如 Available / Maintenance 不應該被 Shadow 關門事件改動。
    logger.warning(
        "硬體已回報關門，但櫃位狀態不符合同步流程: %s-%s，目前狀態: %s，uid: %s",
        location,
        number,
        current_status,
        uid,
    )
    return ResponseFormatter.success(
        data=response_data(location, number, current_status),
        message="櫃位狀態不符合同步流程，暫不同步",
    )


def lambda_handler(event, context):
    """
    預期 event 範例:
    
     {
       "topic": "$aws/things/locker-pi/shadow/name/3",
       "state": {
         "reported": {
           "door_sensor": "closed",
           "lock_status": "locked",
           "box_empty": true
         }
       }
     }
    
     topic 負責提供櫃號。
     state.reported 負責提供硬體狀態。
     Location 不在 IoT topic 裡, 所以由 Lambda 環境變數提供。
     Number 從 event["topic"] 解析。
    """
    location = os.environ.get("DEFAULT_LOCATION")
    number = get_locker_number(event or {})
    if not location or number is None:
        logger.warning("同步事件缺少 DEFAULT_LOCATION 或 topic 櫃號: %s", event)
        return ResponseFormatter.error("缺少櫃位地點或櫃號", 400)

    # Shadow payload 固定讀 state.reported。
    # 目前只用 door_sensor 判斷使用者是否完成開門後的動作。
    reported = (event.get("state") or {}).get("reported") or {}
    # 目前只看門是否關上。
    # lock_status / box_empty 暫時不參與判斷，避免硬體感測不穩造成誤同步。
    if reported.get("door_sensor") != "closed":
        logger.info("門尚未關閉，暫不同步櫃位: %s-%s", location, number)
        return ResponseFormatter.success(
            data=response_data(location, number, reported=reported),
            message="門尚未關閉，暫不同步櫃位",
        )

    try:
        # 門關上後才查 DB，避免門還開著時產生不必要的 DynamoDB 讀取。
        locker = repo.get_locker(location, number)
        if locker is None:
            logger.info("Shadow 同步時找不到櫃位: %s-%s", location, number)
            return ResponseFormatter.error(
                "找不到指定的櫃位",
                404,
                data=response_data(location, number),
            )

        return sync_locker_status(location, number, locker)

    except LockerConflictError as error:
        logger.warning("Shadow 同步時櫃位狀態已被更新: %s", error.message)
        return ResponseFormatter.error(
            "櫃位狀態已被更新，請重新確認",
            409,
            data=response_data(location, number),
        )
    except LockerRepositoryError as error:
        logger.error("Shadow 同步時資料庫更新失敗: %s", error.message)
        return ResponseFormatter.error("櫃位資料庫同步失敗", 500)
    except Exception as error:
        logger.error("Shadow 同步發生未預期錯誤: %s", str(error), exc_info=True)
        return ResponseFormatter.error("系統內部錯誤", 500)
