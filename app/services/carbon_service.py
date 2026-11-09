"""
碳足跡標示 (參考值)。

- 原料設定排放係數 (每 1 單位的 g CO2e) 與產地；餐點碳足跡 = Σ 配方用量 × 係數 (g CO2e / 份)，
  存在 menu_item.carbon_grams (同步到線上端，點餐頁 / 看板直接讀，不必有配方資料)。
- carbon_auto (預設) 時原料係數或配方變更就重算；菜單進階設定可改為手動填寫 (沒有配方、或引用供應商提供的數字)。
- 係數來源由店家自行填寫 (例如環境部產品碳足跡資料庫、供應商碳足跡報告)，點餐頁一律標示「參考值」。
"""
from app.models.inventory import RecipeLine
from app.models.menu import MenuItem

MAX_GRAMS = 1_000_000


def recipe_carbon(menu_item):
    """依配方估算 (g CO2e / 份，四捨五入成整數)；沒有配方或係數都為 0 時回 0"""
    lines = RecipeLine.query.filter_by(menu_item_id=menu_item.id).all()
    total = sum((rl.quantity or 0) * (rl.ingredient.carbon_factor or 0) for rl in lines if rl.ingredient is not None)
    return min(MAX_GRAMS, int(round(total)))


def refresh_item(menu_item):
    """carbon_auto 的餐點重算碳足跡；回傳是否有變動"""
    if not menu_item.carbon_auto:
        return False
    grams = recipe_carbon(menu_item)
    if grams == (menu_item.carbon_grams or 0):
        return False
    menu_item.carbon_grams = grams
    return True


def refresh_for_ingredient(ingredient):
    """原料係數變更：所有用到它的餐點重算；回傳變動的餐點數"""
    item_ids = {rl.menu_item_id for rl in RecipeLine.query.filter_by(ingredient_id=ingredient.id).all()}
    if not item_ids:
        return 0
    return sum(refresh_item(i) for i in MenuItem.query.filter(MenuItem.id.in_(item_ids)).all())


def refresh_all(tenant_id=None):
    """後台一鍵重算 (匯入原料係數後)；回傳變動的餐點數。
    只重算本機有配方的餐點：分店沒有總部的配方資料，否則會把同步來的碳足跡改成 0"""
    ids = {rl.menu_item_id for rl in RecipeLine.query.all()}
    return sum(refresh_item(i) for i in MenuItem.query.filter_by(carbon_auto=True).all() if i.id in ids)


def label(grams):
    """350 → '350 g CO₂e'；1250 → '1.25 kg CO₂e'；0 → ''"""
    grams = int(grams or 0)
    if grams <= 0:
        return ''
    if grams >= 1000:
        kg = grams / 1000
        return f'{kg:.2f}'.rstrip('0').rstrip('.') + ' kg CO₂e'
    return f'{grams} g CO₂e'

