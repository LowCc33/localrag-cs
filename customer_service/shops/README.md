# 商家私有配置说明

## 一、目录结构

```
customer_service/shops/
├── shop_config.template.json   # 配置模板（五大块结构，空值+字段说明）
├── README.md                    # 本说明文件
└── demo_shop.json              # 示例商家配置（合肥XX定制，可直接用来测试）
```

每个商家一个 JSON 文件，文件名就是 `shop_id`（不含 `.json` 后缀）。

## 二、五大块字段说明

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

### 2. pricing（价格体系）
| 字段 | 类型 | 说明 |
| --- | --- | --- |
| base_price_method | string | 基础计价方式：投影面积/展开面积/延米 |
| particle_board_price | number | 颗粒板基础价（元/平米） |
| multi_layer_board_price | number | 多层板基础价（元/平米） |
| osb_board_price | number | 欧松板基础价（元/平米） |
| ecological_board_price | number | 生态板基础价（元/平米） |
| solid_wood_price | number | 实木板基础价（元/平米） |
| drawer_price | number | 单个抽屉加价（元/个） |
| visible_panel_price | number | 见光板价格（元/平米） |
| handle_price | number | 拉手单价（元/个） |
| measurement_fee | number | 测量定金（元） |
| installation_fee | string | 安装费说明 |
| delivery_fee | string | 运输费说明 |
| design_fee | string | 设计费说明 |

### 3. materials（材料配置）
| 字段 | 类型 | 说明 |
| --- | --- | --- |
| default_board_brand | string | 默认板材品牌 |
| default_eco_level | string | 默认环保等级 |
| default_edge_band | string | 默认封边工艺 |
| default_hardware_brand | string | 默认五金品牌 |
| available_boards | array | 可选板材列表 |
| available_edge_bands | array | 可选封边工艺列表 |
| available_hardware | array | 可选五金品牌列表 |

### 4. service（服务与售后）
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

### 5. sales_script（话术池）
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
  "shop_id": "demo_shop"
}
```

- 传了 `shop_id`：加载对应商家的配置，注入到问答prompt中
- 不传 `shop_id`：走默认配置（原有的 `shop_config.yaml`），向后兼容

### 新增商家
复制 `shop_config.template.json`，改名为 `{shop_id}.json`，填好对应字段就行。

## 四、注意事项

- 所有价格字段单位是"元"，填数字就行，不用写"元"字
- 空字段可以留空字符串或0，注入prompt时会自动跳过空值
- 数组类型的字段（selling_points等）至少填1项，越多越好，每次随机抽一个用
