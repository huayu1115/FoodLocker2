import boto3
import os
import logging
from botocore.exceptions import ClientError
from datetime import datetime
from typing import List, Dict, Optional

# 初始化日誌
logger = logging.getLogger()
logger.setLevel(logging.INFO)

class LockerRepositoryError(Exception):
    """資料庫操作基礎異常"""
    def __init__(self, message, code="InternalError"):
        self.message = message
        self.code = code
        super().__init__(self.message)

class LockerConflictError(LockerRepositoryError):
    """當櫃位狀態不符合預期時拋出 (如已被預約)"""
    def __init__(self, message):
        super().__init__(message, code="Conflict")

# --- Repository 實作 ---
class LockerRepository:
    def __init__(self):
        # 從環境變數讀取表名，預設為 'Lockers'
        self.table_name = os.environ.get('DYNAMODB_TABLE', 'Lockers')
        self.table = boto3.resource('dynamodb').Table(self.table_name)

    def get_locker(self, location: str, number: int) -> Optional[Dict]:
        """
        取得單一櫃位資料
        使用場景：開鎖前驗證擁有者 (Locker_Control)
        """
        try:
            # Location 為 Partition Key, Number 為 Sort Key
            response = self.table.get_item(
                Key={'Location': location, 'Number': int(number)}
            )
            return response.get('Item')
        except ClientError as e:
            logger.error(f"GetItem 失敗: {str(e)}")
            raise LockerRepositoryError("無法讀取櫃位資料")

    def list_by_location(self, location: str) -> List[Dict]:
        """
        取得特定區域的所有櫃位矩陣資料
        使用場景：前端 Vue 渲染 Grid
        """
        try:
            # 使用 Query 而非 Scan，效能與成本最佳化
            response = self.table.query(
                KeyConditionExpression="Location = :loc",
                ExpressionAttributeValues={":loc": location}
            )
            return response.get('Items', [])
        except ClientError as e:
            logger.error(f"區域查詢失敗: {str(e)}")
            raise LockerRepositoryError(f"無法取得 {location} 區域資料")

    def list_by_user(self, uid: str) -> List[Dict]:
        """
        取得該使用者預約的所有櫃位
        使用場景：個人中心查看我的櫃位
        """
        try:
            # 無法使用 GSI (UidIndex)，用 Scan 搭配 FilterExpression
            response = self.table.scan(
                FilterExpression="Uid = :u",
                ExpressionAttributeValues={":u": uid}
            )
            
            items = response.get('Items', [])
            
            # 處理 Scan 的分頁機制
            while 'LastEvaluatedKey' in response:
                response = self.table.scan(
                    FilterExpression="Uid = :u",
                    ExpressionAttributeValues={":u": uid},
                    ExclusiveStartKey=response['LastEvaluatedKey']
                )
                items.extend(response.get('Items', []))
                
            logger.info(f"成功掃描使用者 {uid} 的預約紀錄，共 {len(items)} 筆")
            return items
            
        except ClientError as e:
            logger.error(f"使用者掃描過濾失敗: {str(e)}")
            raise LockerRepositoryError("系統目前無法讀取您的預約紀錄，請聯繫管理員")

    def update_locker_status(
        self, 
        location: str, 
        number: int, 
        new_status: str, 
        uid: str = None, 
        expected_status: str = None
    ):
        """
        原子性更新櫃位狀態。
        
        參數說明：
        - expected_status: 預期目前的狀態，若不符則代表被搶佔 (Race Condition)
        """
        now = datetime.utcnow().isoformat()
        
        # 準備更新語句
        update_expr = "SET #s = :s, UpdateDate = :d"
        expr_names = {"#s": "Status"}
        expr_values = {":s": new_status, ":d": now}
        
        if uid:
            update_expr += ", Uid = :u"
            expr_values[":u"] = uid
        elif new_status == 'Available':
            # 若變回可用，則清除 Uid 欄位
            update_expr += " REMOVE Uid"

        update_args = {
            "Key": {'Location': location, 'Number': int(number)},
            "UpdateExpression": update_expr,
            "ExpressionAttributeNames": expr_names,
            "ExpressionAttributeValues": expr_values
        }

        # 加入條件寫入邏輯：防止 Race Condition
        if expected_status:
            update_args["ConditionExpression"] = "#s = :expected"
            expr_values[":expected"] = expected_status

        try:
            self.table.update_item(**update_args)
            logger.info(f"櫃位 {location}-{number} 狀態更新為 {new_status}")
        except ClientError as e:
            if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
                # 補足之處：明確拋出衝突異常，讓 Handler 回傳 409
                raise LockerConflictError(f"櫃位狀態已變更，預期應為 {expected_status}")
            logger.error(f"更新失敗: {str(e)}")
            raise LockerRepositoryError("資料庫更新失敗")