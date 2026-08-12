# 商家私有配置说明

## 一、目录结构

```
customer_service/shops/
├── shop_config.template.json   # 配置模板（含完整字段，按需填写）
├── README.md                    # 本说明文件
└── demo.json                    # 示例商家配置（合肥尚美，可直接用来测试）
```

每个商家一个 JSON 文件，文件名就是 `shop_id`（不含 `.json` 后缀）。

## 二、核心字段说明

### 1. basic_info（店铺基础信息）
| 字段 | 类型 | 说明 |
| --- | --- | --- |
| shop_name | string | 店铺名称，话术里会用到 |
| boss_name | string | 老板/负责人姓名 |
| years_in_business | number | 从业年数，增加信任感 |
| city | string | 所在城市 |
| shop_location | string | 门店详细地址 |
| wechat_id | string | 留资用的微信号 |
| after_sales_phone | string | 售后联系电话 |
| default_material | string | 默认推荐板材key（对应 boards 里的 key） |
| main_material | string | 主推/高端板材key |

### 2. pricing（价格体系）
| 字段 | 类型 | 说明 |
| --- | --- | --- |
| base_price_method | string | 基础计价方式：投影面积/展开面积/延米 |
| drawer_price | number | 单个抽屉加价（元/个） |
| visible_panel_price | number | 见光板价格（元/平米） |
| handle_price | number | 拉手单价（元/个） |
| measurement_fee | number | 测量定金（元） |
| installation_fee | string | 安装费说明 |
| delivery_fee | string | 运输费说明 |
| design_fee | string | 设计费说明 |

> ⚠️ **旧字段兼容**：`particle_board_price`、`multi_layer_board_price` 等5个板材价格字段已废弃，改用 `materials.boards` 列表配置。旧格式仍能自动兼容。

### 3. materials（材料配置）

#### 3.1 基础配置
| 字段 | 类型 | 说明 |
| --- | --- | --- |
| default_board_brand | string | 默认板材品牌 |
| default_eco_level | string | 默认环保等级 |
| default_edge_band | string | 默认封边工艺 |
| default_hardware_brand | string | 默认五金品牌 |

#### 3.2 板材动态配置（boards 列表）⭐ 新格式
**这是 v2.0 新增的动态板材配置**，商家可以任意添加板材种类，不限制数量。

每个板材的字段：
| 字段 | 类型 | 说明 |
| --- | --- | --- |
| key | string | 唯一标识，英文下划线，代码里用 |
| name | string | 中文名，展示给客户看 |
| brand | string | 品牌名 |
| eco_level | string | 环保等级 |
| price | number | 单价（元/平米） |
| is_default | boolean | 是否默认推荐板材（有且只有一个为 true） |
| is_premium | boolean | 是否高端主打板材 |
| keywords | array | 匹配关键词列表，客户问到这些词能识别出是这种板 |

示例：
```json
"boards": [
  {
    "key": "particle_board",
    "name": "颗粒板",
    "brand": "兔宝宝",
    "eco_level": "ENF级",
    "price": 799,
    "is_default": true,
    "is_premium": false,
    "keywords": ["颗粒板", "刨花板", "799"]
  }
]
```

### 4. processes（工艺配置）⭐ 新格式

**v2.0 新增动态工艺配置**，商家可以任意添加工艺种类，每种工艺自带关键词和问答话术。

每个工艺的字段：
| 字段 | 类型 | 说明 |
| --- | --- | --- |
| key | string | 唯一标识，英文下划线 |
| name | string | 工艺中文名 |
| can_do | boolean | 能不能做 |
| keywords | array | 匹配关键词列表 |
| answer_template | string | **【推荐】** 工艺问答的主模板（全局直答层优先用这个） |
| yes_templates | array | 能做的话术模板列表（系统内置工艺匹配用，随机抽一个） |
| no_templates | array | 不能做的话术模板（可空，空的话用兜底话术） |

示例：
```json
"processes": [
  {
    "key": "door_frame",
    "name": "门窗套",
    "can_do": true,
    "keywords": ["门套", "窗套", "垭口"],
    "yes_templates": [
      "门套窗套我们都做的，可以跟柜子同色。加我微信{{wechat_id}}，我给您报个价？"
    ],
    "no_templates": []
  }
]
```

> 💡 **模板变量支持**：`{{shop_name}}`、`{{wechat_id}}`、`{{board_brand}}`、`{{edge_band}}`、`{{hardware_brand}}` 等所有配置变量都能用。

> 💡 **系统内置工艺**：系统内置了圆弧、榻榻米、玻璃门等7种工艺模板。商家配置里如果定义了同 key 的工艺，会**覆盖**系统内置的。

### 5. service（服务与售后）
| 字段 | 类型 | 说明 |
| --- | --- | --- |
| warranty_years | number | 质保年限 |
| production_cycle_days | string | 生产周期（如 "30-45"） |
| install_days | string | 安装周期（如 "1-2"） |
| design_days | string | 设计周期（如 "7"） |
| deposit_ratio | string | 定金比例（如 "50%"） |
| refundable | boolean | 定金是否可退 |
| rush_available | boolean | 是否支持加急 |
| free_design | boolean | 是否免费设计 |
| free_measurement | boolean | 是否免费测量 |
| after_sales_scope | string | 售后范围说明 |

### 6. sales_script（话术池）
| 字段 | 类型 | 说明 |
| --- | --- | --- |
| welcome_text | string | 欢迎语 |
| selling_points | array | 卖点池（随机抽一个用） |
| concessions | array | 让利钩子池（随机抽一个用） |
| urgency_factors | array | 紧迫感因素池（随机抽一个用） |
| lead_hooks | array | 留资钩子池（随机抽一个用） |
| community_cases | array | 小区案例列表 |

## 三、使用方式

### API调用
在问答接口中传 `shop_id` 参数：
```json
{
  "question": "你们颗粒板多少钱一平？",
  "session_id": "test123",
  "shop_id": "demo"
}
```

- 传了 `shop_id`：加载对应商家的配置
- 不传 `shop_id`：走默认配置（`shop_config.yaml`），向后兼容

### 新增商家
复制 `shop_config.template.json`，改名为 `{shop_id}.json`，填好对应字段就行。

## 四、版本兼容

- **旧格式配置**（没有 boards 和 processes 字段）能自动兼容，不用改就能用
- 旧格式的5个固定价格字段会自动转换成 boards 列表
- 旧格式的12种工艺默认值会保留
- 推荐新商家直接用新格式（boards + processes），更灵活
