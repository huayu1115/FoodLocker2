import logging

# 配置日誌，以便在 CloudWatch 中稽核非法存取嘗試
logger = logging.getLogger()
logger.setLevel(logging.INFO)

class OwnershipError(Exception):
    """自定義權限異常類別。"""
    def __init__(self, message="您沒有權限操作此櫃位", status_code=403):
        self.message = message
        self.status_code = status_code
        super().__init__(self.message)

class OwnershipValidator:
    """提供統一的櫃位擁有權檢查邏輯。"""

    @staticmethod
    def validate(locker: dict, uid: str) -> bool:
        """核心驗證函式。"""
        
        # 1. 檢查櫃位是否存在
        if not locker:
            logger.warning("驗證失敗：目標櫃位不存在。")
            raise OwnershipError("找不到指定的櫃位資訊", status_code=404)

        # 2. 獲取櫃位當前的狀態與擁有者
        current_status = locker.get('Status')
        locker_owner = locker.get('Uid')

        # 3. 狀態邏輯預檢
        # 如果櫃位狀態為 'Available'，理論上不應該有 Uid，
        # 此時嘗試開鎖或執行 exec-booking 在業務邏輯上是錯誤的。
        if current_status == 'Available':
            logger.warning(f"驗證失敗：櫃位 {locker.get('PID')} 目前為閒置狀態。")
            raise OwnershipError("此櫃位目前處於閒置狀態，無法執行操作")

        # 4. 擁有權比對
        # 嚴格比對資料庫中的 Uid 與 API Gateway 傳入的 Cognito sub。
        if not locker_owner or locker_owner != uid:
            logger.error(f"安全性警報：使用者 {uid} 嘗試越權操作櫃位！該櫃位屬於 {locker_owner}")
            raise OwnershipError("權限不足：您並非此櫃位的預約者或使用人", status_code=403)

        logger.info(f"權限驗證通過：使用者 {uid} 合法持有櫃位 {locker.get('Location')}-{locker.get('Number')}")
        return True