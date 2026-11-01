#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Boss直聘城市列表自动抓取工具。

原理：城市选择器数据来自官方接口
    https://www.zhipin.com/wapi/zpgeek/common/data/city/site.json
通过已登录的 Chrome 页面（CDP）执行 fetch 获取（携带登录态，避免被风控），
无需点击弹窗、无需猜测页面结构。

输出：crawler/cities_full.json —— {"城市名": "城市代码", ...}
（cities.py 会自动加载它，全项目生效）

前置条件：Chrome 已登录 Boss直聘，且调试端口可用（先运行 --setup 或用菜单 2）。
"""

import json
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from common.cdp import CDPClient, CDPError
from common.chrome import ChromeError, cdp_alive, ensure_zhipin_target, find_chrome, launch_chrome
from common.console import setup_console
from common.log import setup_logging
from common.paths import CHROME_PROFILE_DIR

CDP_PORT = 9222
API_URL = "https://www.zhipin.com/wapi/zpgeek/common/data/city/site.json"
SEARCH_URL = "https://www.zhipin.com/web/geek/job"
OUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cities_full.json")


def ensure_chrome(log):
    """确保 CDP 可用；没启动就尝试启动并等待端口就绪。"""
    if cdp_alive(CDP_PORT):
        log.info("✅ 已连接本机 Chrome (CDP)")
        return True

    exe = find_chrome()
    if not exe:
        log.error("❌ 未找到 Chrome（可设置环境变量 CHROME_PATH）")
        return False
    log.info("🚀 未检测到 CDP Chrome，正在启动...")
    try:
        if launch_chrome(CHROME_PROFILE_DIR, CDP_PORT, SEARCH_URL, exe):
            log.info("✅ Chrome 已启动 (CDP)")
            return True
    except ChromeError as exc:
        log.error("❌ %s" % exc)
        return False
    log.error("❌ Chrome 启动失败，请先运行: python crawler\\boss_cdp_spider.py --setup")
    return False


def fetch_city_data(client):
    """在页面上下文 fetch 官方城市接口，返回解析后的 dict。"""
    script = """(async () => {
        try {
            const resp = await fetch(%s, {
                credentials: 'include',
                headers: {'Accept': 'application/json, text/plain, */*'}
            });
            return await resp.text();
        } catch (e) {
            return 'ERROR:' + e.toString();
        }
    })()""" % json.dumps(API_URL)

    try:
        text = client.evaluate(script, await_promise=True, timeout=30)
    except CDPError as exc:
        print("❌ 页面 fetch 失败: %s" % exc)
        return None

    if not text:
        print("❌ 接口返回为空")
        return None
    if isinstance(text, str) and text.startswith("ERROR:"):
        print("❌ 页面 fetch 错误: %s" % text[:200])
        return None
    try:
        return json.loads(text)
    except ValueError as exc:
        print("❌ 响应不是合法 JSON: %s" % exc)
        print("   响应前 300 字符: %s" % str(text)[:300])
        return None


def _extract_city(city, city_map):
    name = str(
        city.get("name") or city.get("cityName") or city.get("siteName") or ""
    ).strip()
    code = city.get("code")
    if code is None:
        code = city.get("cityCode") or city.get("siteCode") or city.get("codeId")
    if name and code and name != "全国":
        city_map[name] = str(code)


def parse_city_data(raw):
    """从接口响应解析 {城市名: 代码}。

    真实结构: {code:0, zpData:{siteGroup, hotCitySites, otherCitySites, siteList}}
      - siteGroup: [{firstChar, cityList:[{code,name}]}]  ← 市级全量（主数据源）
      - hotCitySites: 热门城市；otherCitySites: 附加城市
      - siteList: 省级列表，不是城市，忽略
    """
    city_map = {}
    if not isinstance(raw, dict):
        print("❌ 响应不是对象: %s" % type(raw))
        return {}

    zp_data = raw.get("zpData")
    if isinstance(zp_data, dict):
        print("   zpData 键: %s" % list(zp_data.keys()))

        group_count = 0
        group = zp_data.get("siteGroup")
        if isinstance(group, list):
            for item in group:
                if isinstance(item, dict):
                    for city in item.get("cityList") or []:
                        if isinstance(city, dict):
                            _extract_city(city, city_map)
                            group_count += 1
        print("   siteGroup 市级数: %d" % group_count)

        for key in ("hotCitySites", "otherCitySites"):
            bucket = zp_data.get(key)
            if isinstance(bucket, list):
                for city in bucket:
                    if isinstance(city, dict):
                        _extract_city(city, city_map)
    elif isinstance(raw.get("cityList"), list):
        for item in raw["cityList"]:
            if isinstance(item, dict):
                _extract_city(item, city_map)

    city_map["全国"] = "0"
    return city_map


def main():
    setup_console()
    log = setup_logging()

    if not ensure_chrome(log):
        return 1

    try:
        ws_url = ensure_zhipin_target(CDP_PORT, SEARCH_URL)
    except ChromeError as exc:
        log.error("❌ %s" % exc)
        return 1
    if not ws_url:
        log.error("❌ 未找到可用的 Boss直聘标签页，请先登录")
        return 1

    log.info("🌐 请求官方城市接口: %s" % API_URL)
    try:
        with CDPClient(ws_url, default_timeout=40) as client:
            if not client.wait_for_js("document.readyState === 'complete'", timeout=15):
                log.warning("⚠️ 页面未完全加载，继续尝试...")
            raw = fetch_city_data(client)
    except CDPError as exc:
        log.error("❌ CDP 异常: %s" % exc)
        return 1

    if raw is None:
        return 1
    if raw.get("error"):
        log.error("❌ 接口错误: %s" % raw["error"])
        return 1
    code = raw.get("code")
    if code not in (0, None):
        log.error("❌ 接口返回 code=%s（可能未登录或触发风控）" % code)
        log.error("   请先运行菜单选 2 登录 Boss直聘")
        return 1

    city_map = parse_city_data(raw)
    if len(city_map) < 50:
        log.error("❌ 只解析出 %d 个城市，接口结构可能变化" % len(city_map))
        log.error("   原始响应: %s" % json.dumps(raw, ensure_ascii=False)[:500])
        return 1

    tmp = OUT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(city_map, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, OUT_FILE)
    log.info("✅ 已抓取 %d 个城市 → %s" % (len(city_map), OUT_FILE))
    for name, city_code in list(city_map.items())[:10]:
        log.info("    %s: %s" % (name, city_code))
    if len(city_map) > 10:
        log.info("    ... 共 %d 个" % len(city_map))

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        from cities import CITY_MAP

        log.info("\n📊 cities.py 当前共 %d 个城市（含官网完整列表）" % len(CITY_MAP))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
