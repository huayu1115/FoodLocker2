import json
from decimal import Decimal
from typing import Any, Dict, List, Union

class ResponseFormatter:
    """
    統一標準化 API Gateway + Lambda 的回傳格式。
    本模組提供成功與錯誤的回傳封裝，並自動處理 CORS 與資料型別轉換。
    """

    @staticmethod
    def _decimal_default(obj: Any) -> Union[int, float]:
        """
        處理 DynamoDB 特有的 Decimal 型別轉換問題。
        由於 json.dumps 原生不支持 Decimal，本函式會將其轉為 Python 標準數值型別。
        """
        if isinstance(obj, Decimal):
            # 判斷是否有小數點，以決定轉換為 int 或 float
            return int(obj) if obj % 1 == 0 else float(obj)
        raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")

    @staticmethod
    def build_response(
        status_code: int, 
        success: bool, 
        message: str = "", 
        data: Any = None
    ) -> Dict[str, Any]:
        """
        核心回傳建構函式，產生 API Gateway Proxy 整合要求的格式。
        
        輸入：
        - status_code (int): HTTP 狀態碼 (例如 200, 400, 500)。
        - success (bool): 業務邏輯執行是否成功。
        - message (str): 提供給前端的提示訊息或錯誤說明。
        - data (Any): 實際回傳的資料內容 (dict, list, 或 str)。
        """
        
        # 定義標準化的 JSON Body 結構
        body_content = {
            "success": success,
            "message": message,
            "data": data if data is not None else {}
        }
        
        return {
            "statusCode": status_code,
            "headers": {
                "Content-Type": "application/json",
                # 加入 CORS 標頭，確保前端 Vue 應用程式能跨網域呼叫 API
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, Authorization"
            },
            # 使用自定義的 _decimal_default 處理 DynamoDB 資料
            "body": json.dumps(body_content, default=ResponseFormatter._decimal_default)
        }

    @staticmethod
    def success(data: Any = None, message: str = "Success", status_code: int = 200) -> Dict[str, Any]:
        """產生成功的 HTTP 回應。"""
        return ResponseFormatter.build_response(status_code, True, message, data)

    @staticmethod
    def error(message: str = "Internal Server Error", status_code: int = 500, data: Any = None) -> Dict[str, Any]:
        """產生失敗的 HTTP 回應。"""
        return ResponseFormatter.build_response(status_code, False, message, data)