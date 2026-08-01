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
    """加载默认的 YAML 配置"""
    if "default" in _config_cache:
        return _config_cache["default"]

    with open(DEFAULT_CONFIG_PATH, encoding="utf-8") as f:
        config = yaml.safe_load(f)

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

    # 组装成引擎兼容的结构
    result = {
        # === 店铺基础信息 ===
        "shop_name": basic.get("shop_name", ""),
        "boss_name": basic.get("boss_name", ""),
        "years_in_business": basic.get("years_in_business", 0),
        "city": basic.get("city", ""),
        "shop_location": basic.get("shop_location", ""),
        "wechat_id": basic.get("wechat_id", ""),
        "default_material": basic.get("default_material", "particle_board"),

        # === 产品硬参数 ===
        "board_brand": materials.get("default_board_brand", ""),
        "edge_band": materials.get("default_edge_band", ""),
        "hardware_brand": materials.get("default_hardware_brand", ""),
        "eco_level": materials.get("default_eco_level", ""),

        # === 价格信息（供议价状态机用） ===
        "_pricing": pricing,  # 原始价格表，内部方法用
        "_base_price_method": pricing.get("base_price_method", "投影面积"),

        # === 工艺能力（JSON里没单独定义，默认全开，商家可自行添加） ===
        "process_capability": {
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
        },

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


# 材料 key → 中文名 映射
MATERIAL_NAME_MAP = {
    "particle_board": "颗粒板",
    "multi_layer_board": "多层板",
    "osb_board": "欧松板",
    "ecological_board": "生态板",
    "solid_wood": "实木板",
}

# 材料key → 价格字段名 映射
MATERIAL_PRICE_KEY_MAP = {
    "particle_board": "particle_board_price",
    "multi_layer_board": "multi_layer_board_price",
    "osb_board": "osb_board_price",
    "ecological_board": "ecological_board_price",
    "solid_wood": "solid_wood_price",
}


def get_material_name(material_key: str) -> str:
    """材料key转中文名"""
    return MATERIAL_NAME_MAP.get(material_key, material_key)


def get_price_range(config: dict) -> tuple:
    """
    计算价格区间（最低价, 最高价）
    从 _pricing 里读各种板材价格，取最小和最大
    """
    pricing = config.get("_pricing", {})
    prices = []
    for price_key in MATERIAL_PRICE_KEY_MAP.values():
        p = pricing.get(price_key, 0)
        if p and p > 0:
            prices.append(p)
    if not prices:
        return 0, 0
    return min(prices), max(prices)


def get_materials_list_text(config: dict) -> str:
    """
    生成材料价格列表字符串，比如"颗粒板799/多层板1099/生态板1199"
    """
    pricing = config.get("_pricing", {})
    items = []
    for mat_key, price_key in MATERIAL_PRICE_KEY_MAP.items():
        price = pricing.get(price_key, 0)
        if price and price > 0:
            name = MATERIAL_NAME_MAP.get(mat_key, mat_key)
            items.append(f"{name}{price}")
    return "/".join(items)


def get_material_price(config: dict, material_key: str) -> int:
    """获取指定材料的价格"""
    pricing = config.get("_pricing", {})
    price_key = MATERIAL_PRICE_KEY_MAP.get(material_key, "")
    if not price_key:
        return 0
    return pricing.get(price_key, 0)


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
