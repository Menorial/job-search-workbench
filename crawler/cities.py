# -*- coding: utf-8 -*-
"""Boss直聘城市代码表（公共模块）。

编码规则：中国天气网城市代码（101 + 省码2位 + 市码2位 + 区县码2位）
所有脚本统一从此模块导入，避免多份重复维护不一致。

⚠️ 修正历史 Bug：
- 合肥：1012200100（10位，错误）→ 101220100（安徽段）
- 青岛：101150200（青海段，错误）→ 101120200（山东段）

💡 完整城市列表：运行 fetch_cities.py 从 Boss直聘官网抓取全部城市，
   生成 cities_full.json 后会自动加载（该文件已在 .gitignore 中）。
"""

import json
import os
import re

CITY_MAP = {
    # 直辖市 / 省会 / 主要城市
    "北京": "101010100", "上海": "101020100", "广州": "101280100",
    "深圳": "101280600", "杭州": "101210100", "成都": "101270100",
    "武汉": "101200100", "南京": "101190100", "西安": "101110100",
    "重庆": "101040100", "天津": "101030100", "苏州": "101190400",
    "郑州": "101180100", "长沙": "101250100", "东莞": "101281600",
    "青岛": "101120200", "沈阳": "101070100", "合肥": "101220100",
    "佛山": "101280800", "济南": "101120100", "厦门": "101230200",
    "哈尔滨": "101050100", "福州": "101230100", "大连": "101070200",
    "石家庄": "101090100", "无锡": "101190500", "昆明": "101290100",
    "南宁": "101300100", "长春": "101060100", "贵阳": "101260100",
    "常州": "101190600", "徐州": "101190300", "温州": "101210300",
    # 江苏其它
    "南通": "101190700", "扬州": "101190800", "泰州": "101190900",
    "镇江": "101191000", "淮安": "101191100", "连云港": "101191200",
    "宿迁": "101191300", "盐城": "101191400",
    # 浙江其它
    "宁波": "101210400", "舟山": "101210700", "湖州": "101210800",
    "金华": "101210900", "衢州": "101211000", "丽水": "101211100",
    "台州": "101211200", "绍兴": "101210500", "嘉兴": "101210600",
    # 其它省会（按天气网编码）
    "太原": "101100100", "呼和浩特": "101080100", "南昌": "101240100",
    "海口": "101310100", "乌鲁木齐": "101130100", "拉萨": "101140100",
    "西宁": "101150100", "兰州": "101160100", "银川": "101170100",
    # 全国
    "全国": "0",
}

# 自动加载官网抓取的完整城市列表（fetch_cities.py 生成）
_FULL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cities_full.json")


def _load_full():
    """读取 cities_full.json（不存在或损坏时返回空 dict）。"""
    if not os.path.exists(_FULL_FILE):
        return {}
    try:
        with open(_FULL_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if k and v is not None}


CITY_MAP.update(_load_full())


def refresh_full():
    """重新加载 cities_full.json（网页面板「更新城市列表」后立即生效）。"""
    CITY_MAP.update(_load_full())
    return CITY_MAP

_CITY_PATTERN = None
_SUFFIXES = ("自治州", "地区", "新区", "市", "盟")


def city_names(include_national=False):
    names = list(CITY_MAP.keys())
    if not include_national:
        names = [name for name in names if name != "全国"]
    return names


def city_pattern(names=None):
    """用于从页面文本里识别城市的正则片段（长名优先，避免「长春」被「长」截断）。"""
    names = names if names is not None else city_names()
    ordered = sorted({name for name in names if name}, key=len, reverse=True)
    return "|".join(re.escape(name) for name in ordered)


def normalize_city(value):
    """把用户输入/页面文本归一成 CITY_MAP 里的城市名，识别不了返回 None。

    支持：'成都'、'成都市'、'成都·武侯区'、'北京市海淀区'、'全国'。
    """
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text in CITY_MAP:
        return text
    for suffix in _SUFFIXES:
        if text.endswith(suffix) and text[: -len(suffix)] in CITY_MAP:
            return text[: -len(suffix)]
    for name in sorted(CITY_MAP.keys(), key=len, reverse=True):
        if text.startswith(name):
            return name
    return None


def city_code_of(value):
    """城市名（或代码/序号）-> 城市代码，识别不了返回 None。"""
    name, code = get_city_code(str(value or ""))
    return code


def get_city_code(city_input):
    """把城市名/数字输入转为 (城市名, 城市代码)。

    数字输入兼容两种历史行为：
    - 城市代码（如 101010100）
    - 序号（1 = 第一个城市，2 = 第二个……）
    """
    city_input = str(city_input or "").strip()
    if not city_input:
        return None, None
    if city_input.isdigit():
        if city_input in CITY_MAP.values():
            for name, code in CITY_MAP.items():
                if code == city_input:
                    return name, code
        index = int(city_input) - 1
        names = list(CITY_MAP.keys())
        if 0 <= index < len(names):
            return names[index], CITY_MAP[names[index]]
        return None, None
    name = normalize_city(city_input)
    if name:
        return name, CITY_MAP[name]
    return None, None


if __name__ == "__main__":
    print("共收录 %d 个城市（含 cities_full.json 时更多）" % len(CITY_MAP))
    for name, code in CITY_MAP.items():
        print("  %s: %s" % (name, code))
