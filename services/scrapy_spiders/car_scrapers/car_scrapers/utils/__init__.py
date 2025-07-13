# services/scrapy_spiders/car_scrapers/car_scrapers/utils/__init__.py
import sys
import os

# Добавляем путь к data_processor в PYTHONPATH
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))
data_processor_path = os.path.join(project_root, 'services', 'data_processor')

if data_processor_path not in sys.path:
    sys.path.append(data_processor_path)
