# -*- coding: utf-8 -*-
"""
商家配置加载器
职责：根据 shop_id 加载对应商家的 JSON 配置，转换成引擎兼容的配置格式
架构位置：customer_service/shop_config_loader.py
"""

import json
import os
import yaml

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SHOPS_DIR = os.path.join(BASE_DIR, "shops")
DEFAULT_CONFIG_PATH = os.path.join(BASE_DIR, "shop_config.yaml")

# 配置缓存，避免每次读文件
_config_cache = {}


def load_shop_config(shop_id: str = None) -> dict:
    """
    加载商家配置
    - shop_id 为 None 或空：返回默认配置（shop_config.yaml）
    - 有 shop_id：从 shops/{shop_id}.json 读取，转换成引擎兼容格式
    - 找不到对应文件：返回默认配置，打个日志
    """
    # 无 shop_id → 默认配置
    if not shop_id:
        return _load_default_config()

    # 有缓存直接返回
    cache_key = f"json_{shop_id}"
    if cache_key in _config_cache:
        return _config_cache[cache_key]

    json_path = os.path.join(SHOPS_DIR, f"{shop_id}.json")
    if not os.path.exists(json_path):
        print(f"[配置加载] 找不到商家 {shop_id}，使用默认配置")
        return _load_default_config()

    # 加载JSON并转换成引擎兼容格式
    with open(json_path, encoding="utf-8") as f:
        json_config = json.load(f)

    converted = _convert_json_to_engine_config(json_config)
    _config_cache[cache_key] = converted
    return converted


def _load_default_config() -> dict:
    """加载默认的 YAML 配置，并做旧格式→动态板材格式转换"""
    if "default" in _config_cache:
        return _config_cache["default"]

    with open(DEFAULT_CONFIG_PATH, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # ponytail: 默认 YAML 配置是旧格式，需要转换成动态板材格式
    # 否则 _boards / _board_name_map / _board_price_map 都是空的
    pricing = config.get("_pricing", {})
    materials = {
        "board_names": config.get("board_names", {}),
        "default_board_brand": config.get("board_brand", ""),
        "default_edge_band": config.get("edge_band", ""),
        "default_hardware_brand": config.get("hardware_brand", ""),
        "default_eco_level": config.get("eco_level", ""),
    }
    boards_list = _convert_old_pricing_to_boards(pricing, materials)
    board_maps = _build_board_maps(boards_list)

    # 把动态字段注入 config
    config["_boards"] = boards_list
    config["_board_name_map"] = board_maps["name_map"]
    config["_board_price_map"] = board_maps["price_map"]
    config["_board_keywords_map"] = board_maps["keywords_map"]
    config["_board_brand_map"] = board_maps["brand_map"]
    config["_board_eco_map"] = board_maps["eco_map"]

    _config_cache["default"] = config
    return config


def _convert_json_to_engine_config(json_cfg: dict) -> dict:
    """
    把JSON格式的五大块配置，转换成引擎兼容的配置结构
    引擎用的是 shop_config.yaml 的扁平+嵌套混合结构
    """
    basic = json_cfg.get("basic_info", {})
    pricing = json_cfg.get("pricing", {})
    materials = json_cfg.get("materials", {})
    service = json_cfg.get("service", {})
    sales = json_cfg.get("sales_script", {})

    # ===== 板材配置动态化：从 boards 列表构建映射 =====
    # 如果有 boards 字段（新格式），直接用；没有就从旧格式自动转换
    boards_list = materials.get("boards", [])
    if not boards_list:
        # ponytail: 兼容旧格式，从 pricing 里的5个固定价格字段 + board_names 自动生成 boards 列表
        boards_list = _convert_old_pricing_to_boards(pricing, materials)

    # 构建各种映射表，供引擎使用
    board_maps = _build_board_maps(boards_list)

    # 工艺列表（新格式有 processes 字段，没有就是空列表）
    processes = json_cfg.get("processes", [])

    # 找默认板材和主推板材
    default_board = None
    main_board = None
    for board in boards_list:
        if board.get("is_default"):
            default_board = board.get("key")
        if board.get("is_premium"):
            main_board = board.get("key")
    # basic_info 里的 default_material 优先级最高（如果配置了的话）
    if basic.get("default_material"):
        default_board = basic["default_material"]
    if basic.get("main_material"):
        main_board = basic["main_material"]
    # 兜底：都没设置就选第一个
    if not default_board and boards_list:
        default_board = boards_list[0]["key"]
    if not main_board and boards_list:
        main_board = boards_list[-1]["key"]  # 最后一个当主推（通常最贵的）

    # 组装成引擎兼容的结构
    result = {
        # === 店铺基础信息 ===
        "shop_name": basic.get("shop_name", ""),
        "boss_name": basic.get("boss_name", ""),
        "years_in_business": basic.get("years_in_business", 0),
        "city": basic.get("city", ""),
        "shop_location": basic.get("shop_location", ""),
        "wechat_id": basic.get("wechat_id", ""),
        "default_material": default_board,
        "main_material": main_board,

        # === 产品硬参数 ===
        "board_brand": materials.get("default_board_brand", ""),
        "edge_band": materials.get("default_edge_band", ""),
        "hardware_brand": materials.get("default_hardware_brand", ""),
        "eco_level": materials.get("default_eco_level", ""),
        "board_names": board_maps["name_map"],

        # === 板材动态化数据 ===
        "_boards": boards_list,              # 原始板材列表
        "_board_name_map": board_maps["name_map"],    # key→中文名
        "_board_price_map": board_maps["price_map"],  # key→价格
        "_board_keywords_map": board_maps["keywords_map"],  # key→关键词列表
        "_board_brand_map": board_maps["brand_map"],  # key→品牌
        "_board_eco_map": board_maps["eco_map"],      # key→环保等级

        # === 价格信息（供议价状态机用） ===
        "_pricing": pricing,  # 原始价格表，内部方法用
        "_base_price_method": pricing.get("base_price_method", "投影面积"),

        # === 工艺能力（从processes读取，没有就用默认） ===
        "process_capability": _build_process_capability(json_cfg),
        "_processes": processes,  # 原始工艺列表，引擎侧合并模板用

        # === 付款方式 ===
        "payment_terms": {
            "deposit": service.get("deposit_ratio", "50%"),
            "production_pay": "45%",
            "final_pay": "5%",
            "deposit_amount": str(pricing.get("measurement_fee", 0)),
            "refundable": service.get("refundable", True),
            "warranty_years": str(service.get("warranty_years", 5)),
            "after_sales_phone": basic.get("after_sales_phone", ""),
        },

        # === 工期动态计算参数 ===
        "production": service.get("production", {
            "design_output_per_day": 30,
            "install_output_per_day": 20,
            "chai_dan_days": 2,
            "production_base_days": 15,
            "production_per_10sqm_days": 1,
            "design_min_days": 3,
            "install_min_days": 1,
        }),

        # === 工期 ===
        "production_cycle": {
            "design_days": str(service.get("design_days", "7")),
            "production_days": str(service.get("production_cycle_days", "30-45")),
            "install_days": str(service.get("install_days", "1-2")),
            "rush_available": service.get("rush_available", False),
            "rush_days": "25",
            "repair_days": "7",
        },

        # === 品牌信任 ===
        "trust_points": {
            "factory_location": f"{basic.get('city', '本地')}本地工厂",
            "factory_size": "中等规模，2条生产线",
            "community_cases": sales.get("community_cases", []),
            "boss_is_local": True,
        },

        # === 话术池 ===
        "concessions": sales.get("concessions", []),
        "urgency_factors": sales.get("urgency_factors", []),
        "selling_points": sales.get("selling_points", []),
        "lead_hooks": sales.get("lead_hooks", []),

        # === 兼容旧字段 ===
        "warranty": service.get("after_sales_scope", ""),
    }

    return result


def _convert_old_pricing_to_boards(pricing: dict, materials: dict) -> list:
    """
    兼容旧格式：从 pricing 里的5个固定价格字段 + board_names 自动生成 boards 列表
    旧格式没有 keywords、brand、eco_level 等字段，用名称和合理默认值填充
    """
    old_price_map = {
        "particle_board": "particle_board_price",
        "multi_layer_board": "multi_layer_board_price",
        "osb_board": "osb_board_price",
        "ecological_board": "ecological_board_price",
        "solid_wood": "solid_wood_price",
    }
    old_name_map = materials.get("board_names", {})
    default_brand = materials.get("default_board_brand", "")
    default_eco = materials.get("default_eco_level", "")

    # 默认关键词映射（旧格式没有，用通用关键词）
    default_keywords = {
        "particle_board": ["颗粒板", "刨花板"],
        "multi_layer_board": ["多层板", "胶合板"],
        "osb_board": ["OSB板", "欧松板", "osb", "OSB"],
        "ecological_board": ["生态板", "免漆板"],
        "solid_wood": ["实木", "原木板"],
    }

    # 内置中文名兜底映射（board_names 没配置时用，避免显示英文key）
    BUILTIN_NAME_MAP = {
        "particle_board": "颗粒板",
        "multi_layer_board": "多层板",
        "osb_board": "欧松板",
        "ecological_board": "生态板",
        "solid_wood": "实木板",
    }

    boards = []
    first = True
    for key, price_field in old_price_map.items():
        price = pricing.get(price_field, 0)
        if price and price > 0:
            name = old_name_map.get(key) or BUILTIN_NAME_MAP.get(key, key)
            boards.append({
                "key": key,
                "name": name,
                "brand": default_brand,
                "eco_level": default_eco,
                "price": price,
                "is_default": first,  # 第一个当默认
                "is_premium": False,   # 旧格式不确定，先都设为false
                "keywords": default_keywords.get(key, [name]),
            })
            first = False
    # 最后一个设为主推（通常最贵）
    if boards:
        boards[-1]["is_premium"] = True
    return boards


def _build_board_maps(boards_list: list) -> dict:
    """
    从 boards 列表动态构建各种映射表
    返回：{name_map, price_map, keywords_map, brand_map, eco_map}
    """
    name_map = {}
    price_map = {}
    keywords_map = {}
    brand_map = {}
    eco_map = {}

    for board in boards_list:
        key = board.get("key")
        if not key:
            continue
        name_map[key] = board.get("name", key)
        price_map[key] = board.get("price", 0)
        keywords_map[key] = board.get("keywords", [])
        brand_map[key] = board.get("brand", "")
        eco_map[key] = board.get("eco_level", "")

    return {
        "name_map": name_map,
        "price_map": price_map,
        "keywords_map": keywords_map,
        "brand_map": brand_map,
        "eco_map": eco_map,
    }


def _build_process_capability(json_cfg: dict) -> dict:
    """
    构建工艺能力字典
    优先从 processes 列表读取，没有就用默认的12种
    """
    processes = json_cfg.get("processes", [])
    if processes:
        # 新格式：从 processes 列表构建
        result = {}
        for p in processes:
            key = p.get("key")
            if key:
                result[key] = p.get("can_do", True)
        return result
    # 旧格式：默认全开（和之前一致）
    return {
        "arc_curved": True,
        "door_wall_cabinet": True,
        "hole_board": True,
        "wall_panel": True,
        "interior_door": True,
        "glass_door": True,
        "aluminum_frame": True,
        "ox_horn_handle": False,
        "hidden_handle": True,
        "rebound_device": True,
        "tatami": True,
        "open_kitchen": True,
    }


def convert_processes_to_templates(processes: list) -> list:
    """
    把商家自定义的 processes 列表转换成引擎 hot_questions 的模板格式
    每个工艺转成一个带 process_key 的 hot_question 条目
    返回：hot_questions 格式的列表
    """
    result = []
    for p in processes:
        key = p.get("key")
        if not key:
            continue
        entry = {
            "category": f"process_{key}",
            "process_key": key,
            "keywords": p.get("keywords", []),
            "yes_templates": p.get("yes_templates", []),
            "no_templates": p.get("no_templates", []),
        }
        result.append(entry)
    return result





def get_material_name(material_key: str, config: dict = None) -> str:
    """材料key转中文名，从配置的动态映射里取"""
    if config:
        name_map = config.get("_board_name_map", {})
        if material_key in name_map:
            return name_map[material_key]
        # 兼容旧字段 board_names
        board_names = config.get("board_names", {})
        if material_key in board_names:
            return board_names[material_key]
    return material_key


def get_material_brand(material_key: str, config: dict = None) -> str:
    """材料key转品牌名，从_boards列表里取"""
    if not config:
        return ""
    boards = config.get("_boards", [])
    for b in boards:
        if isinstance(b, dict) and b.get("key") == material_key:
            return b.get("brand", "")
    return ""


def get_price_range(config: dict) -> tuple:
    """
    计算价格区间（最低价, 最高价）
    从动态板材映射里读所有价格，取最小和最大
    """
    price_map = config.get("_board_price_map", {})
    prices = [p for p in price_map.values() if p and p > 0]
    if not prices:
        return 0, 0
    return min(prices), max(prices)


def get_materials_list_text(config: dict) -> str:
    """
    生成材料价格列表字符串，比如"颗粒板799/多层板1099/生态板1199"
    从动态板材映射读取
    """
    boards = config.get("_boards", [])
    items = []
    for board in boards:
        price = board.get("price", 0)
        name = board.get("name", "")
        if price and price > 0 and name:
            items.append(f"{name}{price}")
    return "/".join(items)


def get_material_price(config: dict, material_key: str) -> int:
    """获取指定材料的价格（从动态映射读取）"""
    price_map = config.get("_board_price_map", {})
    return price_map.get(material_key, 0)


def format_shop_info_text(shop_id: str = None) -> str:
    """
    把商家配置格式化成自然语言文本，用于拼接到prompt前面
    空值字段自动跳过
    （注：当前引擎是模板驱动的，这个函数供未来RAG模式使用）
    """
    config = load_shop_config(shop_id)
    if not config:
        return ""

    lines = []
    lines.append(f"你是【{config.get('shop_name', '全屋定制店')}】的AI客服。")
    lines.append("")
    lines.append("【本店信息】")

    # 基础信息
    if config.get("city"):
        lines.append(f"- 所在城市：{config['city']}")
    if config.get("years_in_business"):
        lines.append(f"- 从业年数：{config['years_in_business']}年")
    if config.get("board_brand"):
        lines.append(f"- 主营板材：{config['board_brand']}")
    if config.get("eco_level"):
        lines.append(f"- 环保等级：{config['eco_level']}")
    if config.get("edge_band"):
        lines.append(f"- 封边工艺：{config['edge_band']}")
    if config.get("hardware_brand"):
        lines.append(f"- 五金品牌：{config['hardware_brand']}")

    # 价格（有就加）
    # 价格字段在新JSON的pricing里，老配置没有，这里从兼容结构里取可能不全
    # 直接读原始JSON更准
    if shop_id:
        json_path = os.path.join(SHOPS_DIR, f"{shop_id}.json")
        if os.path.exists(json_path):
            with open(json_path, encoding="utf-8") as f:
                json_cfg = json.load(f)
            pricing = json_cfg.get("pricing", {})
            if pricing.get("base_price_method"):
                lines.append(f"- 计价方式：{pricing['base_price_method']}")
            if pricing.get("particle_board_price"):
                lines.append(f"- 颗粒板基础价：{pricing['particle_board_price']}元/平米")
            if pricing.get("multi_layer_board_price"):
                lines.append(f"- 多层板基础价：{pricing['multi_layer_board_price']}元/平米")

    # 售后
    pay_terms = config.get("payment_terms", {})
    if pay_terms.get("warranty_years"):
        lines.append(f"- 质保年限：{pay_terms['warranty_years']}年")
    if config.get("warranty"):
        lines.append(f"- 售后说明：{config['warranty']}")

    lines.append("")
    return "\n".join(lines)
