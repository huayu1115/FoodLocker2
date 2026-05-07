import logging
from auth import get_user_id, AuthError
from locker_repository import LockerRepository, LockerRepositoryError
from response import ResponseFormatter

# 初始化日誌記錄與 Repository 實例
logger = logging.getLogger()
logger.setLevel(logging.INFO)
repo = LockerRepository()

# 定義每一列包含的櫃子數量，用於 Grid Mapping
GRID_COLUMNS = 3 

def lambda_handler(event, context):
    """
    負責處理櫃位查詢請求：
    1. GET /lockers - 取得使用者自己的預約清單
    2. GET /lockers/{Location} - 取得特定區域的櫃位佈局矩陣
    """
    try:
        # 從路徑參數提取 Location
        path_params = event.get('pathParameters') or {}
        location = path_params.get('Location')

        # 路由邏輯: 判斷是區域查詢還是個人查詢
        if location:
            return handle_location_query(location)
        else:
            return handle_user_query(event)

    except AuthError as e:
        logger.warning(f"授權失敗: {e.message}")
        return ResponseFormatter.error(e.message, e.status_code)
    except LockerRepositoryError as e:
        logger.error(f"資料庫操作異常: {e.message}")
        return ResponseFormatter.error("無法取得櫃位資料，請稍後再試", 500)
    except Exception as e:
        logger.error(f"未預期的系統錯誤: {str(e)}", exc_info=True)
        return ResponseFormatter.error("伺服器內部錯誤", 500)

def handle_location_query(location: str):
    """取得特定區域的狀態，並轉換為前端渲染用的二維陣列"""
    logger.info(f"執行區域查詢: {location}")
    
    # 從 Repository 取得該區域的所有扁平化櫃位資料
    items = repo.list_by_location(location)
    
    if not items:
        return ResponseFormatter.error(f"找不到 '{location}' 區域的櫃位資訊", 404)

    # 1. 確保資料依據 Number 排序，以保證矩陣順序正確
    sorted_items = sorted(items, key=lambda x: int(x.get('Number', 0)))

    # 2. Grid Mapping 邏輯：將 1D 清單轉換為 2D 矩陣
    grid = []
    for i in range(0, len(sorted_items), GRID_COLUMNS):
        row = []
        for item in sorted_items[i:i + GRID_COLUMNS]:
            row.append({
                "id": str(item.get('Number')).zfill(3), # 格式化為 001, 002
                "status": item.get('Status', 'Error')   # 對應 Occupied, Available, Error
            })
        grid.append(row)

    return ResponseFormatter.success({"Grid": grid})

def handle_user_query(event: dict):
    """取得當前登入使用者所佔用的櫃位清單"""
    # 透過共用模組 auth.py 安全提取 Uid
    uid = get_user_id(event)
    logger.info(f"執行使用者查詢, UID: {uid}")

    # 篩選與 Uid 匹配的紀錄
    user_lockers = repo.list_by_user(uid)
    
    # 格式化輸出資料，僅包含前端需要的欄位
    formatted_list = []
    for locker in user_lockers:
        formatted_list.append({
            "location": locker.get('Location'),
            "number": int(locker.get('Number')),
            "Date": locker.get('UpdateDate') # 返回最後更新時間
        })

    return ResponseFormatter.success({"lockers": formatted_list})