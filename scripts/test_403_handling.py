#!/usr/bin/env python3
"""
Тестовый скрипт для проверки обработки ошибки 403 в Scrapy пауке.
Этот скрипт можно использовать для тестирования различных сценариев ошибок.

НОВАЯ ЛОГИКА: При получении 3 ошибок 403 подряд - пауза на 5 минут.
"""

import requests
import json
import time
from datetime import datetime

class Mock403Handler:
    """Имитирует логику обработки 403 и GraphQL ошибок из spider"""
    
    def __init__(self):
        self.consecutive_403_count = 0
        self.max_consecutive_403 = 3
        self.pause_duration = 300  # 5 minutes
        self.is_paused = False
        self.total_403_errors = 0
        
        # GraphQL обработка
        self.graphql_retry_delay = 5
        self.graphql_max_retries = 3
        self.graphql_errors = 0
        self.graphql_retries = 0
        
    def handle_403_error(self, context="unknown"):
        """Имитирует _handle_403_error из spider"""
        self.total_403_errors += 1
        self.consecutive_403_count += 1
        
        print(f"🚫 403 ошибка в контексте: {context}")
        print(f"   Последовательных 403: {self.consecutive_403_count}/{self.max_consecutive_403}")
        
        if self.consecutive_403_count >= self.max_consecutive_403:
            print(f"🚨 ДОСТИГНУТО МАКСИМАЛЬНОЕ КОЛИЧЕСТВО 403 ОШИБОК ({self.max_consecutive_403})")
            print(f"⏸️  СТАВИМ НА ПАУЗУ НА {self.pause_duration//60} МИНУТ")
            self.is_paused = True
            return True  # Требуется пауза
        else:
            remaining = self.max_consecutive_403 - self.consecutive_403_count
            print(f"⚠️  Продолжаем работу. До паузы осталось {remaining} ошибок 403")
            return False  # Пауза не требуется
    
    def handle_successful_request(self):
        """Имитирует успешный запрос"""
        if self.consecutive_403_count > 0:
            print(f"✅ Успешный запрос. Сбрасываем счетчик 403 ошибок (было: {self.consecutive_403_count})")
            self.consecutive_403_count = 0
    
    def handle_graphql_error(self, error_message, retry_count=0):
        """Имитирует обработку GraphQL ошибок"""
        self.graphql_errors += 1
        
        if error_message == "Internal Error" and retry_count < self.graphql_max_retries:
            self.graphql_retries += 1
            print(f"🔄 GraphQL Internal Error. Повтор {retry_count + 1}/{self.graphql_max_retries}")
            print(f"   Ожидание {self.graphql_retry_delay} секунд...")
            return True  # Нужен повтор
        else:
            print(f"❌ GraphQL ошибка '{error_message}' после {retry_count} попыток")
            return False  # Повтор не нужен
            
    def resume_after_pause(self):
        """Имитирует возобновление после паузы"""
        print(f"⏯️  ВОЗОБНОВЛЯЕМ РАБОТУ ПОСЛЕ ПАУЗЫ")
        print(f"   Сбрасываем счетчик последовательных 403 ошибок")
        self.is_paused = False
        self.consecutive_403_count = 0

def test_403_logic():
    """Тестирует логику обработки 403 ошибок"""
    print("🧪 ТЕСТИРОВАНИЕ ЛОГИКИ ОБРАБОТКИ 403 ОШИБОК")
    print("=" * 50)
    
    handler = Mock403Handler()
    
    # Сценарий 1: 2 ошибки 403, затем успешный запрос
    print("\n📝 Сценарий 1: 2 ошибки 403, затем успех")
    handler.handle_403_error("тест 1")
    handler.handle_403_error("тест 2")
    handler.handle_successful_request()
    
    # Сценарий 2: 3 ошибки 403 подряд - должна быть пауза
    print("\n📝 Сценарий 2: 3 ошибки 403 подряд")
    needs_pause = handler.handle_403_error("тест 3")
    assert not needs_pause
    needs_pause = handler.handle_403_error("тест 4")
    assert not needs_pause
    needs_pause = handler.handle_403_error("тест 5")
    assert needs_pause
    print(f"   Статус паузы: {handler.is_paused}")
    
    # Имитируем паузу (в реальности это 5 минут)
    print("   Имитируем паузу (в реальности 5 минут)...")
    time.sleep(2)  # Короткая пауза для теста
    
    # Возобновляем работу
    handler.resume_after_pause()
    
    # Проверяем, что счетчик сброшен
    print(f"   Счетчик после возобновления: {handler.consecutive_403_count}")
    print(f"   Всего 403 ошибок: {handler.total_403_errors}")
    
    print("\n✅ Тест логики 403 ошибок завершен успешно")

def test_graphql_logic():
    """Тестирует логику обработки GraphQL ошибок"""
    print("\n🧪 ТЕСТИРОВАНИЕ ЛОГИКИ ОБРАБОТКИ GRAPHQL ОШИБОК")
    print("=" * 60)
    
    handler = Mock403Handler()
    
    # Сценарий 1: Internal Error с успешным повтором
    print("\n📝 Сценарий 1: GraphQL Internal Error с повтором")
    needs_retry = handler.handle_graphql_error("Internal Error", 0)
    assert needs_retry
    needs_retry = handler.handle_graphql_error("Internal Error", 1)  # Успешный повтор
    print("   ✅ Второй запрос успешен")
    
    # Сценарий 2: Internal Error после максимума попыток
    print("\n📝 Сценарий 2: Internal Error после максимума попыток")
    for i in range(4):  # 4 попытки - больше максимума
        needs_retry = handler.handle_graphql_error("Internal Error", i)
        if i < 3:
            assert needs_retry
        else:
            assert not needs_retry
    
    # Сценарий 3: Другая GraphQL ошибка (не Internal Error)
    print("\n📝 Сценарий 3: Другая GraphQL ошибка")
    needs_retry = handler.handle_graphql_error("Syntax Error", 0)
    assert not needs_retry
    
    print(f"\n📊 Статистика GraphQL:")
    print(f"   Всего ошибок: {handler.graphql_errors}")
    print(f"   Повторных попыток: {handler.graphql_retries}")
    
    print("\n✅ Тест логики GraphQL ошибок завершен успешно")

def test_real_403_response():
    """Тестирует, что произойдет при получении 403 ошибки от реального сайта"""
    
    # URL API otomoto.pl
    test_url = "https://www.otomoto.pl/graphql"
    
    # Заголовки, имитирующие браузер
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/98.0.4758.109 Safari/537.36',
        'Accept': 'application/json',
        'Content-Type': 'application/json',
    }
    
    # Простой GraphQL запрос
    query = {
        "operationName": "advertSearch",
        "variables": {
            "filters": [{"name": "filter_enum_make", "value": "volkswagen"}],
            "itemsPerPage": 32,
            "page": 1
        }
    }
    
    try:
        print(f"\n🌐 ТЕСТИРОВАНИЕ РЕАЛЬНОГО ЗАПРОСА")
        print(f"[{datetime.now()}] Отправляем тестовый запрос к {test_url}")
        response = requests.post(test_url, json=query, headers=headers, timeout=10)
        
        print(f"[{datetime.now()}] Получен статус: {response.status_code}")
        
        if response.status_code == 403:
            print("🚫 ОШИБКА 403: Доступ запрещен")
            print(f"   Заголовки ответа: {dict(response.headers)}")
            print(f"   Тело ответа: {response.text[:200]}...")
            return False
        elif response.status_code == 200:
            print("✅ Запрос успешен")
            try:
                data = response.json()
                if 'errors' in data:
                    print(f"   GraphQL ошибки: {data['errors']}")
                    return False
                else:
                    ads_count = len(data.get('data', {}).get('advertSearch', {}).get('edges', []))
                    print(f"   Получены данные: {ads_count} объявлений")
                    return True
            except json.JSONDecodeError:
                print("❌ Ошибка декодирования JSON")
                return False
        else:
            print(f"⚠️  Неожиданный статус: {response.status_code}")
            print(f"   Тело ответа: {response.text[:200]}...")
            return False
            
    except requests.exceptions.RequestException as e:
        print(f"❌ Ошибка сети: {e}")
        return False

if __name__ == "__main__":
    print("🕷️  Тестирование обработки ошибки 403 в проекте EuroAutoDataHub")
    print("🆕 НОВАЯ ЛОГИКА: 3 ошибки 403 подряд = пауза на 5 минут")
    print("=" * 70)
    
    # Тестируем логику 403
    test_403_logic()
    
    # Тестируем логику GraphQL
    test_graphql_logic()
    test_graphql_logic()
    
    # Тестируем реальный запрос
    success = test_real_403_response()
    
    if not success:
        print("\n⚠️  Рекомендации при получении 403:")
        print("   1. Проверить User-Agent")
        print("   2. Добавить задержки между запросами")
        print("   3. Использовать прокси")
        print("   4. Проверить заголовки запроса")
        print("   5. Возможно, сайт использует anti-bot защиту")
        print("   6. 🆕 При 3 ошибках подряд - пауза на 5 минут")
        print("   7. 🆕 При GraphQL Internal Error - повтор через 5 секунд")
    
    print(f"\n[{datetime.now()}] Тестирование завершено")
