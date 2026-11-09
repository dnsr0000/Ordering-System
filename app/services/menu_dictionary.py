"""
菜單翻譯字典：Gemini 沒有金鑰 / 過載時的備援 (app/services/ai_service.rule_translate)。
只收「確定翻得對」的常見分類、台灣小吃、飯麵、早餐、飲料與甜點 (英文, 日文)；查不到就留空，不亂翻
(本機小模型會把蘿蔔糕翻成 Taro cake、蚵仔煎翻成 Fish cake，寫進菜單給外國客人看比留空更糟)。
lookup() 也認得常見前綴 (招牌 / 經典 / 古早味…) 與尾端規格「(大)」「（小）」。
"""
import re

WORDS: dict[str, tuple[str, str]] = {
    # ---- 分類 ----
    '主餐': ('Mains', 'メイン'), '副餐': ('Sides', 'サイド'), '飲料': ('Drinks', 'ドリンク'), '點心': ('Snacks', 'スナック'),
    '甜點': ('Desserts', 'デザート'), '湯品': ('Soups', 'スープ'), '湯類': ('Soups', 'スープ'), '套餐': ('Combos', 'セット'),
    '前菜': ('Starters', '前菜'), '小菜': ('Side dishes', '小皿'), '飯類': ('Rice', 'ご飯もの'), '麵類': ('Noodles', '麺類'),
    '素食': ('Vegetarian', 'ベジタリアン'), '兒童餐': ('Kids', 'キッズ'), '早餐': ('Breakfast', '朝食'), '午餐': ('Lunch', 'ランチ'),
    '晚餐': ('Dinner', 'ディナー'), '限定': ('Limited', '限定'), '熱飲': ('Hot drinks', 'ホットドリンク'),
    '冷飲': ('Cold drinks', 'コールドドリンク'), '炸物': ('Fried food', '揚げ物'), '小吃': ('Taiwanese snacks', '台湾小吃'),
    '滷味': ('Braised snacks', '台湾風煮込み'), '燒烤': ('Grill', '焼き物'), '海鮮': ('Seafood', 'シーフード'),
    '茶飲': ('Tea drinks', 'ティードリンク'), '果汁': ('Juice', 'ジュース'), '冰品': ('Shaved ice', 'かき氷'),
    '加購': ('Add-ons', '追加'), '人氣推薦': ('Popular', '人気'), '季節限定': ('Seasonal', '季節限定'),
    '未分類': ('Other', 'その他'),
    # ---- 飯類 ----
    '滷肉飯': ('Braised pork rice', 'ルーローハン'), '魯肉飯': ('Braised pork rice', 'ルーローハン'),
    '肉燥飯': ('Minced pork rice', '肉そぼろご飯'), '雞肉飯': ('Shredded chicken rice', '鶏肉飯'),
    '雞腿飯': ('Chicken leg rice', '鶏もも肉ご飯'), '排骨飯': ('Pork chop rice', 'パイコーご飯'),
    '控肉飯': ('Braised pork belly rice', '豚の角煮ご飯'), '焢肉飯': ('Braised pork belly rice', '豚の角煮ご飯'),
    '豬腳飯': ('Braised pork knuckle rice', '豚足ご飯'), '鰻魚飯': ('Eel rice', 'うな丼'), '咖哩飯': ('Curry rice', 'カレーライス'),
    '蛋炒飯': ('Egg fried rice', '卵チャーハン'), '炒飯': ('Fried rice', 'チャーハン'), '蝦仁炒飯': ('Shrimp fried rice', '海老チャーハン'),
    '肉絲炒飯': ('Shredded pork fried rice', '豚肉チャーハン'), '燴飯': ('Rice with gravy', 'あんかけご飯'),
    '油飯': ('Taiwanese sticky rice', '台湾風おこわ'), '米糕': ('Glutinous rice cake', '台湾風おこわ'),
    '筒仔米糕': ('Tube rice pudding', '竹筒おこわ'), '白飯': ('Steamed rice', 'ライス'), '便當': ('Lunch box', '弁当'),
    '三寶飯': ('Three roast meats rice', '三種焼味ご飯'), '叉燒飯': ('Char siu rice', 'チャーシューご飯'),
    '燒肉飯': ('Grilled pork rice', '焼肉丼'), '親子丼': ('Chicken and egg rice bowl', '親子丼'), '牛丼': ('Beef rice bowl', '牛丼'),
    '豬排丼': ('Pork cutlet rice bowl', 'カツ丼'), '咖哩豬排飯': ('Pork cutlet curry rice', 'カツカレー'),
    # ---- 麵類 ----
    '牛肉麵': ('Beef noodle soup', '牛肉麺'), '紅燒牛肉麵': ('Braised beef noodle soup', '台湾風煮込み牛肉麺'),
    '清燉牛肉麵': ('Clear broth beef noodle soup', '塩味牛肉麺'), '麻辣牛肉麵': ('Spicy beef noodle soup', '麻辣牛肉麺'),
    '牛肉湯麵': ('Beef noodle soup', '牛肉麺'), '陽春麵': ('Plain noodle soup', 'シンプル汁麺'),
    '乾麵': ('Dry noodles', '汁なし麺'), '麻醬麵': ('Sesame paste noodles', 'ごまだれ麺'), '炸醬麵': ('Zhajiang noodles', 'ジャージャー麺'),
    '擔仔麵': ('Danzai noodles', '担仔麺'), '切仔麵': ('Taiwanese noodle soup', '切仔麺'), '餛飩麵': ('Wonton noodle soup', 'ワンタン麺'),
    '大滷麵': ('Thick soup noodles', 'あんかけ麺'), '酸辣麵': ('Hot and sour noodles', '酸辣麺'), '涼麵': ('Cold noodles', '冷やし麺'),
    '炒麵': ('Fried noodles', '焼きそば'), '炒米粉': ('Fried rice noodles', '焼きビーフン'), '米粉湯': ('Rice noodle soup', 'ビーフンスープ'),
    '米苔目': ('Rice pin noodles', '米苔目'), '粄條': ('Flat rice noodles', '客家きしめん'), '鍋燒意麵': ('Pot-cooked yi noodles', '鍋焼き意麺'),
    '鍋燒烏龍麵': ('Pot-cooked udon', '鍋焼きうどん'), '烏龍麵': ('Udon', 'うどん'), '拉麵': ('Ramen', 'ラーメン'),
    '蚵仔麵線': ('Oyster vermicelli', '牡蠣入り麺線'), '大腸麵線': ('Pork intestine vermicelli', 'モツ入り麺線'), '麵線': ('Vermicelli', '麺線'),
    '義大利麵': ('Pasta', 'パスタ'), '肉醬義大利麵': ('Spaghetti bolognese', 'ミートソーススパゲッティ'),
    '白醬義大利麵': ('Cream sauce pasta', 'クリームパスタ'), '青醬義大利麵': ('Pesto pasta', 'ジェノベーゼパスタ'),
    # ---- 小吃 ----
    '蚵仔煎': ('Oyster omelette', '牡蠣オムレツ'), '臭豆腐': ('Stinky tofu', '臭豆腐'), '麻辣臭豆腐': ('Spicy stinky tofu', '麻辣臭豆腐'),
    '小籠包': ('Xiaolongbao', '小籠包'), '湯包': ('Soup dumplings', '小籠包'), '水餃': ('Boiled dumplings', '水餃子'),
    '煎餃': ('Pan-fried dumplings', '焼き餃子'), '鍋貼': ('Potstickers', '焼き餃子'), '餛飩': ('Wontons', 'ワンタン'),
    '紅油抄手': ('Wontons in chili oil', 'ワンタンのラー油がけ'), '肉圓': ('Taiwanese meatball', 'バーワン（肉圓）'),
    '碗粿': ('Savory rice pudding', 'ワーグイ（台湾風米プディング）'), '蘿蔔糕': ('Turnip cake', '大根餅'),
    '蔥油餅': ('Scallion pancake', 'ネギ餅'), '蔥抓餅': ('Flaky scallion pancake', 'ネギ入りパイ餅'), '割包': ('Gua bao', 'グアバオ（台湾風バーガー）'),
    '刈包': ('Gua bao', 'グアバオ（台湾風バーガー）'), '大腸包小腸': ('Sausage in sticky rice sausage', 'もち米腸詰めのソーセージサンド'),
    '香腸': ('Taiwanese sausage', '台湾ソーセージ'), '雞排': ('Fried chicken cutlet', '台湾風フライドチキン（ジーパイ）'),
    '鹽酥雞': ('Taiwanese popcorn chicken', '台湾風唐揚げ'), '炸雞': ('Fried chicken', 'フライドチキン'), '雞塊': ('Chicken nuggets', 'チキンナゲット'),
    '甜不辣': ('Fried fish cake', 'さつま揚げ'), '關東煮': ('Oden', 'おでん'),
    '肉粽': ('Pork rice dumpling', '肉ちまき'), '粽子': ('Rice dumpling', 'ちまき'), '潤餅': ('Taiwanese spring roll', '台湾風生春巻き'),
    '春捲': ('Spring roll', '春巻き'), '蝦捲': ('Shrimp roll', '海老巻き揚げ'), '花枝丸': ('Squid balls', 'イカ団子'),
    '貢丸': ('Pork balls', '豚肉団子'), '魚丸': ('Fish balls', '魚団子'), '豬血糕': ('Pig blood cake', '豚の血入りもち米ケーキ'),
    '燙青菜': ('Blanched greens', '青菜の湯通し'), '地瓜球': ('Sweet potato balls', 'さつまいもボール'), '薯條': ('Fries', 'フライドポテト'),
    '薯餅': ('Hash brown', 'ハッシュポテト'), '洋蔥圈': ('Onion rings', 'オニオンリング'), '炸魷魚': ('Fried squid', 'イカフライ'),
    '皮蛋豆腐': ('Century egg tofu', 'ピータン豆腐'), '涼拌小黃瓜': ('Cucumber salad', 'きゅうりの和え物'), '滷蛋': ('Braised egg', '煮卵'),
    '荷包蛋': ('Fried egg', '目玉焼き'), '泡菜': ('Kimchi', 'キムチ'), '台式泡菜': ('Taiwanese pickled cabbage', '台湾風キャベツの漬物'),
    # ---- 湯品 ----
    '貢丸湯': ('Pork ball soup', '豚肉団子スープ'), '魚丸湯': ('Fish ball soup', '魚団子スープ'), '餛飩湯': ('Wonton soup', 'ワンタンスープ'),
    '酸辣湯': ('Hot and sour soup', '酸辣湯（サンラータン）'), '玉米濃湯': ('Corn soup', 'コーンスープ'), '蛤蜊湯': ('Clam soup', 'ハマグリのスープ'),
    '味噌湯': ('Miso soup', '味噌汁'), '蛋花湯': ('Egg drop soup', 'かき玉スープ'), '排骨湯': ('Pork rib soup', 'スペアリブスープ'),
    '四神湯': ('Four herbs soup', '四神湯（薬膳スープ）'), '虱目魚湯': ('Milkfish soup', 'サバヒーのスープ'), '羅宋湯': ('Borscht', 'ボルシチ'),
    # ---- 早餐 ----
    '蛋餅': ('Egg crepe', '台湾風卵クレープ（ダンビン）'), '起司蛋餅': ('Cheese egg crepe', 'チーズ入りダンビン'),
    '燒餅油條': ('Sesame flatbread with fried dough', '焼餅と揚げパン'), '燒餅': ('Sesame flatbread', '焼餅（シャオビン）'),
    '油條': ('Fried dough stick', '揚げパン（油条）'), '飯糰': ('Taiwanese rice roll', '台湾風おにぎり'), '饅頭': ('Steamed bun', '蒸しパン（マントウ）'),
    '包子': ('Steamed bun', '肉まん'), '肉包': ('Pork bun', '肉まん'), '菜包': ('Vegetable bun', '野菜まん'),
    '吐司': ('Toast', 'トースト'), '火腿蛋吐司': ('Ham and egg toast', 'ハムエッグトースト'), '三明治': ('Sandwich', 'サンドイッチ'),
    '總匯三明治': ('Club sandwich', 'クラブサンドイッチ'), '漢堡': ('Burger', 'ハンバーガー'), '起司漢堡': ('Cheeseburger', 'チーズバーガー'),
    '牛肉漢堡': ('Beef burger', 'ビーフバーガー'), '雞腿堡': ('Chicken burger', 'チキンバーガー'), '鐵板麵': ('Teppan noodles', '鉄板麺'),
    '豆漿': ('Soy milk', '豆乳'), '米漿': ('Peanut rice milk', 'ピーナッツライスミルク'), '鹹豆漿': ('Savory soy milk soup', '塩味豆乳スープ'),
    # ---- 西式 / 其他 ----
    '沙拉': ('Salad', 'サラダ'), '凱薩沙拉': ('Caesar salad', 'シーザーサラダ'), '披薩': ('Pizza', 'ピザ'), '燉飯': ('Risotto', 'リゾット'),
    '牛排': ('Steak', 'ステーキ'), '豬排': ('Pork cutlet', 'ポークカツ'), '炸豬排': ('Fried pork cutlet', 'とんかつ'),
    '熱狗': ('Hot dog', 'ホットドッグ'), '鬆餅': ('Pancakes', 'パンケーキ'), '咖哩': ('Curry', 'カレー'),
    # ---- 飲料 ----
    '茶': ('Tea', 'お茶'), '紅茶': ('Black tea', '紅茶'), '綠茶': ('Green tea', '緑茶'), '烏龍茶': ('Oolong tea', '烏龍茶'),
    '青茶': ('Light oolong tea', '青茶'), '四季春': ('Four seasons oolong tea', '四季春茶'), '鐵觀音': ('Tieguanyin tea', '鉄観音茶'),
    '東方美人': ('Oriental beauty tea', '東方美人茶'), '高山茶': ('High mountain tea', '高山茶'), '茉莉綠茶': ('Jasmine green tea', 'ジャスミン緑茶'),
    '麥茶': ('Barley tea', '麦茶'), '冬瓜茶': ('Winter melon tea', '冬瓜茶'), '青草茶': ('Herbal tea', '青草茶（ハーブティー）'),
    '奶茶': ('Milk tea', 'ミルクティー'), '珍珠奶茶': ('Bubble milk tea', 'タピオカミルクティー'),
    '波霸奶茶': ('Boba milk tea', 'タピオカミルクティー（大粒）'), '珍珠鮮奶茶': ('Bubble fresh milk tea', 'タピオカ生乳ミルクティー'),
    '鮮奶茶': ('Fresh milk tea', '生乳ミルクティー'), '黑糖珍珠鮮奶': ('Brown sugar boba milk', '黒糖タピオカミルク'),
    '紅茶拿鐵': ('Black tea latte', '紅茶ラテ'), '抹茶拿鐵': ('Matcha latte', '抹茶ラテ'), '奶綠': ('Milk green tea', 'ミルク緑茶'),
    '多多綠茶': ('Yakult green tea', 'ヤクルト緑茶'), '檸檬綠茶': ('Lemon green tea', 'レモン緑茶'), '檸檬紅茶': ('Lemon black tea', 'レモン紅茶'),
    '檸檬汁': ('Lemon juice', 'レモンジュース'), '檸檬愛玉': ('Lemon aiyu jelly drink', 'レモン愛玉ゼリー'), '百香綠茶': ('Passion fruit green tea', 'パッションフルーツ緑茶'),
    '柳橙汁': ('Orange juice', 'オレンジジュース'), '蘋果汁': ('Apple juice', 'アップルジュース'), '西瓜汁': ('Watermelon juice', 'スイカジュース'),
    '木瓜牛奶': ('Papaya milk', 'パパイヤミルク'), '芒果冰沙': ('Mango smoothie', 'マンゴースムージー'), '甘蔗汁': ('Sugarcane juice', 'サトウキビジュース'),
    '咖啡': ('Coffee', 'コーヒー'), '美式咖啡': ('Americano', 'アメリカーノ'), '拿鐵': ('Latte', 'ラテ'), '拿鐵咖啡': ('Caffe latte', 'カフェラテ'),
    '卡布奇諾': ('Cappuccino', 'カプチーノ'), '摩卡': ('Mocha', 'モカ'), '焦糖瑪奇朵': ('Caramel macchiato', 'キャラメルマキアート'),
    '濃縮咖啡': ('Espresso', 'エスプレッソ'), '熱可可': ('Hot chocolate', 'ホットココア'), '可可': ('Cocoa', 'ココア'),
    '可樂': ('Cola', 'コーラ'), '汽水': ('Soda', 'ソーダ'), '雪碧': ('Sprite', 'スプライト'), '礦泉水': ('Mineral water', 'ミネラルウォーター'),
    '鮮奶': ('Fresh milk', '牛乳'), '養樂多': ('Yakult', 'ヤクルト'), '啤酒': ('Beer', 'ビール'), '台灣啤酒': ('Taiwan beer', '台湾ビール'),
    # ---- 甜點 / 冰品 ----
    '豆花': ('Tofu pudding', '豆花（トウファ）'), '芒果冰': ('Mango shaved ice', 'マンゴーかき氷'), '剉冰': ('Shaved ice', 'かき氷'),
    '雪花冰': ('Snow ice', 'スノーアイス'), '紅豆湯': ('Red bean soup', 'おしるこ'), '綠豆湯': ('Mung bean soup', '緑豆スープ'),
    '仙草': ('Grass jelly', '仙草ゼリー'), '燒仙草': ('Hot grass jelly', 'ホット仙草ゼリー'), '愛玉': ('Aiyu jelly', '愛玉ゼリー'),
    '芋圓': ('Taro balls', 'タロイモ団子'), '湯圓': ('Glutinous rice balls', '白玉団子'), '鳳梨酥': ('Pineapple cake', 'パイナップルケーキ'),
    '蛋塔': ('Egg tart', 'エッグタルト'), '布丁': ('Pudding', 'プリン'), '冰淇淋': ('Ice cream', 'アイスクリーム'), '蛋糕': ('Cake', 'ケーキ'),
    '起司蛋糕': ('Cheesecake', 'チーズケーキ'), '提拉米蘇': ('Tiramisu', 'ティラミス'), '鬆餅冰淇淋': ('Waffle with ice cream', 'ワッフルアイス'),
}

# 前綴 (中文, 英文, 日文)：「招牌滷肉飯」→ Signature braised pork rice / 看板ルーローハン
PREFIXES: tuple[tuple[str, str, str], ...] = (
    ('招牌', 'Signature', '看板'), ('經典', 'Classic', '定番'), ('特製', 'Special', '特製'), ('古早味', 'Old-fashioned', '昔ながらの'),
    ('傳統', 'Traditional', '伝統の'), ('手工', 'Handmade', '手作り'), ('現炸', 'Freshly fried', '揚げたて'), ('人氣', 'Popular', '人気の'),
    ('素', 'Vegetarian', 'ベジタリアン'), ('熱', 'Hot', 'ホット'), ('冰', 'Iced', 'アイス'),
)
# 尾端規格「(大)」「（小）」
_SIZE_RE = re.compile(r'\s*[\(（]\s*(大|中|小)\s*[\)）]\s*$')
_SIZES = {'大': ('Large', '大'), '中': ('Medium', '中'), '小': ('Small', '小')}
# 專有名詞 / 專有形容詞 / 品牌：接在前綴後面仍保留首字大寫 (Iced Americano、Signature Taiwanese meatball)
PROPER_WORDS = frozenset({'Taiwan', 'Taiwanese', 'Yakult', 'Americano', 'Caesar', 'Sprite', 'Oriental'})


def _capitalize(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def _after_prefix(text: str) -> str:
    """接在前綴後面的基礎譯名：一般名詞首字改小寫，專有名詞不改"""
    if text.split(' ', 1)[0] in PROPER_WORDS:
        return text
    return text[:1].lower() + text[1:]


def lookup(name: str) -> tuple[str, str] | None:
    """品名 / 分類 → (英文, 日文)；只在整個名稱 (可含前綴與尾端規格) 都認得時回傳，否則 None"""
    text = (name or '').strip()
    if not text:
        return None
    size = None
    m = _SIZE_RE.search(text)
    if m:
        size, text = _SIZES[m.group(1)], text[:m.start()].strip()
    hit = WORDS.get(text)
    if hit is None:
        for zh, en, ja in PREFIXES:
            base = text[len(zh):].strip()
            if text.startswith(zh) and base in WORDS:
                b_en, b_ja = WORDS[base]
                hit = (f'{en} {_after_prefix(b_en)}', f'{ja}{b_ja}')
                break
    if hit is None:
        return None
    en, ja = _capitalize(hit[0]), hit[1]
    if size:
        en, ja = f'{en} ({size[0]})', f'{ja}（{size[1]}）'
    return en, ja
