"""Один «день» обхода: настоящий паук и KafkaPipeline против фейкового сайта.

Запускается отдельным процессом (reactor Twisted нельзя перезапустить): python crawl_day.py <сценарий.json>
Сообщения уходят в Kafka с источником e2e.test, чтобы не смешиваться с настоящими данными.
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services/scrapy_spiders/car_scrapers"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.chdir(ROOT / "services/scrapy_spiders/car_scrapers")  # scrapy.cfg проекта

from fake_site import serve  # noqa: E402
from scrapy.crawler import CrawlerProcess  # noqa: E402
from scrapy.utils.project import get_project_settings  # noqa: E402

from car_scrapers.spiders.autoscout24 import AutoScout24Spider  # noqa: E402
from car_scrapers.spiders.otomoto import OtomotoSpider  # noqa: E402

SOURCE = "e2e.test"
SOURCE_AS24 = "e2e.as24"

with open(sys.argv[1]) as f:
    scenario = json.load(f)
AS24 = scenario.get("site") == "autoscout24"
if AS24:
    import fake_autoscout24
    server = fake_autoscout24.serve(scenario["catalog"])
else:
    server = serve(scenario["catalog"], scenario.get("blocked", []), scenario.get("broken"))
# Прокси: фейковые сайты с тем же каталогом, до «площадки» запросы доходят только через них
proxies = [serve(scenario["catalog"], broken="banned" if p.get("banned") else None)
           for p in scenario.get("proxies", [])] if not AS24 else []
TARGET = "e2e-target.test" if proxies else "127.0.0.1"


class E2ESpider(OtomotoSpider):
    name = "e2e"
    SOURCE_NAME = SOURCE
    BASE_URL = (f"http://{TARGET}/graphql" if proxies else f"http://127.0.0.1:{server.server_port}/graphql")
    allowed_domains = [TARGET]


class E2EAutoScout24Spider(AutoScout24Spider):
    name = "e2e_as24"
    SOURCE_NAME = SOURCE_AS24
    BASE_URL = f"http://127.0.0.1:{server.server_port}"
    allowed_domains = ["127.0.0.1"]


settings = get_project_settings()
settings.setdict({
    "KAFKA_BOOTSTRAP_SERVERS": os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9094"),
    "PAUSE_DURATION": 1, "MAX_PAUSES": 3, "LOG_FILE": None, "LOG_LEVEL": "WARNING",
    "PROGRESS_BAR": "false", "AUTOTHROTTLE_ENABLED": False, "DOWNLOAD_DELAY": 0,
    "RAW_RESPONSES_DIR": "", "PROXIES": [f"http://127.0.0.1:{p.server_port}" for p in proxies],
    **scenario.get("settings", {}),
}, priority="cmdline")
process = CrawlerProcess(settings)
if AS24:
    crawler = process.create_crawler(E2EAutoScout24Spider)
    makes = sorted({make for by_make in scenario["catalog"].values() for make in by_make})
    process.crawl(crawler, makes=",".join(makes), countries=",".join(scenario["catalog"]))
else:
    crawler = process.create_crawler(E2ESpider)
    process.crawl(crawler, makes=",".join(scenario["catalog"]))
process.start()
print("RESULT " + json.dumps({"run_id": crawler.spider.run_id,
                              "shards": {k: v["complete"] for k, v in crawler.spider.shard_results.items()},
                              "proxy_requests": [p.requests for p in proxies],
                              "site_requests": getattr(server, "requests", None)}))
