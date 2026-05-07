import logging

# 配置日誌記錄，便於在 CloudWatch 中追蹤問題
logger = logging.getLogger()
logger.setLevel(logging.INFO)

class AuthError(Exception):
    """自定義授權異常"""
    def __init__(self, message, status_code=401):
        self.message = message
        self.status_code = status_code
        super().__init__(self.message)

def get_user_id(event: dict) -> str:
    """從 API Gateway 觸發的事件中解析並回傳 Cognito 使用者唯一識別碼 (sub)。"""
    try:
        # 經 API Gateway 驗證過的 authorizer context 中提取 userID
        authorizer = event.get('requestContext', {}).get('authorizer', {})
        claims = authorizer.get('claims', {})
        
        # 'sub' 是 Cognito 中使用者的唯一且不可變的識別碼
        uid = claims.get('sub')
        
        if not uid:
            logger.error("解析失敗：事件物件中缺少授權資訊 (authorizer claims)")
            raise AuthError("未授權：無法驗證使用者身分")
            
        logger.info(f"成功解析身分, UID: {uid}")
        return str(uid)
        
    except KeyError as e:
        logger.error(f"事件結構異常，找不到關鍵路徑: {str(e)}")
        raise AuthError("系統錯誤：身分驗證路徑配置錯誤", status_code=500)
    except Exception as e:
        if isinstance(e, AuthError):
            raise e
        logger.error(f"未知的身分驗證錯誤: {str(e)}")
        raise AuthError("身分驗證失敗")

def is_admin(event: dict) -> bool:
    """(擴充功能) 檢查使用者是否具備管理員權限。"""
    claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
    groups = claims.get('cognito:groups', [])
    
    # 支援單一字串或列表格式的群組資訊
    if isinstance(groups, str):
        groups = [groups]
        
    return 'Admins' in groups